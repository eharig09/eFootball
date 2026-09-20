"""NFL Football Lab independent total/margin calibration experiment.

Builds on the leak-safe end-to-end scoring bridge. For each season:
1) generate out-of-fold upstream scoring inputs,
2) fit prior-season scoring bridge,
3) derive raw home/away points,
4) fit independent total and margin calibrators on PRIOR seasons only,
5) reconstruct final scores from (T +/- M) / 2.

Vegas is not used in fitting. Market lines remain available later only as a
benchmark/disagreement layer.
"""
from __future__ import annotations

import math
from typing import Any
import numpy as np

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.scoring_bridge import (
    _core_oof_rows,
    _fit_score,
    _predict_score,
)

MODEL_VERSION = "nfl-total-margin-calibration-v1"
RIDGE_ALPHA = 8.0

TOTAL_FEATURES = (
    "raw_total",
    "sum_pred_drives",
    "sum_pred_total_plays",
    "sum_pred_combined_epa",
)
MARGIN_FEATURES = (
    "raw_margin",
    "diff_pred_drives",
    "diff_pred_plays_per_drive",
    "diff_pred_pass_rate",
    "diff_pred_pass_epa",
    "diff_pred_rush_epa",
    "diff_pred_combined_epa",
)


def _fit_ridge(rows: list[dict[str, Any]], features: tuple[str, ...], target: str):
    eligible = [
        r for r in rows
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
    return {"features": features, "means": means, "scales": scales, "beta": beta, "n": len(eligible)}


def _predict(model, row):
    x = np.asarray([row[k] for k in model["features"]], dtype=float)
    z = (x-model["means"]) / model["scales"]
    return float(model["beta"][0] + z @ model["beta"][1:])


def _pair_summary(values: list[tuple[float, float]]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    errors = [p-a for p,a in values]
    ae = [abs(e) for e in errors]
    return {
        "n": len(values),
        "mae": round(sum(ae)/len(ae), 4),
        "rmse": round(math.sqrt(sum(e*e for e in errors)/len(errors)), 4),
        "bias": round(sum(errors)/len(errors), 4),
    }


def _game_rows(repository: NFLRepository, *, start_season: int, end_season: int) -> list[dict[str, Any]]:
    oof = _core_oof_rows(repository, start_season, end_season)
    seasons = sorted({int(r["season"]) for r in oof})
    scored_team_rows: list[dict[str, Any]] = []

    # First-stage scoring bridge remains strict walk-forward.
    for season in seasons:
        train = [r for r in oof if int(r["season"]) < season]
        test = [dict(r) for r in oof if int(r["season"]) == season]
        score_model = _fit_score(train)
        if score_model is None:
            continue
        for r in test:
            r["pred_points_per_drive"] = _predict_score(score_model, r)
            r["raw_pred_points"] = r["pred_points_per_drive"] * r["pred_drives"]
            scored_team_rows.append(r)

    by_game: dict[str, list[dict[str, Any]]] = {}
    for row in scored_team_rows:
        by_game.setdefault(str(row["game_id"]), []).append(row)

    games = []
    for game_rows in by_game.values():
        if len(game_rows) != 2:
            continue
        home = next((r for r in game_rows if r["side"]=="home"), None)
        away = next((r for r in game_rows if r["side"]=="away"), None)
        if not home or not away:
            continue
        hp = float(home["raw_pred_points"])
        ap = float(away["raw_pred_points"])
        ha = float(home["actual_points"])
        aa = float(away["actual_points"])
        games.append({
            "game_id": home["game_id"],
            "season": int(home["season"]),
            "raw_home_points": hp,
            "raw_away_points": ap,
            "raw_total": hp+ap,
            "raw_margin": hp-ap,
            "actual_home_points": ha,
            "actual_away_points": aa,
            "actual_total": ha+aa,
            "actual_margin": ha-aa,
            "sum_pred_drives": float(home["pred_drives"])+float(away["pred_drives"]),
            "sum_pred_total_plays": float(home["pred_total_plays"])+float(away["pred_total_plays"]),
            "sum_pred_combined_epa": float(home["pred_combined_epa"])+float(away["pred_combined_epa"]),
            "diff_pred_drives": float(home["pred_drives"])-float(away["pred_drives"]),
            "diff_pred_plays_per_drive": float(home["pred_plays_per_drive"])-float(away["pred_plays_per_drive"]),
            "diff_pred_pass_rate": float(home["pred_pass_rate"])-float(away["pred_pass_rate"]),
            "diff_pred_pass_epa": float(home["pred_pass_epa"])-float(away["pred_pass_epa"]),
            "diff_pred_rush_epa": float(home["pred_rush_epa"])-float(away["pred_rush_epa"]),
            "diff_pred_combined_epa": float(home["pred_combined_epa"])-float(away["pred_combined_epa"]),
        })
    return games


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    games = _game_rows(repository, start_season=start_season, end_season=end_season)
    seasons = sorted({int(r["season"]) for r in games})
    pooled = []
    folds = []

    for season in seasons:
        train = [r for r in games if int(r["season"]) < season]
        test = [dict(r) for r in games if int(r["season"]) == season]
        total_model = _fit_ridge(train, TOTAL_FEATURES, "actual_total")
        margin_model = _fit_ridge(train, MARGIN_FEATURES, "actual_margin")
        if total_model is None or margin_model is None or not test:
            continue

        for r in test:
            r["cal_total"] = _predict(total_model, r)
            r["cal_margin"] = _predict(margin_model, r)
            r["dual_home"] = (r["cal_total"] + r["cal_margin"])/2.0
            r["dual_away"] = (r["cal_total"] - r["cal_margin"])/2.0

        raw_total = [(float(r["raw_total"]), float(r["actual_total"])) for r in test]
        raw_margin = [(float(r["raw_margin"]), float(r["actual_margin"])) for r in test]
        dual_total = [(float(r["cal_total"]), float(r["actual_total"])) for r in test]
        dual_margin = [(float(r["cal_margin"]), float(r["actual_margin"])) for r in test]
        raw_scores = []
        dual_scores = []
        for r in test:
            raw_scores.extend([
                (float(r["raw_home_points"]), float(r["actual_home_points"])),
                (float(r["raw_away_points"]), float(r["actual_away_points"])),
            ])
            dual_scores.extend([
                (float(r["dual_home"]), float(r["actual_home_points"])),
                (float(r["dual_away"]), float(r["actual_away_points"])),
            ])

        pooled.extend(test)
        folds.append({
            "season": season,
            "train_games": len(train),
            "test_games": len(test),
            "raw": {
                "score": _pair_summary(raw_scores),
                "total": _pair_summary(raw_total),
                "margin": _pair_summary(raw_margin),
            },
            "dual": {
                "score": _pair_summary(dual_scores),
                "total": _pair_summary(dual_total),
                "margin": _pair_summary(dual_margin),
            },
            "training_rows": {
                "total": total_model["n"],
                "margin": margin_model["n"],
            },
        })

    raw_scores=[]; dual_scores=[]; raw_total=[]; dual_total=[]; raw_margin=[]; dual_margin=[]
    for r in pooled:
        raw_scores += [
            (float(r["raw_home_points"]), float(r["actual_home_points"])),
            (float(r["raw_away_points"]), float(r["actual_away_points"])),
        ]
        dual_scores += [
            (float(r["dual_home"]), float(r["actual_home_points"])),
            (float(r["dual_away"]), float(r["actual_away_points"])),
        ]
        raw_total.append((float(r["raw_total"]), float(r["actual_total"])))
        dual_total.append((float(r["cal_total"]), float(r["actual_total"])))
        raw_margin.append((float(r["raw_margin"]), float(r["actual_margin"])))
        dual_margin.append((float(r["cal_margin"]), float(r["actual_margin"])))

    return {
        "version": MODEL_VERSION,
        "market_used": False,
        "architecture": "independent calibrated total + calibrated margin; scores=(T±M)/2",
        "total_features": list(TOTAL_FEATURES),
        "margin_features": list(MARGIN_FEATURES),
        "walk_forward": folds,
        "pooled": {
            "raw": {
                "score": _pair_summary(raw_scores),
                "total": _pair_summary(raw_total),
                "margin": _pair_summary(raw_margin),
            },
            "dual": {
                "score": _pair_summary(dual_scores),
                "total": _pair_summary(dual_total),
                "margin": _pair_summary(dual_margin),
            },
        },
    }
