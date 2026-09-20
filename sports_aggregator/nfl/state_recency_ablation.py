"""Pregame NFL team-state recency ablation.

Tests offseason regression directly in the underlying drive/plays snapshots.
Current-season games receive weight 1.0; each season back is multiplied by an
offseason decay. Whole-week batching prevents same-week leakage.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.nfl.drive_projection import _raw_games, MIN_PRIOR_GAMES
from sports_aggregator.nfl.repository import NFLRepository

DECAYS = (1.0, 0.75, 0.5, 0.25)


def _snapshot(history: list[dict[str, Any]], season: int, decay: float) -> dict[str, float] | None:
    if len(history) < MIN_PRIOR_GAMES:
        return None
    weights = [decay ** max(0, int(season) - int(r["season"])) for r in history]
    sw = sum(weights)
    if sw <= 0:
        return None

    def wsum(key):
        return sum(w * float(r[key]) for w, r in zip(weights, history))

    drives_for = wsum("drives_for")
    drives_against = wsum("drives_against")
    plays = wsum("plays")
    opponent_plays = wsum("opponent_plays")
    return {
        "drives_for": drives_for / sw,
        "drives_allowed": drives_against / sw,
        "plays_per_drive": plays / drives_for if drives_for else 0.0,
        "plays_per_drive_allowed": opponent_plays / drives_against if drives_against else 0.0,
    }


def _summary(rows, key, target):
    vals = [(float(r[key]), float(r[target])) for r in rows if r.get(key) is not None]
    if not vals:
        return {"n": 0}
    err = [p-a for p,a in vals]
    ae = [abs(e) for e in err]
    return {
        "n": len(vals),
        "mae": round(sum(ae)/len(ae), 4),
        "rmse": round(math.sqrt(sum(e*e for e in err)/len(err)), 4),
        "bias": round(sum(err)/len(err), 4),
    }


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    games = _raw_games(repository, start_season, end_season)
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for game in games:
        by_week[(int(game["season"]), int(game["week"]))].append(game)

    rows: list[dict[str, Any]] = []
    for season_week in sorted(by_week):
        season, _ = season_week
        current = by_week[season_week]
        for game in current:
            home, away = str(game["home_team"]), str(game["away_team"])
            for side, team, opponent in (("home", home, away), ("away", away, home)):
                other = "away" if side == "home" else "home"
                row = {
                    "season": season,
                    "game_id": game["game_id"],
                    "team": team,
                    "actual_drives": float(game[f"{side}_drives"]),
                    "actual_plays_per_drive": (
                        float(game[f"{side}_plays"]) / float(game[f"{side}_drives"])
                        if float(game[f"{side}_drives"]) else None
                    ),
                }
                for decay in DECAYS:
                    own = _snapshot(histories[team], season, decay)
                    opp = _snapshot(histories[opponent], season, decay)
                    label = str(decay).replace(".", "_")
                    if own and opp:
                        row[f"drive_{label}"] = (
                            own["drives_for"] + opp["drives_allowed"]
                        ) / 2.0
                        row[f"plays_{label}"] = (
                            own["plays_per_drive"] + opp["plays_per_drive_allowed"]
                        ) / 2.0
                rows.append(row)

        for game in current:
            home, away = str(game["home_team"]), str(game["away_team"])
            for side, team in (("home", home), ("away", away)):
                other = "away" if side == "home" else "home"
                histories[team].append({
                    "season": int(game["season"]),
                    "drives_for": float(game[f"{side}_drives"]),
                    "drives_against": float(game[f"{other}_drives"]),
                    "plays": float(game[f"{side}_plays"]),
                    "opponent_plays": float(game[f"{other}_plays"]),
                })

    evaluated = [r for r in rows if int(r["season"]) > int(start_season)]
    pooled = {}
    for decay in DECAYS:
        label = str(decay).replace(".", "_")
        pooled[str(decay)] = {
            "drives": _summary(evaluated, f"drive_{label}", "actual_drives"),
            "plays_per_drive": _summary(evaluated, f"plays_{label}", "actual_plays_per_drive"),
        }

    by_season = []
    for season in sorted({int(r["season"]) for r in evaluated}):
        test = [r for r in evaluated if int(r["season"]) == season]
        by_season.append({
            "season": season,
            "variants": {
                str(decay): {
                    "drives": _summary(test, f"drive_{str(decay).replace('.', '_')}", "actual_drives"),
                    "plays_per_drive": _summary(test, f"plays_{str(decay).replace('.', '_')}", "actual_plays_per_drive"),
                }
                for decay in DECAYS
            },
        })

    return {
        "version": "nfl-team-state-recency-v1",
        "offseason_decays": list(DECAYS),
        "interpretation": "1.0=no offseason regression; lower values discount each prior season more heavily",
        "leakage_policy": "pregame snapshots; entire week updated only after predictions",
        "pooled": pooled,
        "walk_forward": by_season,
    }
