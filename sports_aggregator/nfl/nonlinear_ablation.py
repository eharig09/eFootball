"""NFL non-linear second-stage ablation (margin and total).

The calibrated margin and total are ridge regressions. This asks whether
(a) extra context -- rest, division game, wind, temperature, dome -- and
(b) a shallow gradient-boosted correction fitted on the ridge residuals
improve walk-forward error. The boosted model is deliberately weak (depth 2,
strong regularisation, optional shrink) because a season is only ~270 games.

Leak/caveat policy: every model for season S trains on seasons before S. Weather
is the *observed* game-time value stored by nflverse; a live forecast would only
have a forecast, so weather gains here are an upper bound. Market lines are used
only as a benchmark in the report, never as features.
"""
from __future__ import annotations

from contextlib import closing
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, _paired, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import TOTAL_FEATURES, _fit_ridge, _predict

MODEL_VERSION = "nfl-nonlinear-ablation-v1"
CONTEXT = ("ctx_rest_diff", "ctx_division", "ctx_wind", "ctx_temp_below_50", "ctx_dome")
BOOST = dict(max_depth=2, max_iter=150, learning_rate=0.03, min_samples_leaf=40,
             l2_regularization=5.0, random_state=0)
SHRINKS = (0.5, 1.0)


def add_context(rows: list[dict[str, Any]], repository: NFLRepository) -> None:
    with closing(repository._connect()) as connection:
        games = {str(g["game_id"]): g for g in connection.execute(
            """SELECT game_id,home_rest,away_rest,division_game,roof,temperature,wind,
                      spread_line,total_line FROM games""")}
    for r in rows:
        g = games.get(str(r["game_id"]))
        if g is None:
            continue
        dome = g["roof"] in ("dome", "closed")
        outdoor_known = (not dome) and g["wind"] is not None and g["temperature"] is not None
        if not (dome or outdoor_known):
            continue  # unknown weather: leave the row out of the context sample
        r["ctx_rest_diff"] = float(g["home_rest"] or 0) - float(g["away_rest"] or 0)
        r["ctx_division"] = float(g["division_game"] or 0)
        r["ctx_wind"] = 0.0 if dome else float(g["wind"])
        r["ctx_temp_below_50"] = 0.0 if dome else max(0.0, 50.0 - float(g["temperature"]))
        r["ctx_dome"] = 1.0 if dome else 0.0
        r["market_spread"] = g["spread_line"]
        r["market_total"] = g["total_line"]


def _gbm_features(features: tuple[str, ...]):
    return lambda rows: np.asarray([[r[k] for k in features] for r in rows], dtype=float)


def _walk(sample, base_features, ctx_features, target, shrinks=SHRINKS):
    """Per season: ridge, ridge+context, and ridge+context+boost at each shrink."""
    full = base_features + ctx_features
    pooled, folds = [], []
    for season in sorted({int(r["season"]) for r in sample}):
        train = [r for r in sample if int(r["season"]) < season]
        test = [dict(r) for r in sample if int(r["season"]) == season]
        ridge = _fit_ridge(train, base_features, target)
        ridge_ctx = _fit_ridge(train, full, target)
        if ridge is None or ridge_ctx is None or not test:
            continue
        residual = np.asarray([r[target] - _predict(ridge_ctx, r) for r in train])
        boost = HistGradientBoostingRegressor(**BOOST).fit(_gbm_features(full)(train), residual)
        correction = boost.predict(_gbm_features(full)(test))
        for r, c in zip(test, correction):
            r["p_ridge"] = _predict(ridge, r)
            r["p_ridge_ctx"] = _predict(ridge_ctx, r)
            for s in shrinks:
                r[f"p_boost_{s}"] = r["p_ridge_ctx"] + s * float(c)
        pooled.extend(test)
        folds.append(test)
    return pooled, folds


def _mae(rows, key, target):
    e = np.asarray([float(r[key]) - float(r[target]) for r in rows])
    return round(float(np.abs(e).mean()), 4), round(float(np.sqrt((e * e).mean())), 4)


def _err_rows(rows, key, target):
    return [{"actual_margin": float(r[target]), key: float(r[key])} for r in rows]


def _block(pooled, folds, target, labels, bench_key=None):
    out = {"n": len(pooled), "pooled": {}, "paired_vs_ridge": {}, "seasons_better_than_ridge": {}}
    for label in labels:
        mae, rmse = _mae(pooled, label, target)
        out["pooled"][label] = {"mae": mae, "rmse": rmse}
        if label == "p_ridge":
            continue
        a = _err_rows(pooled, "p_ridge", target)
        b = _err_rows(pooled, label, target)
        merged = [{"actual_margin": x["actual_margin"], "pred_a": x["p_ridge"], "pred_b": y[label]}
                  for x, y in zip(a, b)]
        out["paired_vs_ridge"][label] = _paired(merged, "pred_a", "pred_b")
        out["seasons_better_than_ridge"][label] = (
            f"{sum(_mae(f, label, target)[0] < _mae(f, 'p_ridge', target)[0] for f in folds)}/{len(folds)}")
    if bench_key:
        rows = [r for r in pooled if r.get(bench_key) is not None]
        out["market_benchmark_mae"] = _mae(
            [dict(r, p_market=r[bench_key]) for r in rows], "p_market", target)[0]
        out["market_benchmark_n"] = len(rows)
    return out


def report(repository: NFLRepository, *, start_season=2013, end_season=2025):
    rows = build_rows(repository, start_season, end_season)
    add_context(rows, repository)
    labels = ["p_ridge", "p_ridge_ctx"] + [f"p_boost_{s}" for s in SHRINKS]

    m_need = set(SHRUNK_CHANGE) | set(CONTEXT)
    m_sample = [r for r in rows if r.get("actual_margin") is not None
                and all(r.get(k) is not None for k in m_need)]
    m_pool, m_folds = _walk(m_sample, SHRUNK_CHANGE, CONTEXT, "actual_margin")

    t_need = set(TOTAL_FEATURES) | set(CONTEXT)
    t_sample = [r for r in rows if r.get("actual_total") is not None
                and all(r.get(k) is not None for k in t_need)]
    t_pool, t_folds = _walk(t_sample, TOTAL_FEATURES, CONTEXT, "actual_total")

    return {
        "version": MODEL_VERSION,
        "market_used": False,
        "boost_params": BOOST,
        "caveat": "weather is observed game-time weather; a live forecast would only have a forecast",
        "margin": _block(m_pool, m_folds, "actual_margin", labels, "market_spread"),
        "total": _block(t_pool, t_folds, "actual_total", labels, "market_total"),
    }
