"""Incremental walk-forward feature ablation for the CFB margin model.

Builds on the validated dual total/margin architecture and asks which
pregame football-strength features actually improve margin/score accuracy.
Vegas is never a margin-model feature; market spread is used only for
compression diagnostics.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.xpoints import DATASET_VERSION as XPOINTS_VERSION


FEATURE_SETS = {
    "base": ("raw_margin", "ppd_diff", "drive_diff"),
    "plus_elo": ("raw_margin", "ppd_diff", "drive_diff", "elo_diff"),
    "plus_core": ("raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin"),
    "plus_fpi": ("raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin", "fpi_margin"),
    "plus_recent": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff",
    ),
    "plus_yards": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "yards_diff",
    ),
    "plus_returning_production": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "yards_diff", "returning_ppa_diff",
    ),
}


def _fit_ridge(train: list[dict[str, Any]], features: tuple[str, ...], l2: float = 2.0):
    rows = [r for r in train if r.get("actual_margin") is not None
            and all(r.get(k) is not None for k in features)]
    if len(rows) < 100:
        return None
    means = {k: sum(float(r[k]) for r in rows) / len(rows) for k in features}
    scales = {}
    for k in features:
        s = math.sqrt(sum((float(r[k]) - means[k]) ** 2 for r in rows) / len(rows))
        scales[k] = s or 1.0
    size = len(features) + 1
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size
    for r in rows:
        x = [1.0] + [(float(r[k]) - means[k]) / scales[k] for k in features]
        y = float(r["actual_margin"])
        for i in range(size):
            xty[i] += x[i] * y
            for j in range(size):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, size):
        xtx[i][i] += l2
    aug = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    n = size
    for col in range(n):
        pivot = max(range(col, n), key=lambda rr: abs(aug[rr][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for rr in range(col + 1, n):
            factor = aug[rr][col] / aug[col][col]
            for cc in range(col, n + 1):
                aug[rr][cc] -= factor * aug[col][cc]
    beta = [0.0] * n
    for rr in range(n - 1, -1, -1):
        beta[rr] = (
            aug[rr][n] - sum(aug[rr][cc] * beta[cc] for cc in range(rr + 1, n))
        ) / aug[rr][rr]
    return {
        "features": features, "means": means, "scales": scales,
        "beta": beta, "n": len(rows), "l2": l2,
    }


def _predict(model, row):
    if model is None or any(row.get(k) is None for k in model["features"]):
        return None
    value = model["beta"][0]
    for i, key in enumerate(model["features"], 1):
        value += model["beta"][i] * (
            (float(row[key]) - model["means"][key]) / model["scales"][key]
        )
    return float(value)


def _linear_total_fit(train):
    pairs = [(r["raw_total"], r["actual_total"]) for r in train
             if r.get("raw_total") is not None and r.get("actual_total") is not None]
    if len(pairs) < 50:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    var = sum((x - mx) ** 2 for x, _ in pairs)
    if var <= 1e-12:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pairs) / var
    return {"intercept": my - slope * mx, "slope": slope, "n": len(pairs)}


def _load(repository, start: int, end: int, version: str):
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT p.game_id,p.season,p.side,p.team,p.opponent,
                      p.projected_offensive_points,p.projected_points_per_drive,
                      p.projected_drives,p.projected_total_yards,p.actual_score_points,
                      g.week,g.home_team,g.away_team,
                      x.elo_difference,x.core_margin,x.fpi_margin,
                      x.team_recent_margin,x.opponent_recent_margin,
                      rp.percent_ppa AS returning_percent_ppa,
                      (SELECT AVG(gl.spread) FROM game_lines gl
                       WHERE gl.game_id=p.game_id AND gl.spread IS NOT NULL) market_spread
               FROM cfb_projection_backtest p
               JOIN games g USING(game_id)
               LEFT JOIN cfb_xpoints_dataset x
                 ON x.game_id=p.game_id AND x.team=p.team AND x.dataset_version=?
               LEFT JOIN returning_production rp
                 ON rp.season=p.season AND rp.team=p.team
               WHERE p.backtest_version=? AND p.season BETWEEN ? AND ?
                 AND p.projected_offensive_points IS NOT NULL
                 AND p.actual_score_points IS NOT NULL
               ORDER BY p.season,g.week,p.game_id,p.side""",
            (XPOINTS_VERSION, version, int(start), int(end)),
        )]
    grouped = defaultdict(dict)
    for r in rows:
        grouped[int(r["game_id"])][str(r["side"])] = r
    out = []
    for gid, sides in grouped.items():
        h, a = sides.get("home"), sides.get("away")
        if not h or not a:
            continue
        rh = float(h["projected_offensive_points"]); ra = float(a["projected_offensive_points"])
        ah = float(h["actual_score_points"]); aa = float(a["actual_score_points"])
        out.append({
            "game_id": gid, "season": int(h["season"]), "week": int(h["week"] or 0),
            "raw_total": rh + ra, "raw_margin": rh - ra,
            "actual_total": ah + aa, "actual_margin": ah - aa,
            "ppd_diff": (
                float(h["projected_points_per_drive"]) - float(a["projected_points_per_drive"])
                if h["projected_points_per_drive"] is not None and a["projected_points_per_drive"] is not None else None
            ),
            "drive_diff": (
                float(h["projected_drives"]) - float(a["projected_drives"])
                if h["projected_drives"] is not None and a["projected_drives"] is not None else None
            ),
            "yards_diff": (
                float(h["projected_total_yards"]) - float(a["projected_total_yards"])
                if h["projected_total_yards"] is not None and a["projected_total_yards"] is not None else None
            ),
            "elo_diff": float(h["elo_difference"]) if h["elo_difference"] is not None else None,
            "core_margin": float(h["core_margin"]) if h["core_margin"] is not None else None,
            "fpi_margin": float(h["fpi_margin"]) if h["fpi_margin"] is not None else None,
            "recent_margin_diff": (
                float(h["team_recent_margin"]) - float(h["opponent_recent_margin"])
                if h["team_recent_margin"] is not None and h["opponent_recent_margin"] is not None else None
            ),
            "returning_ppa_diff": (
                float(h["returning_percent_ppa"]) - float(a["returning_percent_ppa"])
                if h["returning_percent_ppa"] is not None and a["returning_percent_ppa"] is not None else None
            ),
            "market_spread": float(h["market_spread"]) if h["market_spread"] is not None else None,
        })
    return out


