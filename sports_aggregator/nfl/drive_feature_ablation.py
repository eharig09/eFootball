"""NFL xDrives feature ablation: football-only vs market-assisted ridge."""
from __future__ import annotations
from typing import Any
import numpy as np

from sports_aggregator.nfl.drive_projection import (
    build_rows, RIDGE_ALPHA, _summary,
)
from sports_aggregator.nfl.repository import NFLRepository

FOOTBALL_FEATURES = (
    "team_drives",
    "opponent_drives_allowed",
    "team_plays_per_drive",
    "opponent_plays_per_drive_allowed",
    "team_neutral_seconds_per_play",
    "opponent_neutral_seconds_per_play",
    "team_neutral_pass_rate",
    "opponent_neutral_pass_rate",
    "team_epa_per_play",
    "opponent_epa_allowed_per_play",
    "team_success_rate",
    "opponent_success_allowed_rate",
    "team_explosive_rate",
    "opponent_explosive_allowed_rate",
    "rest_diff",
    "home",
    "division_game",
)
MARKET_FEATURES = FOOTBALL_FEATURES + ("market_total", "abs_spread")


def _fit(train: list[dict[str, Any]], features: tuple[str, ...]):
    if len(train) < 100:
        return None
    x = np.asarray([[r[k] for k in features] for r in train], dtype=float)
    y = np.asarray([r["actual_drives"] for r in train], dtype=float)
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
    x = np.asarray([row[k] for k in model["features"]], dtype=float)
    z = (x - model["means"]) / model["scales"]
    return float(model["beta"][0] + z @ model["beta"][1:])


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
        football = _fit(train, FOOTBALL_FEATURES)
        market = _fit(train, MARKET_FEATURES)
        if football is None or market is None:
            continue
        for r in test:
            r["pred_football"] = _predict(football, r)
            r["pred_market"] = _predict(market, r)
        pooled.extend(test)
        folds.append({
            "season": season,
            "football_ridge": _summary(test, "pred_football"),
            "market_ridge": _summary(test, "pred_market"),
        })
    return {
        "version": "nfl-drive-feature-ablation-v1",
        "football_features": list(FOOTBALL_FEATURES),
        "market_features": list(MARKET_FEATURES),
        "walk_forward": folds,
        "pooled": {
            "football_ridge": _summary(pooled, "pred_football"),
            "market_ridge": _summary(pooled, "pred_market"),
        },
    }
