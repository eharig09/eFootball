"""End-to-end NFL Football Lab scoring bridge.

Generates walk-forward out-of-fold predictions for the frozen market-free core:
xDrives -> xPlays/Drive -> neutral pass rate -> pass EPA/play -> rush EPA/play.

A second-stage ridge maps only PRIOR-SEASON out-of-fold core predictions to
actual points per drive, then reconstructs expected team points. This prevents
the scoring layer from training on in-sample first-stage predictions.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from sports_aggregator.nfl.drive_projection import build_rows, STATE_SEASON_DECAY
from sports_aggregator.nfl.drive_feature_ablation import (
    FOOTBALL_FEATURES as DRIVE_FEATURES,
    _fit as _fit_drive,
    _predict as _predict_drive,
)
from sports_aggregator.nfl.plays_projection import (
    BASE_FEATURES as PLAYS_FEATURES,
    _fit as _fit_plays,
    _predict as _predict_plays,
)
from sports_aggregator.nfl.efficiency_projection import (
    PASS_FEATURES,
    _fit as _fit_efficiency,
    _predict as _predict_efficiency,
)
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-scoring-bridge-v1"
RIDGE_ALPHA = 8.0

SCORING_FEATURES = (
    "pred_drives",
    "pred_plays_per_drive",
    "pred_total_plays",
    "pred_pass_rate",
    "pred_pass_epa",
    "pred_rush_epa",
    "pred_combined_epa_per_play",
    "pred_combined_epa",
)


def _score_map(repository: NFLRepository, start_season: int, end_season: int) -> dict[tuple[str, str], float]:
    repository.initialize()
    with repository._connect() as connection:
        rows = connection.execute(
            """SELECT game_id,home_team,away_team,home_score,away_score
               FROM games
               WHERE season BETWEEN ? AND ? AND completed=1
                 AND home_score IS NOT NULL AND away_score IS NOT NULL""",
            (int(start_season), int(end_season)),
        )
        out: dict[tuple[str, str], float] = {}
        for row in rows:
            out[(str(row["game_id"]), str(row["home_team"]))] = float(row["home_score"])
            out[(str(row["game_id"]), str(row["away_team"]))] = float(row["away_score"])
        return out


def _fit_score(train: list[dict[str, Any]]):
    eligible = [
        r for r in train
        if r.get("actual_points_per_drive") is not None
        and all(r.get(k) is not None for k in SCORING_FEATURES)
    ]
    if len(eligible) < 100:
        return None
    x = np.asarray([[r[k] for k in SCORING_FEATURES] for r in eligible], dtype=float)
    y = np.asarray([r["actual_points_per_drive"] for r in eligible], dtype=float)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales == 0] = 1.0
    z = (x - means) / scales
    design = np.column_stack([np.ones(len(z)), z])
    penalty = np.eye(design.shape[1]) * RIDGE_ALPHA
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    return {
        "means": means, "scales": scales, "beta": beta,
        "training_rows": len(eligible),
    }


def _predict_score(model, row):
    x = np.asarray([row[k] for k in SCORING_FEATURES], dtype=float)
    z = (x - model["means"]) / model["scales"]
    return max(0.0, float(model["beta"][0] + z @ model["beta"][1:]))


def _summary(rows: list[dict[str, Any]], pred_key: str, actual_key: str) -> dict[str, Any]:
    vals = [
        (float(r[pred_key]), float(r[actual_key]))
        for r in rows if r.get(pred_key) is not None and r.get(actual_key) is not None
    ]
    if not vals:
        return {"n": 0}
    errors = [p-a for p,a in vals]
    ae = [abs(e) for e in errors]
    return {
        "n": len(vals),
        "mae": round(sum(ae)/len(ae), 4),
        "rmse": round(math.sqrt(sum(e*e for e in errors)/len(errors)), 4),
        "bias": round(sum(errors)/len(errors), 4),
    }


def _game_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_game: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_game.setdefault(str(row["game_id"]), []).append(row)
    margins = []
    totals = []
    for game_rows in by_game.values():
        if len(game_rows) != 2 or any(r.get("pred_points") is None for r in game_rows):
            continue
        home = next((r for r in game_rows if r["side"] == "home"), None)
        away = next((r for r in game_rows if r["side"] == "away"), None)
        if not home or not away:
            continue
        pred_margin = float(home["pred_points"]) - float(away["pred_points"])
        actual_margin = float(home["actual_points"]) - float(away["actual_points"])
        pred_total = float(home["pred_points"]) + float(away["pred_points"])
        actual_total = float(home["actual_points"]) + float(away["actual_points"])
        margins.append((pred_margin, actual_margin))
        totals.append((pred_total, actual_total))

    def pair_summary(vals):
        if not vals:
            return {"n": 0}
        e = [p-a for p,a in vals]
        ae = [abs(x) for x in e]
        return {
            "n": len(vals),
            "mae": round(sum(ae)/len(ae), 4),
            "rmse": round(math.sqrt(sum(x*x for x in e)/len(e)), 4),
            "bias": round(sum(e)/len(e), 4),
        }
    return {
        "margin": pair_summary(margins),
        "total": pair_summary(totals),
    }


def _core_oof_rows(repository: NFLRepository, start_season: int, end_season: int) -> list[dict[str, Any]]:
    rows = build_rows(repository, start_season=start_season, end_season=end_season)
    score_map = _score_map(repository, start_season, end_season)
    seasons = sorted({int(r["season"]) for r in rows})
    out: list[dict[str, Any]] = []

    for season in seasons:
        train = [r for r in rows if int(r["season"]) < season]
        test = [dict(r) for r in rows if int(r["season"]) == season]
        if len(train) < 100 or not test:
            continue

        drive_model = _fit_drive(train, DRIVE_FEATURES)
        plays_model = _fit_plays(train, PLAYS_FEATURES)
        pass_model = _fit_efficiency(train, PASS_FEATURES, "actual_pass_epa_per_play")
        if not all((drive_model, plays_model, pass_model)):
            continue

        for r in test:
            r["actual_points"] = score_map.get((str(r["game_id"]), str(r["team"])))
            r["actual_points_per_drive"] = (
                float(r["actual_points"]) / float(r["actual_drives"])
                if r.get("actual_points") is not None and float(r["actual_drives"]) > 0
                else None
            )
            r["pred_drives"] = _predict_drive(drive_model, r)
            r["pred_plays_per_drive"] = _predict_plays(plays_model, r)
            r["pred_total_plays"] = r["pred_drives"] * r["pred_plays_per_drive"]

            # Frozen pass-rate choice: transparent offense/defense blend.
            r["pred_pass_rate"] = (
                float(r["team_neutral_pass_rate"])
                + float(r["opponent_neutral_pass_rate"])
            ) / 2.0
            r["pred_pass_epa"] = _predict_efficiency(pass_model, r)

            # Frozen rush choice: simple offense/defense matchup blend.
            r["pred_rush_epa"] = (
                float(r["team_rush_epa_per_play"])
                + float(r["opponent_rush_epa_allowed_per_play"])
            ) / 2.0
            r["pred_combined_epa_per_play"] = (
                r["pred_pass_rate"] * r["pred_pass_epa"]
                + (1.0-r["pred_pass_rate"]) * r["pred_rush_epa"]
            )
            r["pred_combined_epa"] = (
                r["pred_total_plays"] * r["pred_combined_epa_per_play"]
            )
            out.append(r)
    return out


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    oof = _core_oof_rows(repository, start_season, end_season)
    seasons = sorted({int(r["season"]) for r in oof})
    folds = []
    pooled = []

    for season in seasons:
        train = [r for r in oof if int(r["season"]) < season]
        test = [dict(r) for r in oof if int(r["season"]) == season]
        model = _fit_score(train)
        if model is None or not test:
            continue

        league_ppd = (
            sum(float(r["actual_points_per_drive"]) for r in train
                if r.get("actual_points_per_drive") is not None)
            / sum(1 for r in train if r.get("actual_points_per_drive") is not None)
        )
        for r in test:
            r["pred_ppd_league"] = league_ppd
            r["pred_points_league"] = league_ppd * r["pred_drives"]
            r["pred_points_per_drive"] = _predict_score(model, r)
            r["pred_points"] = r["pred_points_per_drive"] * r["pred_drives"]

        pooled.extend(test)
        folds.append({
            "season": season,
            "train_oof_rows": len(train),
            "test_rows": len(test),
            "points": {
                "league_ppd_baseline": _summary(test, "pred_points_league", "actual_points"),
                "scoring_bridge": _summary(test, "pred_points", "actual_points"),
            },
            "points_per_drive": {
                "league": _summary(test, "pred_ppd_league", "actual_points_per_drive"),
                "scoring_bridge": _summary(test, "pred_points_per_drive", "actual_points_per_drive"),
            },
            "games": _game_metrics(test),
            "score_model_training_rows": model["training_rows"],
        })

    return {
        "version": MODEL_VERSION,
        "state_season_decay": STATE_SEASON_DECAY,
        "market_used": False,
        "stacking_policy": (
            "All first-stage core predictions are season-out-of-fold. "
            "Scoring model for season S trains only on prior seasons' OOF predictions."
        ),
        "core_choices": {
            "drives": "football-only ridge",
            "plays_per_drive": "football-only ridge",
            "pass_rate": "offense/defense blend",
            "pass_epa_per_play": "football-only ridge",
            "rush_epa_per_play": "offense/defense blend",
        },
        "scoring_features": list(SCORING_FEATURES),
        "walk_forward": folds,
        "pooled": {
            "points": {
                "league_ppd_baseline": _summary(pooled, "pred_points_league", "actual_points"),
                "scoring_bridge": _summary(pooled, "pred_points", "actual_points"),
            },
            "points_per_drive": {
                "league": _summary(pooled, "pred_ppd_league", "actual_points_per_drive"),
                "scoring_bridge": _summary(pooled, "pred_points_per_drive", "actual_points_per_drive"),
            },
            "games": _game_metrics(pooled),
        },
    }
