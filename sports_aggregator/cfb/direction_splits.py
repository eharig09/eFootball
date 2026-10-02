"""Where college teams throw and run: left / middle / right splits for offense and defense.

The NFL page splits throws and runs into field side and boundary using the hash mark. College play-by-play has no
hash, so the same panel is built on what it does record -- the direction and depth of each parsed pass
(`cfbd_passing_plays`) and the direction of each run (`cfbd_rushing_plays`), available from 2025. Each side shows its
share against the FBS split (the tick), volume, EPA/play, yards, completion or success rate and aDOT, plus a depth by
direction grid for throws.

The output matches `nfl.enhanced_tables.side_view` so the shared `side_panel` macro renders both.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from typing import Any

from sports_aggregator.cfb.derived_cache import derived
from sports_aggregator.nfl.enhanced_tables import _signed_class, heat

DIRECTIONS = ("left", "middle", "right")
LABELS = {"left": "Left", "middle": "Middle", "right": "Right"}
DEPTHS = (("short", "Short"), ("deep", "Deep"))
#: Fewer charted plays than this on a side is too thin to split three ways; fall back to last season.
MIN_CHARTED = 60
#: A three-way split of fewer plays than this is noise, so the panel says so instead of drawing it.
MIN_SECTION = 20

_PASS = """
SELECT p.{side} AS team, p.pass_direction, COALESCE(p.pass_depth, 'short'), COUNT(*),
       SUM(COALESCE(p.total_yards, 0.0)), SUM(CASE WHEN p.outcome = 'completion' THEN 1 ELSE 0 END),
       SUM(p.air_yards), COUNT(p.air_yards), SUM(e.epa), COUNT(e.epa)
FROM cfbd_passing_plays p
JOIN games g ON g.game_id = p.game_id AND g.season_type = 'regular'
LEFT JOIN cfb_play_epa e ON e.play_id = p.play_id
WHERE p.season = ? AND p.pass_direction IN ('left','middle','right')
GROUP BY p.{side}, p.pass_direction, COALESCE(p.pass_depth, 'short')
"""
_RUSH = """
SELECT p.{side} AS team, p.rush_direction, COUNT(*), SUM(COALESCE(p.rushing_yards, 0.0)),
       SUM(e.epa), COUNT(e.epa), SUM(m.success), COUNT(m.success)