def _bucket(x):
    if x < 3: return "<3"
    if x < 7: return "3-6.5"
    if x < 14: return "7-13.5"
    return "14+"


def _metrics(rows, key):
    vals = [r for r in rows if r.get(key) is not None and r.get("cal_total") is not None]
    if not vals:
        return {"n": 0}
    margin_err = [abs(float(r[key]) - r["actual_margin"]) for r in vals]
    score_err = []
    for r in vals:
        ph = (r["cal_total"] + float(r[key])) / 2
        pa = (r["cal_total"] - float(r[key])) / 2
        actual_home = (r["actual_total"] + r["actual_margin"]) / 2
        actual_away = (r["actual_total"] - r["actual_margin"]) / 2
        score_err.extend([abs(ph - actual_home), abs(pa - actual_away)])
    return {
        "n": len(vals),
        "margin_mae": round(sum(margin_err)/len(margin_err), 4),
        "score_mae": round(sum(score_err)/len(score_err), 4),
    }


def _compression(rows, key):
    usable = [r for r in rows if r.get(key) is not None and r.get("market_spread") not in (None, 0)]
    if not usable:
        return {"n": 0}
    recs = []
    for r in usable:
        market_home = -float(r["market_spread"])
        fav_home = market_home > 0
        market_abs = abs(market_home)
        model_fav = float(r[key]) if fav_home else -float(r[key])
        change = model_fav - market_abs
        recs.append((market_abs, model_fav, change))
    def one(vals):
        if not vals: return {"n": 0}
        n = len(vals)
        mm = sum(v[0] for v in vals)/n
        fm = sum(v[1] for v in vals)/n
        return {
            "n": n,
            "compression_ratio": round(fm/mm, 4) if mm else None,
            "mean_favorite_margin_change": round(sum(v[2] for v in vals)/n, 3),
            "favorite_more_favored_rate": round(sum(v[2] > .05 for v in vals)/n, 4),
            "underdog_closer_rate": round(sum((v[1] >= 0 and v[2] < -.05) for v in vals)/n, 4),
            "favorite_flipped_rate": round(sum(v[1] < 0 for v in vals)/n, 4),
        }
    return {
        "overall": one(recs),
        "by_bucket": {b: one([v for v in recs if _bucket(v[0]) == b])
                      for b in ("<3","3-6.5","7-13.5","14+")},
    }


def report(repository, *, from_season=2023, to_season=2025,
           training_from_season=2021, backtest_version=BACKTEST_VERSION):
    games = _load(repository, min(training_from_season, from_season), to_season, backtest_version)
    folds = []; pooled = []
    for season in range(int(from_season), int(to_season)+1):
        train = [r for r in games if training_from_season <= r["season"] < season]
        test = [dict(r) for r in games if r["season"] == season]
        tf = _linear_total_fit(train)
        if tf is None or not test:
            continue
        models = {name: _fit_ridge(train, features) for name, features in FEATURE_SETS.items()}
        for r in test:
            r["cal_total"] = tf["intercept"] + tf["slope"] * r["raw_total"]
            for name, model in models.items():
                r[f"margin_{name}"] = _predict(model, r)
        pooled.extend(test)
        folds.append({
            "season": season,
            "train_games": len(train),
            "test_games": len(test),
            "models": {
                name: {
                    "training_rows": model["n"] if model else 0,
                    "features": list(FEATURE_SETS[name]),
                    "metrics": _metrics(test, f"margin_{name}"),
                    "compression": _compression(test, f"margin_{name}"),
                }
                for name, model in models.items()
            },
        })
    return {
        "version": "cfb-margin-feature-ablation-v1",
        "backtest_version": backtest_version,
        "from_season": from_season,
        "to_season": to_season,
        "training_from_season": training_from_season,
        "feature_sets": {k:list(v) for k,v in FEATURE_SETS.items()},
        "walk_forward": folds,
        "pooled": {
            name: {
                "metrics": _metrics(pooled, f"margin_{name}"),
                "compression": _compression(pooled, f"margin_{name}"),
            }
            for name in FEATURE_SETS
        },
    }
