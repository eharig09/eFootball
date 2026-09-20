"""NFL margin-strength common-sample ablation.

Tests whether NFL-specific strength/context signals improve the independently
calibrated margin beyond the core Football Lab score differential. Every
variant is evaluated on the same games where the full feature stack exists.

No market/odds inputs are used.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any
import numpy as np

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import (
    _game_rows,
    MARGIN_FEATURES,
    RIDGE_ALPHA,
)
from sports_aggregator.nfl.qb_quality_projection import _pregame_qb_states
from sports_aggregator.nfl.drive_projection import STATE_SEASON_DECAY

MODEL_VERSION = "nfl-margin-strength-ablation-v1"

FEATURE_SETS = (
    ("core", MARGIN_FEATURES),
    ("plus_elo", MARGIN_FEATURES + ("elo_diff",)),
    ("plus_recent", MARGIN_FEATURES + ("elo_diff", "recent_margin_diff")),
    ("plus_qb_epa", MARGIN_FEATURES + (
        "elo_diff", "recent_margin_diff", "qb_epa_diff",
    )),
    ("plus_qb_cpoe", MARGIN_FEATURES + (
        "elo_diff", "recent_margin_diff", "qb_epa_diff", "qb_cpoe_diff",
    )),
)


def _fit(rows: list[dict[str, Any]], features: tuple[str, ...]):
    if len(rows) < 100:
        return None
    x = np.asarray([[r[k] for k in features] for r in rows], dtype=float)
    y = np.asarray([r["actual_margin"] for r in rows], dtype=float)
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
    z = (x-model["means"]) / model["scales"]
    return float(model["beta"][0] + z @ model["beta"][1:])


def _summary(rows: list[dict[str, Any]], key: str):
    vals = [(float(r[key]), float(r["actual_margin"])) for r in rows if r.get(key) is not None]
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


def _elo_map(repository: NFLRepository, start_season: int, end_season: int):
    with repository._connect() as connection:
        rows = connection.execute(
            """SELECT game_id,home_pre,away_pre
               FROM nfl_elo_games
               WHERE season BETWEEN ? AND ?""",
            (int(start_season), int(end_season)),
        )
        return {
            str(r["game_id"]): float(r["home_pre"])-float(r["away_pre"])
            for r in rows
        }


def _recent_margin_map(repository: NFLRepository, start_season: int, end_season: int):
    with repository._connect() as connection:
        games = [dict(r) for r in connection.execute(
            """SELECT game_id,season,week,home_team,away_team,home_score,away_score
               FROM games
               WHERE season BETWEEN ? AND ? AND completed=1
                 AND home_score IS NOT NULL AND away_score IS NOT NULL
               ORDER BY season,week,game_date,game_id""",
            (int(start_season), int(end_season)),
        )]

    by_week: dict[tuple[int,int], list[dict[str,Any]]] = defaultdict(list)
    for g in games:
        by_week[(int(g["season"]), int(g["week"]))].append(g)

    history: dict[str, list[dict[str,Any]]] = defaultdict(list)
    out: dict[tuple[str,str], float] = {}

    def snapshot(team: str, season: int):
        records = history.get(team, [])
        if not records:
            return None
        weighted = []
        for r in records:
            w = STATE_SEASON_DECAY ** max(0, season-int(r["season"]))
            weighted.append((w, float(r["margin"])))
        sw = sum(w for w,_ in weighted)
        return sum(w*m for w,m in weighted)/sw if sw else None

    for (season, _week) in sorted(by_week):
        current = by_week[(season, _week)]
        for g in current:
            h = str(g["home_team"]); a = str(g["away_team"])
            hs = snapshot(h, season); as_ = snapshot(a, season)
            if hs is not None:
                out[(str(g["game_id"]), h)] = hs
            if as_ is not None:
                out[(str(g["game_id"]), a)] = as_
        for g in current:
            h = str(g["home_team"]); a = str(g["away_team"])
            hm = float(g["home_score"])-float(g["away_score"])
            history[h].append({"season": season, "margin": hm})
            history[a].append({"season": season, "margin": -hm})
    return out


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    games = _game_rows(repository, start_season=start_season, end_season=end_season)
    elo = _elo_map(repository, start_season, end_season)
    recent = _recent_margin_map(repository, start_season, end_season)
    qb = _pregame_qb_states(repository, start_season, end_season)

    # Add full-strength features.
    for r in games:
        gid = str(r["game_id"])
        r["elo_diff"] = elo.get(gid)
        # recover teams from raw game rows
    with repository._connect() as connection:
        team_rows = {
            str(x["game_id"]): (str(x["home_team"]), str(x["away_team"]))
            for x in connection.execute(
                """SELECT game_id,home_team,away_team FROM games
                   WHERE season BETWEEN ? AND ?""",
                (int(start_season), int(end_season)),
            )
        }

    for r in games:
        gid = str(r["game_id"])
        teams = team_rows.get(gid)
        if not teams:
            continue
        home, away = teams
        hr = recent.get((gid, home)); ar = recent.get((gid, away))
        r["recent_margin_diff"] = hr-ar if hr is not None and ar is not None else None
        hq = qb.get((gid, home)); aq = qb.get((gid, away))
        r["qb_epa_diff"] = (
            float(hq["qb_epa_per_attempt"])-float(aq["qb_epa_per_attempt"])
            if hq and aq else None
        )
        r["qb_cpoe_diff"] = (
            float(hq["qb_cpoe"])-float(aq["qb_cpoe"])
            if hq and aq else None
        )

    full = FEATURE_SETS[-1][1]
    common = [
        r for r in games
        if all(r.get(k) is not None for k in full)
        and r.get("actual_margin") is not None
    ]
    seasons = sorted({int(r["season"]) for r in common})
    pooled = []
    folds = []

    for season in seasons:
        train = [r for r in common if int(r["season"]) < season]
        test = [dict(r) for r in common if int(r["season"]) == season]
        if len(train) < 100 or not test:
            continue
        models = {}
        for label, features in FEATURE_SETS:
            models[label] = _fit(train, features)
        if any(v is None for v in models.values()):
            continue
        for r in test:
            for label, model in models.items():
                r[f"pred_{label}"] = _predict(model, r)
        pooled.extend(test)
        folds.append({
            "season": season,
            "train_games": len(train),
            "test_games": len(test),
            "models": {
                label: _summary(test, f"pred_{label}")
                for label, _ in FEATURE_SETS
            },
        })

    return {
        "version": MODEL_VERSION,
        "market_used": False,
        "common_sample": True,
        "feature_sets": {label: list(features) for label, features in FEATURE_SETS},
        "raw_games": len(games),
        "common_games": len(common),
        "coverage": round(len(common)/len(games), 4) if games else 0.0,
        "walk_forward": folds,
        "pooled": {
            label: _summary(pooled, f"pred_{label}")
            for label, _ in FEATURE_SETS
        },
    }