FROM cfbd_rushing_plays p
JOIN games g ON g.game_id = p.game_id AND g.season_type = 'regular'
LEFT JOIN cfb_play_epa e ON e.play_id = p.play_id
LEFT JOIN cfb_play_metrics m ON m.play_id = p.play_id
WHERE p.season = ? AND p.rush_direction IN ('left','middle','right') AND COALESCE(p.is_kneel, 0) = 0
GROUP BY p.{side}, p.rush_direction
"""


def _new() -> dict[str, float]:
    return {"n": 0, "epa": 0.0, "epa_n": 0, "yards": 0.0, "completions": 0, "air": 0.0, "air_n": 0,
            "success": 0.0, "success_n": 0}


def _tally(repository, season: int) -> dict[str, Any]:
    """{'pass'|'run': {'offense'|'defense': {team: {'dir': {direction: cell}, 'matrix': {(depth, dir): cell}}}}}.

    Aggregated by SQLite (a few thousand grouped rows) rather than looped over in Python (~100,000 plays).
    """
    out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
        kind: {side: defaultdict(lambda: {"dir": defaultdict(_new), "matrix": defaultdict(_new)})
               for side in ("offense", "defense")} for kind in ("pass", "run")}
    with closing(repository._connect()) as connection:
        fbs = {row[0] for row in connection.execute("SELECT school FROM teams WHERE classification = 'fbs'")}
        for side in ("offense", "defense"):
            for team, direction, depth, n, yards, completions, air, air_n, epa, epa_n in connection.execute(
                    _PASS.format(side=side), (int(season),)):
                if team not in fbs:
                    continue
                bucket = out["pass"][side][team]
                for cell in (bucket["dir"][direction], bucket["matrix"][(depth, direction)]):
                    cell["n"] += n
                    cell["yards"] += yards or 0.0
                    cell["completions"] += completions or 0
                    cell["air"] += air or 0.0
                    cell["air_n"] += air_n or 0
                    cell["epa"] += epa or 0.0
                    cell["epa_n"] += epa_n or 0
            for team, direction, n, yards, epa, epa_n, success, success_n in connection.execute(
                    _RUSH.format(side=side), (int(season),)):
                if team not in fbs:
                    continue
                cell = out["run"][side][team]["dir"][direction]
                cell["n"] += n
                cell["yards"] += yards or 0.0
                cell["epa"] += epa or 0.0
                cell["epa_n"] += epa_n or 0
                cell["success"] += success or 0.0
                cell["success_n"] += success_n or 0
    return {kind: {side: {team: {"dir": dict(v["dir"]), "matrix": dict(v["matrix"])} for team, v in teams.items()}
                   for side, teams in sides.items()} for kind, sides in out.items()}


def league_splits(repository, season: int) -> dict[str, Any]:
    return derived(repository, "cfb_direction_splits", lambda: _tally(repository, season), int(season))


def _league_shares(teams: dict[str, Any]) -> dict[str, float]:
    totals = defaultdict(float)
    for team in teams.values():
        for direction, cell in team["dir"].items():
            totals[direction] += cell["n"]
    grand = sum(totals.values()) or 1.0
    return {direction: totals[direction] / grand for direction in DIRECTIONS}


def _ratio(left: float, right: float) -> str | None:
    return f"{left / right:.2f} : 1" if right else None


def _section(mine: dict[str, Any] | None, league_shares: dict[str, float], *, passing: bool) -> dict[str, Any]:
    cells = (mine or {}).get("dir", {})
    total = int(sum(cell["n"] for cell in cells.values()))
    if total < MIN_SECTION:
        return {"has_data": False, "total": 0, "rows": [], "ratio": None, "league_ratio": None, "matrix": []}
    rows = []
    for direction in DIRECTIONS:
        cell = cells.get(direction) or _new()
        n = int(cell["n"])
        epa = cell["epa"] / cell["epa_n"] if cell["epa_n"] else None
        row = {"bucket": LABELS[direction], "n": n, "share": n / total, "league_share": league_shares.get(direction),
               "epa_per": epa, "epa_class": _signed_class(epa, 0.0),
               "yards_per": cell["yards"] / n if n else None}
        if passing:
            row["completion_rate"] = cell["completions"] / n if n else None
            row["adot"] = cell["air"] / cell["air_n"] if cell["air_n"] else None
        else:
            row["success"] = cell["success"] / cell["success_n"] if cell["success_n"] else None
        rows.append(row)
    left, right = cells.get("left", _new())["n"], cells.get("right", _new())["n"]
    matrix = []
    if passing:
        grid = (mine or {}).get("matrix", {})
        grid_total = sum(cell["n"] for cell in grid.values()) or 1
        for depth, label in DEPTHS:
            line = {"label": label, "cells": []}
            for direction in DIRECTIONS:
                cell = grid.get((depth, direction))
                n = int(cell["n"]) if cell else 0
                epa = cell["epa"] / cell["epa_n"] if cell and cell["epa_n"] else None
                line["cells"].append({"n": n, "epa_per": epa, "epa_class": _signed_class(epa, 0.0),
                                      "heat": heat(n / grid_total * 3, 0.02) if n else None})
            matrix.append(line)
    return {"has_data": True, "total": total, "rows": rows, "ratio": _ratio(left, right),
            "league_ratio": _ratio(league_shares.get("left", 0), league_shares.get("right", 0)), "matrix": matrix}


def team_side(repository, season: int, school: str, side: str) -> dict[str, Any]:
    """{'has_data', 'season', 'passes', 'runs'} for a team's offense or defense this season."""
    data = league_splits(repository, season)
    passes = _section(data["pass"][side].get(school), _league_shares(data["pass"][side]), passing=True)
    runs = _section(data["run"][side].get(school), _league_shares(data["run"][side]), passing=False)
    return {"has_data": bool(passes["has_data"] or runs["has_data"]), "season": int(season),
            "passes": passes, "runs": runs}


def team_view(repository, season: int, school: str) -> dict[str, Any] | None:
    """Offense and defense views, using last season as a labelled baseline until this one has enough charted plays."""
    chosen, note = season, ""
    current = {side: team_side(repository, season, school, side) for side in ("offense", "defense")}
    charted = current["offense"]["passes"]["total"] + current["offense"]["runs"]["total"]
    if charted < MIN_CHARTED:
        prior = {side: team_side(repository, season - 1, school, side) for side in ("offense", "defense")}
        if prior["offense"]["has_data"]:
            current, chosen = prior, season - 1
            note = f"{season - 1} baseline (only {charted} charted plays in {season})"
        elif not current["offense"]["has_data"]:
            return None
    return {"season": chosen, "note": note, "offense": current["offense"], "defense": current["defense"]}
