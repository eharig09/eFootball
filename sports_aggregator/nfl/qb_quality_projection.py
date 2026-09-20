"""Leak-safe NFL QB-quality adjustment experiment.

Builds pregame quarterback state from qb_pass_profiles only from prior games,
with whole-week batching and 0.25 offseason decay to mirror the team-state
architecture. Tests whether QB-specific information improves team passing
EPA/play beyond the recency-weighted team/opponent efficiency model.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any
import numpy as np

from sports_aggregator.nfl.drive_projection import build_rows, STATE_SEASON_DECAY
from sports_aggregator.nfl.efficiency_projection import PASS_FEATURES
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-qb-quality-v1"
RIDGE_ALPHA = 8.0
MIN_QB_ATTEMPTS = 30

QB_FEATURES = (
    "qb_epa_per_attempt",
    "qb_cpoe",
    "qb_yards_per_attempt",
    "qb_td_rate",
    "qb_int_rate",
    "qb_deep_attempt_rate",
)
BASE_FEATURES = PASS_FEATURES
QB_MODEL_FEATURES = PASS_FEATURES + QB_FEATURES


def _qb_games(repository: NFLRepository, start_season: int, end_season: int) -> list[dict[str, Any]]:
    repository.initialize()
    with repository._connect() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT season,week,game_id,offense_team,defense_team,
                      passer_player_id,passer_name,
                      SUM(attempts) attempts,
                      SUM(completions) completions,
                      SUM(passing_yards) passing_yards,
                      SUM(total_epa) total_epa,
                      SUM(touchdowns) touchdowns,
                      SUM(interceptions) interceptions,
                      SUM(cpoe_total) cpoe_total,
                      SUM(cpoe_plays) cpoe_plays,
                      SUM(CASE WHEN depth_bucket='deep' THEN attempts ELSE 0 END) deep_attempts
               FROM qb_pass_profiles
               WHERE season BETWEEN ? AND ?
               GROUP BY season,week,game_id,offense_team,defense_team,
                        passer_player_id,passer_name
               ORDER BY season,week,game_id,attempts DESC""",
            (int(start_season), int(end_season)),
        )]
    # Primary passer = most attempts for that offense in the game.
    primary: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["game_id"]), str(row["offense_team"]))
        current = primary.get(key)
        if current is None or int(row["attempts"] or 0) > int(current["attempts"] or 0):
            primary[key] = row
    return list(primary.values())


def _state_snapshot(records: list[dict[str, Any]], target_season: int) -> dict[str, float] | None:
    if not records:
        return None
    weighted = []
    for r in records:
        weight = STATE_SEASON_DECAY ** max(0, int(target_season) - int(r["season"]))
        weighted.append((weight, r))
    attempts = sum(w * float(r["attempts"] or 0) for w, r in weighted)
    if attempts < MIN_QB_ATTEMPTS:
        return None
    epa = sum(w * float(r["total_epa"] or 0) for w, r in weighted)
    yards = sum(w * float(r["passing_yards"] or 0) for w, r in weighted)
    tds = sum(w * float(r["touchdowns"] or 0) for w, r in weighted)
    ints = sum(w * float(r["interceptions"] or 0) for w, r in weighted)
    deep = sum(w * float(r["deep_attempts"] or 0) for w, r in weighted)
    cpoe_total = sum(w * float(r["cpoe_total"] or 0) for w, r in weighted)
    cpoe_plays = sum(w * float(r["cpoe_plays"] or 0) for w, r in weighted)
    return {
        "qb_epa_per_attempt": epa / attempts,
        "qb_cpoe": cpoe_total / cpoe_plays if cpoe_plays else 0.0,
        "qb_yards_per_attempt": yards / attempts,
        "qb_td_rate": tds / attempts,
        "qb_int_rate": ints / attempts,
        "qb_deep_attempt_rate": deep / attempts,
        "qb_effective_attempts": attempts,
    }


