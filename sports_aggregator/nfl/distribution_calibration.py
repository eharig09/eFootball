"""NFL probability calibration: how should a point forecast become a probability?

Point accuracy is settled elsewhere (qb_player_ablation); this asks which
distribution turns the QB-aware margin and the calibrated total into honest
home-win, cover and over/under probabilities. NFL margins are integers that pile
up on key numbers (3, 7, 10, ...), which a Gaussian cannot represent.

Margin distributions compared, all discretised to integer outcomes so pushes and
key numbers are treated identically across methods:

  normal_global   Gaussian, one sigma from prior out-of-fold residuals
  normal_scaled   Gaussian, sigma = a + b*|predicted margin| fitted on priors
  empirical       prior out-of-fold residuals resampled around the forecast
  keynum_scaled   normal_scaled x per-margin weights learned from prior finals

Everything for season S uses only seasons before S (forecasts are walk-forward
out of fold, weights and sigmas come from earlier seasons). Closing lines are the
outcome threshold being predicted, never an input. Scored by log loss and Brier.
"""
from __future__ import annotations

from contextlib import closing
import math
from typing import Any

import numpy as np

from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import TOTAL_FEATURES, _fit_ridge, _predict

MODEL_VERSION = "nfl-distribution-calibration-v1"
KS = np.arange(-70, 71)
MIN_SIGMA, MAX_SIGMA = 8.0, 20.0
KEY_PRIOR = 5.0
METHODS = ("normal_global", "normal_scaled", "empirical", "keynum_scaled")
TOTAL_METHODS = ("normal_global", "empirical")
EPS = 1e-4

_erf = np.vectorize(math.erf)


def _phi(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf(x / math.sqrt(2.0)))


def _pmf(center: float, sigma: float, weights: np.ndarray | None = None) -> np.ndarray:
    mass = _phi((KS + 0.5 - center) / sigma) - _phi((KS - 0.5 - center) / sigma)
    if weights is not None:
        mass = mass * weights
    return mass / mass.sum()


def _empirical_pmf(center: float, residuals: np.ndarray) -> np.ndarray:
    pseudo = np.clip(np.rint(center + residuals), KS[0], KS[-1]).astype(int)
    counts = np.bincount(pseudo - KS[0], minlength=len(KS)).astype(float)
    # Light smoothing so a single prior sample cannot make a probability exactly 0 or 1.
    counts += 0.05
    return counts / counts.sum()


def _tail(pmf: np.ndarray, threshold: float, ks: np.ndarray = KS) -> float | None:
    """P(outcome > threshold) with exact pushes removed; None if all mass is a push."""
    push = float(pmf[ks == threshold].sum())
    if push >= 1.0:
        return None
    return float(pmf[ks > threshold].sum()) / (1.0 - push)


def _clip(p: float) -> float:
    return min(1 - EPS, max(EPS, p))


def _score(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    if not pairs:
        return {"n": 0}
    p = np.asarray([_clip(a) for a, _ in pairs])
    y = np.asarray([b for _, b in pairs], dtype=float)
    return {
        "n": len(pairs),
        "log_loss": round(float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()), 5),
        "brier": round(float(((p - y) ** 2).mean()), 5),
    }


