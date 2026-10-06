"""Build (and cache) the NFL playoff forecast the web page, API and CLI share."""
from __future__ import annotations

import hashlib
from contextlib import closing
from typing import Any

from sports_aggregator.cfb import derived_cache
from sports_aggregator.nfl.playoff_sim import Prepared, simulate
from sports_aggregator.nfl.playoff_state import RatingParams, load_state

MODEL_VERSION = "nfl-playoffs-v1"
DEFAULT_SIMS = 3000
MIN_TEAMS = 28   # an unsynced season has no schedule to project


def teams_per_conference(season: int) -> int:
    """Seven per conference since 2020, six before."""
    return 7 if season >= 2020 else 6


def empty_forecast(season: int, as_of_week: int | None = None) -> dict[str, Any]:
    """Same shape as a real forecast, for a season with no schedule stored."""
    per = teams_per_conference(season)
    return {"season": season, "as_of_week": as_of_week or 0, "n_sims": 0, "games_remaining": 0,
            "rows": [], "format": {"teams_per_conference": per, "byes_per_conference": 1 if per == 7 else 2},
            "params": {}, "model_version": MODEL_VERSION, "insufficient_data": True}


def _fingerprint(repository, season: int, as_of_week: int | None, n_sims: int) -> str:
    """Changes only when an input of the forecast changes (results, lines, Elo, model)."""
    repository.initialize()
    with closing(repository._connect()) as connection:
        games = tuple(connection.execute(
            "SELECT COUNT(*), SUM(completed), MAX(updated_at) FROM games WHERE season=? AND season_type='REG'",
            (season,)).fetchone())
        espn = tuple(connection.execute(
            "SELECT COUNT(*), MAX(fetched_at) FROM espn_market_lines WHERE season=?", (season,)).fetchone())
        elo = tuple(connection.execute(
            "SELECT COUNT(*) FROM nfl_elo_games WHERE season=?", (season,)).fetchone())
    raw = "|".join(str(x) for x in (MODEL_VERSION, season, as_of_week, n_sims, games, espn, elo))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def build_forecast(repository, season: int, *, n_sims: int = DEFAULT_SIMS,
                   as_of_week: int | None = None, use_cache: bool = True) -> dict[str, Any]:
    season = int(season)

    def build() -> dict[str, Any]:
        state = load_state(repository, season, as_of_week=as_of_week)
        if len(state.teams) < MIN_TEAMS or not state.games:
            return empty_forecast(season, state.as_of_week)
        out = simulate(Prepared(state, RatingParams(), teams_per_conference(season)), n_sims=n_sims)
        out["model_version"] = MODEL_VERSION
        return out

    if not use_cache:
        return build()
    return derived_cache.persisted(
        repository, f"nfl_playoffs_{season}", _fingerprint(repository, season, as_of_week, n_sims), build)
