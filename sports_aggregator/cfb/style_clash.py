"""The CFB style-clash panel: each team's style set against the opponent's, and the opponent-adjusted expectation.

The same panel as the NFL's (nfl/style_clash.py), on CFB team styles: offence and defence rated separately on
each measure, adjusted for the opposition faced, from games before this one, with the earlier seasons carried in.
Ranks and category cut-offs are among FBS teams.

This is description plus an opponent-adjusted projection, not a pick and not the Football Lab number: in a
2023-2025 walk-forward its expected margin misses by about 12.8 points on average (the Football Lab margin, which
uses far more, is about 11.1), so the categories label the matchup and never move a number.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb import derived_cache, team_styles
from sports_aggregator.nfl import style_clash as nfl_panel

MIN_GAMES_SETTLED = 3


def _games_played(repository, season: int, team: str, before: str) -> int:
    from contextlib import closing
    with closing(repository._connect()) as connection:
        return int(connection.execute(
            """SELECT COUNT(*) FROM games WHERE season=? AND completed=1 AND start_date<?
               AND (home_team=? OR away_team=?)""", (season, before, team, team)).fetchone()[0])


def _expected(points: dict[str, Any], team: str, opponent: str, home_col: float) -> float:
    return points["mu"] + points["home"] * home_col + points["off"][team] + points["def"][opponent]


def build(repository, game: dict[str, Any]) -> dict[str, Any]:
    """Panel data for one game, or {"available": False, "reason": ...}."""
    away, home = game["away_team"], game["home_team"]
    before = game.get("start_date") or datetime.now(timezone.utc).isoformat()
    season = int(game["season"])
    snap = derived_cache.derived(
        repository, "cfb_team_styles", lambda: team_styles.snapshot_for(repository, season, before),
        season, str(before)[:10])
    if not snap:
        return {"available": False, "reason": "Style ratings need charted play-by-play for the two teams' seasons."}
    ratings, labels = snap["ratings"], snap["labels"]
    if any(team not in snap["fbs"] for team in (away, home)):
        return {"available": False, "reason": "Style ratings compare FBS teams; one of these teams is not in the FBS."}
    if any(team not in ratings["points"]["off"] for team in (away, home)):
        return {"available": False, "reason": "Style ratings need charted games for both teams."}
    played = min(_games_played(repository, season, team, before) for team in (away, home))
    neutral = bool(game.get("neutral_site"))
    points = ratings["points"]
    home_base = _expected(points, home, away, 0.0 if neutral else 0.5)
    away_base = _expected(points, away, home, 0.0 if neutral else -0.5)
    return {
        "available": True, "games_played": played, "settled": played >= MIN_GAMES_SETTLED,
        "labels": {away: labels[away], home: labels[home]},
        "directions": [
            nfl_panel._direction(ratings, labels, away, home, 0.0 if neutral else -0.5),
            nfl_panel._direction(ratings, labels, home, away, 0.0 if neutral else 0.5),
        ],
        "expected": {"away": away_base, "home": home_base, "margin": home_base - away_base,
                     "total": home_base + away_base},
        "neutral": neutral,
    }
