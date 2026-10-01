"""Offensive tendencies by down and distance: how often a team passes in each situation, against the league.

Built from stored scrimmage plays (`nfl_plays`), regular season, excluding kneels and spikes. Each of the twelve
situations is a down and a distance band (short / medium / long); a cell reports the dominant call, its rate, the
sample, and how the rate compares with the league in the same situation.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository

MINIMUM_PLAYS = 10

#: (down, band) -> label. First down splits goal-to-go / up to ten / eleven-plus; later downs split by yards to go.
SITUATIONS = (
    (1, "short", "1st & goal"), (1, "medium", "1st & 10"), (1, "long", "1st & 11+"),
    (2, "short", "2nd & 1–3"), (2, "medium", "2nd & 4–6"), (2, "long", "2nd & 7+"),
    (3, "short", "3rd & 1–3"), (3, "medium", "3rd & 4–6"), (3, "long", "3rd & 7+"),
    (4, "short", "4th & 1–2"), (4, "medium", "4th & 3–5"), (4, "long", "4th & 6+"),
)
BANDS = ("short", "medium", "long")
DOWNS = (1, 2, 3, 4)
_QUERY = """
SELECT posteam team, down, is_pass, ydstogo, goal_to_go, epa, success FROM nfl_plays
WHERE season=? AND season_type='REG' AND down BETWEEN 1 AND 4 AND (is_pass=1 OR is_rush=1)
  AND COALESCE(play_type,'') NOT IN ('qb_kneel','qb_spike') {week_filter}
"""


def band(down: int, ydstogo: int | None, goal_to_go: int | None) -> str:
    distance = ydstogo if ydstogo is not None else 10
    if down == 1:
        return "short" if goal_to_go else ("medium" if distance <= 10 else "long")
    if down in (2, 3):
        return "short" if distance <= 3 else ("medium" if distance <= 6 else "long")
    return "short" if distance <= 2 else ("medium" if distance <= 5 else "long")


def _tally(repository: NFLRepository, season: int, before_week: int | None) -> dict[str, Any]:
    repository.initialize()
    week_filter = "AND week<?" if before_week is not None else ""
    parameters: list[Any] = [int(season)] + ([int(before_week)] if before_week is not None else [])
    cells: dict[tuple[str, int, str], list[float]] = defaultdict(lambda: [0, 0, 0.0, 0])   # plays, passes, epa, successes
    with closing(repository._connect()) as connection:
        for team, down, is_pass, ydstogo, goal_to_go, epa, success in connection.execute(
                _QUERY.format(week_filter=week_filter), parameters):
            cell = cells[(team, down, band(down, ydstogo, goal_to_go))]
            cell[0] += 1
            cell[1] += is_pass
            cell[2] += epa or 0.0
            cell[3] += success or 0
    return cells


def league_tendencies(repository: NFLRepository, season: int, *, before_week: int | None = None) -> dict[str, Any]:
    """{'teams': {team: {(down, band): cell}}, 'league': {(down, band): pass_rate}} for a season."""
    return repository.memo(("situational_tendencies", int(season), before_week),
                           lambda: _league(repository, season, before_week))


def _league(repository: NFLRepository, season: int, before_week: int | None) -> dict[str, Any]:
    cells = _tally(repository, season, before_week)
    totals: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
    teams: dict[str, dict[tuple[int, str], list[float]]] = defaultdict(dict)
    for (team, down, situation_band), values in cells.items():
        teams[team][(down, situation_band)] = values
        totals[(down, situation_band)][0] += values[0]
        totals[(down, situation_band)][1] += values[1]
    return {"teams": dict(teams),
            "league": {key: (passes / plays if plays else None) for key, (plays, passes) in totals.items()}}


def team_tendencies(repository: NFLRepository, season: int, team: str, *,
                    before_week: int | None = None) -> dict[str, Any] | None:
    """The 4x3 grid for one team, or None when the team has no stored plays this season."""
    data = league_tendencies(repository, season, before_week=before_week)
    mine = data["teams"].get(team)
    if not mine:
        return None
    grid: dict[tuple[int, str], dict[str, Any]] = {}
    for down, situation_band, label in SITUATIONS:
        values = mine.get((down, situation_band))
        league_rate = data["league"].get((down, situation_band))
        plays = int(values[0]) if values else 0
        cell: dict[str, Any] = {"label": label, "down": down, "band": situation_band, "plays": plays,
                                "status": "none" if not plays else ("small" if plays < MINIMUM_PLAYS else "ok"),
                                "league_pass_rate": league_rate}
        if values and plays:
            pass_rate = values[1] / plays
            cell.update({"pass_rate": pass_rate, "run_rate": 1 - pass_rate, "epa": values[2] / plays,
                         "success": values[3] / plays,
                         "call": "PASS" if pass_rate >= .5 else "RUN",
                         "call_rate": pass_rate if pass_rate >= .5 else 1 - pass_rate,
                         "vs_league": (pass_rate - league_rate) if league_rate is not None else None})
        grid[(down, situation_band)] = cell
    return {"team": team, "season": int(season),
            "plays": sum(int(values[0]) for values in mine.values()),
            "rows": [{"down": down, "cells": [grid[(down, situation_band)] for situation_band in BANDS]} for down in DOWNS]}


def compare(away: dict[str, Any] | None, home: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One row per situation with both teams' cells side by side, for the matchup summary."""
    rows = []
    for index, (down, situation_band, label) in enumerate(SITUATIONS):
        def cell(profile):
            return profile["rows"][down - 1]["cells"][BANDS.index(situation_band)] if profile else None
        rows.append({"label": label, "away": cell(away), "home": cell(home)})
    return rows
