"""Walk-forward NFL neutral pass-rate projection.

Projects neutral-situation pass rate from pregame team/opponent tendencies and
football context. Market-assisted variant is retained only as an ablation.
"""
from __future__ import annotations

import math
from typing import Any
import numpy as np

from sports_aggregator.nfl.drive_projection import build_rows
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-pass-rate-v1"
RIDGE_ALPHA = 8.0

BASE_FEATURES = (
    "team_neutral_pass_rate",
    "opponent_neutral_pass_rate",
    "team_plays_per_drive",
    "opponent_plays_per_drive_allowed",
    "team_neutral_seconds_per_play",
    "opponent_neutral_seconds_per_play",
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
MARKET_FEATURES = BASE_FEATURES + ("market_total", "abs_spread")


def _fit(train: list[dict[str, Any]], features: tuple[str, ...]):
    eligible = [r for r in train if r.get("actual_neutral_pass_rate") is not None]
    if len(eligible) < 100:
        return None
    x = np.asarray([[r[k] for k in features] for r in eligible], dtype=float)
    y = np.asarray([r["actual_neutral_pass_rate"] for r in eligible], dtype=float)
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
    return min(1.0, max(0.0, float(model["beta"][0] + z @ model["beta"][1:])))


def _summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    vals = [
        (float(r[key]), float(r["actual_neutral_pass_rate"]))
        for r in rows
        if r.get(key) is not None and r.get("actual_neutral_pass_rate") is not None
    ]
    if not vals:
        return {"n": 0}
    errors = [p - a for p, a in vals]
    ae = [abs(e) for e in errors]
    return {
        "n": len(vals),
        "mae": round(sum(ae) / len(ae), 4),
        "rmse": round(math.sqrt(sum(e * e for e in errors) / len(errors)), 4),
        "bias": round(sum(errors) / len(errors), 4),
        "within_0_05": round(sum(e <= 0.05 for e in ae) / len(ae), 4),
        "within_0_10": round(sum(e <= 0.10 for e in ae) / len(ae), 4),
    }


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    rows = build_rows(repository, start_season=start_season, end_season=end_season)
    seasons = sorted({int(r["season"]) for r in rows})
    pooled = []
    folds = []

    for season in seasons:
        train = [r for r in rows if int(r["season"]) < season]
        test = [dict(r) for r in rows if int(r["season"]) == season]
        football = _fit(train, BASE_FEATURES)
        market = _fit(train, MARKET_FEATURES)
        if football is None or market is None or not test:
            continue

        league = (
            sum(float(r["actual_neutral_pass_rate"]) for r in train
                if r.get("actual_neutral_pass_rate") is not None)
            / sum(1 for r in train if r.get("actual_neutral_pass_rate") is not None)
        )

        for r in test:
            r["pred_league"] = league
            r["pred_team"] = float(r["team_neutral_pass_rate"])
            r["pred_blend"] = (
                float(r["team_neutral_pass_rate"]) + float(r["opponent_neutral_pass_rate"])
            ) / 2.0
            r["pred_football_ridge"] = _predict(football, r)
            r["pred_market_ridge"] = _predict(market, r)

        pooled.extend(test)
        folds.append({
            "season": season,
            "train_rows": len(train),
            "test_rows": len(test),
            "league": _summary(test, "pred_league"),
            "team": _summary(test, "pred_team"),
            "blend": _summary(test, "pred_blend"),
            "football_ridge": _summary(test, "pred_football_ridge"),
            "market_ridge": _summary(test, "pred_market_ridge"),
        })

    return {
        "version": MODEL_VERSION,
        "target": "neutral-situation pass rate",
        "features": {
            "football_ridge": list(BASE_FEATURES),
            "market_ridge": list(MARKET_FEATURES),
        },
        "walk_forward": folds,
        "pooled": {
            "league": _summary(pooled, "pred_league"),
            "team": _summary(pooled, "pred_team"),
            "blend": _summary(pooled, "pred_blend"),
            "football_ridge": _summary(pooled, "pred_football_ridge"),
            "market_ridge": _summary(pooled, "pred_market_ridge"),
        },
    }