def _loss_vector(pairs: list[tuple[float, int]]) -> np.ndarray:
    p = np.asarray([_clip(a) for a, _ in pairs])
    y = np.asarray([b for _, b in pairs], dtype=float)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _paired_t(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    """Mean per-game log-loss difference b - a (negative = b better) and its t."""
    d = b - a
    if len(d) < 30:
        return {"n": int(len(d))}
    se = d.std(ddof=1) / math.sqrt(len(d))
    return {"n": int(len(d)), "mean_diff": round(float(d.mean()), 5),
            "t": round(float(d.mean() / se), 3) if se else None}


def _lines(repository: NFLRepository, start: int, end: int):
    with closing(repository._connect()) as connection:
        return {str(r["game_id"]): (r["spread_line"], r["total_line"]) for r in connection.execute(
            "SELECT game_id,spread_line,total_line FROM games WHERE season BETWEEN ? AND ?",
            (int(start), int(end)))}


def _walk(rows, features, target, pred_key):
    """Walk-forward out-of-fold point forecasts, season by season."""
    out = []
    for season in sorted({int(r["season"]) for r in rows}):
        train = [r for r in rows if int(r["season"]) < season]
        test = [dict(r) for r in rows if int(r["season"]) == season]
        model = _fit_ridge(train, features, target)
        if model is None or not test:
            continue
        for r in test:
            r[pred_key] = _predict(model, r)
        out.extend(test)
    return out


def _key_weights(margins: np.ndarray, sigma_hint: float) -> np.ndarray:
    """Per-integer-margin weight: shrunk empirical frequency / smooth Gaussian mass."""
    n = len(margins)
    counts = np.bincount(np.clip(margins, KS[0], KS[-1]).astype(int) - KS[0],
                         minlength=len(KS)).astype(float)
    mu, sd = float(margins.mean()), max(float(margins.std()), sigma_hint * 0.5)
    expected = n * (_phi((KS + 0.5 - mu) / sd) - _phi((KS - 0.5 - mu) / sd))
    weights = (counts + KEY_PRIOR) / (expected + KEY_PRIOR)
    weights[np.abs(KS) > 35] = 1.0
    return weights


def _scaled_sigma(coefs: tuple[float, float], m: float) -> float:
    a, b = coefs
    return float(min(MAX_SIGMA, max(MIN_SIGMA, 1.2533 * (a + b * abs(m)))))


def report(repository: NFLRepository, *, start_season=2013, end_season=2025):
    rows = build_rows(repository, start_season, end_season)
    margin_rows = [r for r in rows if r.get("actual_margin") is not None
                   and all(r.get(k) is not None for k in SHRUNK_CHANGE)]
    preds = _walk(margin_rows, SHRUNK_CHANGE, "actual_margin", "pred_margin")
    total_rows = [r for r in rows if r.get("actual_total") is not None
                  and all(r.get(k) is not None for k in TOTAL_FEATURES)]
    total_preds = {str(r["game_id"]): r for r in _walk(total_rows, TOTAL_FEATURES, "actual_total", "pred_total")}
    lines = _lines(repository, start_season, end_season)

    seasons = sorted({int(r["season"]) for r in preds})
    eval_seasons = seasons[2:]  # needs two prior forecast seasons for residual history
    win = {m: [] for m in METHODS}
    cover = {m: [] for m in METHODS}
    over = {m: [] for m in TOTAL_METHODS}
    cover_calib = []
    bench_win: list[tuple[float, int]] = []   # market line -> Gaussian win probability
    bench_cover: list[tuple[float, int]] = []  # "no information": 50% on every cover

    for season in eval_seasons:
        hist = [r for r in preds if int(r["season"]) < season]
        res = np.asarray([r["actual_margin"] - r["pred_margin"] for r in hist])
        sigma_global = float(min(MAX_SIGMA, max(MIN_SIGMA, res.std())))
        x = np.asarray([abs(r["pred_margin"]) for r in hist])
        slope, intercept = np.polyfit(x, np.abs(res), 1)
        coefs = (float(intercept), float(slope))
        prior_finals = np.asarray([r["actual_margin"] for r in margin_rows if int(r["season"]) < season])
        weights = _key_weights(prior_finals, sigma_global)

        thist = [total_preds[str(r["game_id"])] for r in margin_rows
                 if int(r["season"]) < season and str(r["game_id"]) in total_preds]
        tres = np.asarray([r["actual_total"] - r["pred_total"] for r in thist]) if thist else np.zeros(1)
        total_sigma = float(tres.std()) or 13.0

        prior_mkt = np.asarray([
            r["actual_margin"] - float(lines[str(r["game_id"])][0]) for r in margin_rows
            if int(r["season"]) < season and lines.get(str(r["game_id"]), (None,))[0] is not None])
        sigma_mkt = float(min(MAX_SIGMA, max(MIN_SIGMA, prior_mkt.std()))) if len(prior_mkt) else sigma_global

        for r in [x for x in preds if int(x["season"]) == season]:
            m = float(r["pred_margin"])
            actual = int(round(r["actual_margin"]))
            spread, total_line = lines.get(str(r["game_id"]), (None, None))
            sigma_s = _scaled_sigma(coefs, m)
            if spread is not None and actual != 0:
                p_mkt = _tail(_pmf(float(spread), sigma_mkt), 0.0)
                if p_mkt is not None:
                    bench_win.append((p_mkt, int(actual > 0)))
            if spread is not None and actual != float(spread):
                bench_cover.append((0.5, int(actual > float(spread))))
            pmfs = {
                "normal_global": _pmf(m, sigma_global),
                "normal_scaled": _pmf(m, sigma_s),
                "empirical": _empirical_pmf(m, res),
                "keynum_scaled": _pmf(m, sigma_s, weights),
            }
            for name, pmf in pmfs.items():
                if actual != 0:
                    p_win = _tail(pmf, 0.0)
                    if p_win is not None:
                        win[name].append((p_win, int(actual > 0)))
                if spread is not None and actual != float(spread):
                    p_cov = _tail(pmf, float(spread))
                    if p_cov is not None:
                        cover[name].append((p_cov, int(actual > float(spread))))
                        if name == "keynum_scaled":
                            cover_calib.append((p_cov, int(actual > float(spread))))

            tr = total_preds.get(str(r["game_id"]))
            if tr is not None and total_line is not None and tr["actual_total"] != float(total_line):
                a_total = int(round(tr["actual_total"]))
                kt = np.arange(0, 121)
                t_pmfs = {
                    "normal_global": _total_pmf_normal(tr["pred_total"], total_sigma, kt),
                    "empirical": _total_pmf_empirical(tr["pred_total"], tres, kt),
                }
                for name, pmf in t_pmfs.items():
                    p_over = _tail(pmf, float(total_line), kt)
                    if p_over is not None and a_total != float(total_line):
                        over[name].append((p_over, int(a_total > float(total_line))))

    def block(table):
        scores = {m: _score(v) for m, v in table.items()}
        base = _loss_vector(table["normal_global"])
        vs = {m: _paired_t(base, _loss_vector(v)) for m, v in table.items() if m != "normal_global"}
        return {"scores": scores, "paired_logloss_vs_normal_global": vs}

    buckets = []
    if cover_calib:
        arr = np.asarray(cover_calib)
        edges = [0.0, 0.40, 0.45, 0.50, 0.55, 0.60, 1.0]
        for lo, hi in zip(edges, edges[1:]):
            sel = arr[(arr[:, 0] >= lo) & (arr[:, 0] < hi)]
            if len(sel):
                buckets.append({"predicted_range": [lo, hi], "n": int(len(sel)),
                                "mean_predicted": round(float(sel[:, 0].mean()), 4),
                                "observed_rate": round(float(sel[:, 1].mean()), 4)})
    return {
        "version": MODEL_VERSION,
        "market_used": False,
        "note": "closing lines are only the outcome thresholds; forecasts never see them",
        "evaluation_seasons": eval_seasons,
        "home_win": {**block(win), "benchmark_market_line_gaussian": _score(bench_win)},
        "cover_vs_closing_spread": {**block(cover), "benchmark_constant_50pct": _score(bench_cover),
                                    "keynum_scaled_calibration": buckets},
        "total_over_vs_closing_total": block(over),
        "key_number_weights_last_season": {
            str(int(k)): round(float(w), 3) for k, w in zip(KS, weights) if abs(k) <= 14
        } if eval_seasons else {},
    }


def _total_pmf_normal(center: float, sigma: float, ks: np.ndarray) -> np.ndarray:
    mass = _phi((ks + 0.5 - center) / sigma) - _phi((ks - 0.5 - center) / sigma)
    return mass / mass.sum()


def _total_pmf_empirical(center: float, residuals: np.ndarray, ks: np.ndarray) -> np.ndarray:
    pseudo = np.clip(np.rint(center + residuals), ks[0], ks[-1]).astype(int)
    counts = np.bincount(pseudo - ks[0], minlength=len(ks)).astype(float) + 0.05
    return counts / counts.sum()
