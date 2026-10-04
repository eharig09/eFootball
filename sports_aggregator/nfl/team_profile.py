"""A team's opponent-adjusted profile, where its points come from, and how it fares against different styles.

Everything is built from team_styles ratings (offence and defence rated separately, adjusted for the opposition
faced, in the Margin Power construction) for the season shown, including every game played so far.

  adjusted   raw season average beside the schedule-adjusted rating, with league ranks, for both sides of the ball;
             the gap between them is what the schedule did to the raw number
  paths      points per game as drives x plays per drive x points per play, each against the league average and
             each opponent-adjusted; the difference from the league is shared out across the three in proportion to
             their log gaps, so the contributions add up to the whole gap
  opponents  the team's results split by the style of the defences it attacked and the offences it faced (terciles on
             the same ratings), with the sample size beside every row. Small by nature: ~17 games in 3 groups.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.nfl import team_styles
from sports_aggregator.nfl.repository import NFLRepository

# key, label, format, higher is better for the side described, quality metric
ADJUSTED_ROWS = (
    ("points", "Points / game", "f1", True),
    ("pass_epa", "Pass EPA / play", "signed2", True),
    ("rush_epa", "Rush EPA / play", "signed2", True),
    ("explosive", "Explosive play rate", "rate", True),
    ("pass_rate", "Neutral pass rate", "rate", None),
    ("pace", "Seconds / play", "f1", None),
)
#: rank 1 = best for quality metrics (highest on offence, lowest allowed on defence); for the two style metrics,
#: 1 = most pass-heavy / fastest, and on defence the most passing / fastest pace it forces
_OFF_DESC = {"points": True, "pass_epa": True, "rush_epa": True, "explosive": True, "pass_rate": True, "pace": False}
_DEF_DESC = {"points": False, "pass_epa": False, "rush_epa": False, "explosive": False, "pass_rate": True, "pace": False}
PATH_ROWS = (
    ("drives", "Drives / game", "f1"),
    ("plays_per_drive", "Plays / drive", "f2"),
    ("points_per_play", "Points / play", "f2"),
)
#: opponent split groups: (side, label, classification key, ordered buckets)
OFFENCE_SPLITS = (
    ("vulnerability", "Defence weakness", ("Pass-vulnerable", "Balanced", "Run-vulnerable")),
    ("big_plays", "Defence big plays", ("Big-play prone", "Average", "Contains")),
)
DEFENCE_SPLITS = (
    ("approach", "Offence approach", ("Pass-heavy", "Balanced", "Run-heavy")),
    ("tempo", "Offence tempo", ("Up-tempo", "Average tempo", "Slow")),
)


def _raw(rows: list[dict[str, Any]], metric: str) -> float | None:
    total = weight = 0.0
    for r in rows:
        value = team_styles._value(r, metric)
        if value is None:
            continue
        total += value[0] * value[1]
        weight += value[1]
    return total / weight if weight else None


def _rank(values: dict[str, float], team: str, descending: bool) -> int:
    return sorted(values, key=lambda t: values[t], reverse=descending).index(team) + 1


def adjusted_table(snap: dict[str, dict[str, Any]], observations: list[dict[str, Any]], team: str) -> list[dict[str, Any]]:
    mine = [r for r in observations if r["team"] == team]
    faced = [r for r in observations if r["opp"] == team]       # what opposing offences did against this team
    rows = []
    for key, label, fmt, high_is_good in ADJUSTED_ROWS:
        rating = snap[key]
        mu = rating["mu"]
        off_adj, def_adj = mu + rating["off"][team], mu + rating["def"][team]
        off_raw, def_raw = _raw(mine, key), _raw(faced, key)
        rows.append({
            "key": key, "label": label, "format": fmt, "quality": high_is_good is not None,
            "off_raw": off_raw, "off_adj": off_adj, "off_rank": _rank(rating["off"], team, _OFF_DESC[key]),
            "off_schedule": None if off_raw is None else off_adj - off_raw,
            "def_raw": def_raw, "def_adj": def_adj, "def_rank": _rank(rating["def"], team, _DEF_DESC[key]),
            "def_schedule": None if def_raw is None else def_adj - def_raw,
            "league": mu,
            "off_tone": _tone(off_adj - mu, True) if high_is_good else "neutral",
            "def_tone": _tone(def_adj - mu, False) if high_is_good else "neutral",
        })
    return rows


def _tone(delta: float, higher_is_good: bool) -> str:
    if abs(delta) < 1e-9:
        return "neutral"
    return "good" if (delta > 0) == higher_is_good else "poor"


def _product(values: dict[str, float]) -> float:
    return values["drives"] * values["plays_per_drive"] * values["points_per_play"]


def paths_table(snap: dict[str, dict[str, Any]], team: str) -> dict[str, Any]:
    """Offence and defence: three-factor split of points per game and each factor's share of the gap to the league."""
    out: dict[str, Any] = {}
    for side in ("off", "def"):
        league = {k: snap[k]["mu"] for k, _, _ in PATH_ROWS}
        mine = {k: snap[k]["mu"] + snap[k][side][team] for k, _, _ in PATH_ROWS}
        gap = _product(mine) - _product(league)
        logs = {k: math.log(mine[k] / league[k]) if mine[k] > 0 and league[k] > 0 else 0.0 for k in mine}
        spread = sum(logs.values())
        rows = []
        for key, label, fmt in PATH_ROWS:
            rows.append({
                "key": key, "label": label, "format": fmt, "team": mine[key], "league": league[key],
                "pct": mine[key] / league[key] - 1.0 if league[key] else None,
                "points": gap * logs[key] / spread if abs(spread) > 1e-9 else 0.0,
                "rank": _rank(snap[key][side], team, side == "off"),
            })
        out[side] = {"rows": rows, "estimate": _product(mine), "league_estimate": _product(league), "gap": gap,
                     "rated_points": snap["points"]["mu"] + snap["points"][side][team]}
    return out


