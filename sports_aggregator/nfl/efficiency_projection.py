"""Walk-forward NFL pass/rush efficiency projections.

Models passing and rushing EPA/play separately using pregame offense/defense
state. Market-assisted variants are explicit ablations only.
"""
from __future__ import annotations

import math
from typing import Any
import numpy as np

from sports_aggregator.nfl.drive_projection import build_rows
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-efficiency-v1"
RIDGE_ALPHA = 8.0

COMMON = (
    "team_neutral_seconds_per_play",
    "opponent_neutral_seconds_per_play",
    "team_neutral_pass_rate",
    "opponent_neutral_pass_rate",
    "team_success_rate",
    "opponent_success_allowed_rate",
    "team_explosive_rate",
    "opponent_explosive_allowed_rate",
    "rest_diff",
    "home",
    "division_game",
)
PASS_FEATURES = (
    "team_pass_epa_per_play",
    "opponent_pass_epa_allowed_per_play",
) + COMMON
RUSH_FEATURES = (
    "team_rush_epa_per_play",
    "opponent_rush_epa_allowed_per_play",
) + COMMON
PASS_MARKET_FEATURES = PASS_FEATURES + ("market_total", "abs_spread")
RUSH_MARKET_FEATURES = RUSH_FEATURES + ("market_total", "abs_spread")


def _fit(train: list[dict[str, Any]], features: tuple[str, ...], target: str):
    eligible = [
        r for r in train
        if r.get(target) is not None and all(r.get(k) is not None for k in features)
    ]
    if len(eligible) < 100:
        return None
    x = np.asarray([[r[k] for k in features] for r in eligible], dtype=float)
    y = np.asarray([r[target] for r in eligible], dtype=float)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales == 0] = 1.0
    z = (x - means) / scales
    design = np.column_stack([np.ones(len(z)), z])
    penalty = np.eye(design.shape[1]) * RIDGE_ALPHA
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    return {"features": features, "means": means, "scales": scales, "beta": beta}


def _predict(model, row):
    if model is None or any(row.get(k) is None for k in model["features"]):
        return None
    x = np.asarray([row[k] for k in model["features"]], dtype=float)
    z = (x - model["means"]) / model["scales"]
    return float(model["beta"][0] + z @ model["beta"][1:])


def _summary(rows, key, target):
    vals = [(float(r[key]), float(r[target])) for r in rows
            if r.get(key) is not None and r.get(target) is not None]
    if not vals:
        return {"n": 0}
    err = [p-a for p,a in vals]
    ae = [abs(e) for e in err]
    return {
        "n": len(vals),
        "mae": round(sum(ae)/len(ae), 4),
        "rmse": round(math.sqrt(sum(e*e for e in err)/len(err)), 4),
        "bias": round(sum(err)/len(err), 4),
        "within_0_10": round(sum(e <= 0.10 for e in ae)/len(ae), 4),
        "within_0_20": round(sum(e <= 0.20 for e in ae)/len(ae), 4),
    }


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    rows = build_rows(repository, start_season=start_season, end_season=end_season)
    seasons = sorted({int(r["season"]) for r in rows})
    pooled = []
    folds = []

    for season in seasons:
        train = [r for r in rows if int(r["season"]) < season]
        test = [dict(r) for r in rows if int(r["season"]) == season]
        if len(train) < 100 or not test:
            continue

        pass_football = _fit(train, PASS_FEATURES, "actual_pass_epa_per_play")
        pass_market = _fit(train, PASS_MARKET_FEATURES, "actual_pass_epa_per_play")
        rush_football = _fit(train, RUSH_FEATURES, "actual_rush_epa_per_play")
        rush_market = _fit(train, RUSH_MARKET_FEATURES, "actual_rush_epa_per_play")
        if not all((pass_football, pass_market, rush_football, rush_market)):
            continue

        for r in test:
            r["pred_pass_blend"] = (
                (float(r["team_pass_epa_per_play"]) + float(r["opponent_pass_epa_allowed_per_play"])) / 2.0
                if r.get("team_pass_epa_per_play") is not None
                and r.get("opponent_pass_epa_allowed_per_play") is not None else None
            )
            r["pred_rush_blend"] = (
                (float(r["team_rush_epa_per_play"]) + float(r["opponent_rush_epa_allowed_per_play"])) / 2.0
                if r.get("team_rush_epa_per_play") is not None
                and r.get("opponent_rush_epa_allowed_per_play") is not None else None
            )
            r["pred_pass_football"] = _predict(pass_football, r)
            r["pred_pass_market"] = _predict(pass_market, r)
            r["pred_rush_football"] = _predict(rush_football, r)
            r["pred_rush_market"] = _predict(rush_market, r)

        pooled.extend(test)
        folds.append({
            "season": season,
            "pass": {
                "blend": _summary(test, "pred_pass_blend", "actual_pass_epa_per_play"),
                "football_ridge": _summary(test, "pred_pass_football", "actual_pass_epa_per_play"),
                "market_ridge": _summary(test, "pred_pass_market", "actual_pass_epa_per_play"),
            },
            "rush": {
                "blend": _summary(test, "pred_rush_blend", "actual_rush_epa_per_play"),
                "football_ridge": _summary(test, "pred_rush_football", "actual_rush_epa_per_play"),
                "market_ridge": _summary(test, "pred_rush_market", "actual_rush_epa_per_play"),
            },
        })

    return {
        "version": MODEL_VERSION,
        "targets": ["passing EPA/play", "rushing EPA/play"],
        "features": {
            "pass_football": list(PASS_FEATURES),
            "rush_football": list(RUSH_FEATURES),
            "pass_market": list(PASS_MARKET_FEATURES),
            "rush_market": list(RUSH_MARKET_FEATURES),
        },
        "walk_forward": folds,
        "pooled": {
            "pass": {
                "blend": _summary(pooled, "pred_pass_blend", "actual_pass_epa_per_play"),
                "football_ridge": _summary(pooled, "pred_pass_football", "actual_pass_epa_per_play"),
                "market_ridge": _summary(pooled, "pred_pass_market", "actual_pass_epa_per_play"),
            },
            "rush": {
                "blend": _summary(pooled, "pred_rush_blend", "actual_rush_epa_per_play"),
                "football_ridge": _summary(pooled, "pred_rush_football", "actual_rush_epa_per_play"),
                "market_ridge": _summary(pooled, "pred_rush_market", "actual_rush_epa_per_play"),
            },
        },
    }