def _pregame_qb_states(repository: NFLRepository, start_season: int, end_season: int):
    games = _qb_games(repository, start_season, end_season)
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in games:
        by_week[(int(row["season"]), int(row["week"]))].append(row)

    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    states: dict[tuple[str, str], dict[str, Any]] = {}
    for season_week in sorted(by_week):
        season, _ = season_week
        current = by_week[season_week]
        for row in current:
            qb = str(row["passer_player_id"])
            snap = _state_snapshot(history[qb], season)
            if snap:
                states[(str(row["game_id"]), str(row["offense_team"]))] = {
                    **snap,
                    "qb_player_id": qb,
                    "qb_name": str(row["passer_name"]),
                }
        # Whole-week update after every state is captured.
        for row in current:
            history[str(row["passer_player_id"])].append(row)
    return states


def _fit(train: list[dict[str, Any]], features: tuple[str, ...]):
    eligible = [
        r for r in train
        if r.get("actual_pass_epa_per_play") is not None
        and all(r.get(k) is not None for k in features)
    ]
    if len(eligible) < 100:
        return None
    x = np.asarray([[r[k] for k in features] for r in eligible], dtype=float)
    y = np.asarray([r["actual_pass_epa_per_play"] for r in eligible], dtype=float)
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
    if model is None or any(row.get(k) is None for k in model["features"]):
        return None
    x = np.asarray([row[k] for k in model["features"]], dtype=float)
    z = (x - model["means"]) / model["scales"]
    return float(model["beta"][0] + z @ model["beta"][1:])


def _summary(rows, key):
    vals = [(float(r[key]), float(r["actual_pass_epa_per_play"])) for r in rows
            if r.get(key) is not None and r.get("actual_pass_epa_per_play") is not None]
    if not vals:
        return {"n": 0}
    err = [p-a for p,a in vals]
    ae = [abs(e) for e in err]
    return {
        "n": len(vals),
        "mae": round(sum(ae)/len(ae), 4),
        "rmse": round(math.sqrt(sum(e*e for e in err)/len(err)), 4),
        "bias": round(sum(err)/len(err), 4),
        "within_0_10": round(sum(e <= .10 for e in ae)/len(ae), 4),
        "within_0_20": round(sum(e <= .20 for e in ae)/len(ae), 4),
    }


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    rows = build_rows(repository, start_season=start_season, end_season=end_season)
    qb_states = _pregame_qb_states(repository, start_season, end_season)
    for row in rows:
        qb = qb_states.get((str(row["game_id"]), str(row["team"])))
        if qb:
            row.update(qb)

    seasons = sorted({int(r["season"]) for r in rows})
    pooled = []
    folds = []
    for season in seasons:
        train = [r for r in rows if int(r["season"]) < season]
        test = [dict(r) for r in rows if int(r["season"]) == season]
        base = _fit(train, BASE_FEATURES)
        qb_model = _fit(train, QB_MODEL_FEATURES)
        if base is None or qb_model is None or not test:
            continue
        for r in test:
            r["pred_base"] = _predict(base, r)
            r["pred_qb"] = _predict(qb_model, r)
        common = [r for r in test if r.get("pred_base") is not None and r.get("pred_qb") is not None]
        pooled.extend(common)
        folds.append({
            "season": season,
            "test_rows": len(test),
            "common_qb_rows": len(common),
            "qb_coverage": round(len(common)/len(test), 4) if test else 0.0,
            "base": _summary(common, "pred_base"),
            "plus_qb": _summary(common, "pred_qb"),
            "training_rows": {
                "base": base["n"],
                "plus_qb": qb_model["n"],
            },
        })

    return {
        "version": MODEL_VERSION,
        "state_season_decay": STATE_SEASON_DECAY,
        "minimum_qb_effective_attempts": MIN_QB_ATTEMPTS,
        "qb_features": list(QB_FEATURES),
        "base_features": list(BASE_FEATURES),
        "leakage_policy": "QB prior games only; whole-week batch snapshots; season-decayed history",
        "walk_forward": folds,
        "pooled_common_sample": {
            "base": _summary(pooled, "pred_base"),
            "plus_qb": _summary(pooled, "pred_qb"),
            "n": len(pooled),
        },
    }