def opponent_splits(snap: dict[str, dict[str, Any]], observations: list[dict[str, Any]], team: str,
                    labels: dict[str, dict[str, str]]) -> dict[str, Any]:
    """The team's games grouped by the style of the opposition, raw and against what its rating predicted."""
    by_game: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in observations:
        by_game[r["game_id"]][r["team"]] = r
    mine = [(by_game[r["game_id"]], r) for r in observations if r["team"] == team and len(by_game[r["game_id"]]) == 2]

    def group(groups, side: str) -> list[dict[str, Any]]:
        out = []
        for attribute, title, buckets in groups:
            cells = []
            for bucket in buckets:
                picked = []
                for pair, own in mine:
                    opp = labels.get(own["opp"])
                    if opp and opp[attribute] == bucket:
                        picked.append((own, pair[own["opp"]]))
                # a side's results: its own offence rows for the offence table, the opponent's offence rows for defence
                subject = [own if side == "off" else theirs for own, theirs in picked]
                expected = []
                for own, theirs in picked:
                    attacker, defender = (own, theirs) if side == "off" else (theirs, own)
                    expected.append(snap["points"]["mu"] + snap["points"]["off"][attacker["team"]]
                                    + snap["points"]["def"][defender["team"]] + snap["points"]["home"] * attacker["home_col"])
                points = _raw(subject, "points")
                cells.append({
                    "bucket": bucket, "games": len(subject),
                    "points": points, "pass_epa": _raw(subject, "pass_epa"), "rush_epa": _raw(subject, "rush_epa"),
                    "explosive": _raw(subject, "explosive"),
                    "vs_rating": (points - sum(expected) / len(expected)) if subject and points is not None else None,
                })
            out.append({"title": title, "cells": cells})
        return out

    return {"offence": group(OFFENCE_SPLITS, "off"), "defence": group(DEFENCE_SPLITS, "def")}


def packet(repository: NFLRepository, season: int, team: str) -> dict[str, Any]:
    """Panel data for one team-season, or {"available": False, "reason": ...}."""
    snap = team_styles.snapshot_for(repository, int(season), 99)
    observations = team_styles.load_observations(repository, int(season), int(season))
    if not snap or not snap["points"]["off"] or team not in snap["points"]["off"]:
        return {"available": False, "reason": "Style ratings need play-by-play efficiency rows for this team."}
    games = sum(1 for r in observations if r["team"] == team)
    if not games:
        return {"available": False, "reason": f"No completed {season} games with play-by-play rows yet."}
    labels = team_styles.classify(snap)
    return {
        "available": True, "season": int(season), "team": team, "games": games, "settled": games >= 4,
        "labels": labels.get(team, {}),
        "adjusted": adjusted_table(snap, observations, team),
        "paths": paths_table(snap, team),
        "splits": opponent_splits(snap, observations, team, labels),
    }
