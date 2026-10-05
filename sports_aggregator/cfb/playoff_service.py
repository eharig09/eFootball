"""Build (and cache) the playoff forecast the web page and CLI both serve."""
from __future__ import annotations

import hashlib
from typing import Any

from sports_aggregator.cfb import derived_cache
from sports_aggregator.cfb.playoff_sim import COMMITTEE, Prepared, simulate
from sports_aggregator.cfb.playoff_state import RatingParams, load_state

MODEL_VERSION = "cfp12-v2"
DEFAULT_SIMS = 4000


MIN_TEAMS = 24   # below this there is no field to project (an unsynced or tiny season)


def empty_forecast(season: int, as_of_week: int | None = None) -> dict[str, Any]:
    """The same shape as a real forecast, for a season with no teams to simulate."""
    return {"season": season, "as_of_week": as_of_week or 0, "n_sims": 0, "games_remaining": 0,
            "rows": [], "expected_field": [], "projected_field": [], "common_fields": [],
            "format": {"field_size": 12, "auto_bids": 5, "byes": 4, "seeding": "straight"},
            "params": {"title_game_conferences": []}, "model_version": MODEL_VERSION,
            "insufficient_data": True}


def _fingerprint(repository, season: int, as_of_week: int | None, n_sims: int) -> str:
    """Changes only when an input of the forecast changes (results, lines, Elo, model)."""
    with repository._reader() as connection:
        games = connection.execute(
            """SELECT COUNT(*) n, SUM(completed) done, MAX(updated_at) u FROM games WHERE season=?""",
            (season,)).fetchone()
        lines = connection.execute(
            "SELECT COUNT(*) n, MAX(fetched_at) f FROM game_lines WHERE season=?", (season,)).fetchone()
    raw = "|".join(str(x) for x in (MODEL_VERSION, season, as_of_week, n_sims, tuple(games),
                                    tuple(lines), sorted(COMMITTEE.weights.items())))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def build_forecast(repository, season: int | None = None, *, n_sims: int = DEFAULT_SIMS,
                   as_of_week: int | None = None, use_cache: bool = True) -> dict[str, Any]:
    if season is None:
        with repository._reader() as connection:
            season = int(connection.execute("SELECT MAX(season) s FROM games").fetchone()["s"])

    def build() -> dict[str, Any]:
        state = load_state(repository, season, as_of_week=as_of_week)
        if len(state.teams) < MIN_TEAMS:
            return empty_forecast(season, state.as_of_week)
        out = simulate(Prepared(state, RatingParams()), n_sims=n_sims)
        order = {r["team"]: r["avg_rank"] for r in out["rows"]}
        out["projected_field"] = sorted(out["expected_field"], key=order.__getitem__)
        out["model_version"] = MODEL_VERSION
        return out

    if not use_cache:
        return build()
    return derived_cache.persisted(
        repository, f"cfp_forecast_{season}", _fingerprint(repository, season, as_of_week, n_sims), build)
