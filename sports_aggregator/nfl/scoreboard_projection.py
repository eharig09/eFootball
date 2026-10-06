"""The engine's projected score for each game of a week, for the scoreboard.

Upcoming games take the Football Lab forecast from the picks dashboard (about a second and a half
for a week, so it is memoised on the database's state). Finished games take the forecast frozen in
the issuance ledger, which by construction predates kickoff, so a result is never shown beside a
projection that was recomputed with knowledge of it. A finished game with no ledger row simply has
no projection.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from typing import Any

from sports_aggregator.cfb import derived_cache

LOGGER = logging.getLogger(__name__)


def _upcoming(repository, season: int, week: int) -> dict[str, dict[str, Any]]:
    from sports_aggregator.nfl.engine_picks import build_dashboard
    dashboard = derived_cache.derived(
        repository, "nfl_scoreboard_dashboard", lambda: build_dashboard(repository, season, week),
        int(season), int(week))
    out: dict[str, dict[str, Any]] = {}
    for game in dashboard.get("games") or []:
        model = game.get("football_lab")
        if not model:
            continue
        out[str(game["game_id"])] = {
            "away": float(model["away_points"]), "home": float(model["home_points"]),
            "margin": float(model["margin"]), "total": float(model["total"]), "frozen": False}
    return out


def _frozen(repository, game_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Latest forecast issued for each game: the ledger only ever stores pre-kickoff issuances."""
    if not game_ids:
        return {}
    marks = ",".join("?" for _ in game_ids)
    try:
        repository.initialize()
        with closing(repository._connect()) as connection:
            rows = connection.execute(
                f"""SELECT f.game_id,f.away_points,f.home_points,f.model_margin,f.model_total
                    FROM nfl_engine_forecasts f
                    JOIN (SELECT game_id,MAX(generated_at) AS latest FROM nfl_engine_forecasts
                          WHERE game_id IN ({marks}) GROUP BY game_id) l
                      ON l.game_id=f.game_id AND l.latest=f.generated_at""", game_ids).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(r[0]): {"away": float(r[1]), "home": float(r[2]), "margin": float(r[3]),
                        "total": float(r[4]), "frozen": True} for r in rows}


def week_projections(repository, season: int, week: int, games: list[dict[str, Any]]
                     ) -> dict[str, dict[str, Any]]:
    """Projection per game id for the week; games the engine has no number for are absent."""
    finished = [str(g["game_id"]) for g in games if g.get("completed")]
    out = _frozen(repository, finished)
    if any(not g.get("completed") for g in games):
        try:
            upcoming = _upcoming(repository, season, week)
        except Exception:  # a forecast that cannot be built must not take the scoreboard down
            LOGGER.exception("NFL scoreboard projections unavailable season=%s week=%s", season, week)
            upcoming = {}
        for game in games:
            if not game.get("completed") and str(game["game_id"]) in upcoming:
                out[str(game["game_id"])] = upcoming[str(game["game_id"])]
    return out
