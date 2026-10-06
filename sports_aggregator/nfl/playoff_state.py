"""Leak-safe NFL season snapshots and team ratings for the playoff simulator.

`load_state` rebuilds what was knowable after a given week (results, the Elo each
team carried into its next game, market lines posted by then), so the same code
drives the live forecast and the historical backtests.

Ratings are points of scoreboard margin vs an average team, from a ridge fit of
actual margins and market lines pulled toward an Elo prior.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from sports_aggregator.nfl.naming import canon_team

ELO_PER_POINT = 25.0
DEFAULT_HFA = 1.8


@dataclass
class RatingParams:
    prior_weight: float = 3.0    # ridge strength, in "games worth" of Elo prior
    margin_weight: float = 1.0
    line_weight: float = 2.0
    hfa: float = DEFAULT_HFA
    margin_cap: float = 24.0
    elo_scale: float = ELO_PER_POINT


@dataclass
class SeasonState:
    season: int
    as_of_week: int
    conference: dict[str, str]
    division: dict[str, str]
    games: list[dict[str, Any]]
    elo: dict[str, float] = field(default_factory=dict)

    @property
    def teams(self) -> list[str]:
        return sorted(self.conference)


def load_state(repository, season: int, *, as_of_week: int | None = None,
               line_horizon_weeks: int = 1) -> SeasonState:
    """Snapshot the regular season `season` as of the end of `as_of_week`.

    Each game dict: id, week, home, away, neutral, div_game, home_pts/away_pts (None
    unless played as of the snapshot), line (expected home margin, only if posted by
    then), total (the posted total or None), home_elo/away_elo.
    Postseason games are not loaded; the simulator plays those itself.
    """
    season = int(season)
    repository.initialize()
    with closing(repository._connect()) as connection:
        teams = [dict(r) for r in connection.execute(
            "SELECT abbreviation, conference, division FROM teams")]
        raw = [dict(r) for r in connection.execute(
            """SELECT game_id, week, neutral_site, division_game, completed, home_team, away_team,
                      home_score, away_score, spread_line, total_line
               FROM games WHERE season=? AND season_type='REG' ORDER BY week, game_date, game_id""",
            (season,))]
        elo_rows = [dict(r) for r in connection.execute(
            """SELECT week, home_team, away_team, home_pre, away_pre, home_post, away_post
               FROM nfl_elo_games WHERE season=? ORDER BY week, game_id""", (season,))]
    conference = {canon_team(t["abbreviation"]): t["conference"] for t in teams}
    division = {canon_team(t["abbreviation"]): t["division"] for t in teams}
    if as_of_week is None:
        done = [int(g["week"]) for g in raw if g["completed"]]
        as_of_week = max(done) if done else 0
    as_of_week = int(as_of_week)

    games: list[dict[str, Any]] = []
    for g in raw:
        week = int(g["week"])
        played = bool(g["completed"]) and week <= as_of_week and g["home_score"] is not None
        has_line = g["spread_line"] is not None
        usable = has_line and (played or week <= as_of_week + line_horizon_weeks)
        games.append({
            "id": str(g["game_id"]), "week": week,
            "home": canon_team(g["home_team"]), "away": canon_team(g["away_team"]),
            "neutral": bool(g["neutral_site"]), "div_game": bool(g["division_game"]),
            "home_pts": int(g["home_score"]) if played else None,
            "away_pts": int(g["away_score"]) if played else None,
            "line": float(g["spread_line"]) if usable else None,
            "total": float(g["total_line"]) if usable and g["total_line"] is not None else None,
        })

    # Elo a team carries into the as-of week: its pre-game rating in its first game
    # after the cutoff (never leaks, bye weeks don't move Elo), else the post-game
    # rating of its last game played.
    elo: dict[str, float] = {}
    for row in elo_rows:
        if int(row["week"]) > as_of_week:
            for team, value in ((row["home_team"], row["home_pre"]), (row["away_team"], row["away_pre"])):
                if value is not None:
                    elo.setdefault(canon_team(team), float(value))
    for row in reversed(elo_rows):
        if int(row["week"]) <= as_of_week:
            for team, value in ((row["home_team"], row["home_post"]), (row["away_team"], row["away_post"])):
                if value is not None:
                    elo.setdefault(canon_team(team), float(value))
    return SeasonState(season, as_of_week, conference, division, games, elo)


def prior_ratings(state: SeasonState, params: RatingParams) -> dict[str, float]:
    teams = state.teams
    values = {t: state.elo[t] for t in teams if t in state.elo}
    if not values:
        return {t: 0.0 for t in teams}
    centre = sum(values.values()) / len(values)
    return {t: (values[t] - centre) / params.elo_scale if t in values else 0.0 for t in teams}


def ridge_system(state: SeasonState, params: RatingParams, *, sim_margins: bool = False
                 ) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """Normal equations (A, b) of the rating fit plus the team index.

    With `sim_margins`, unplayed games enter A with the margin weight (outcomes not in
    b yet): the matrix the simulator needs to turn a scenario's results into
    end-of-season ratings exactly.
    """
    teams = state.teams
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    prior = prior_ratings(state, params)
    A = np.eye(n) * params.prior_weight
    b = np.array([prior[t] for t in teams]) * params.prior_weight

    def add(h: int, a: int, target: float | None, weight: float) -> None:
        if weight <= 0:
            return
        A[h, h] += weight; A[a, a] += weight
        A[h, a] -= weight; A[a, h] -= weight
        if target is not None:
            b[h] += weight * target; b[a] -= weight * target

    for g in state.games:
        h, a = index.get(g["home"]), index.get(g["away"])
        if h is None or a is None:
            continue
        hfa = 0.0 if g["neutral"] else params.hfa
        if g["home_pts"] is not None:
            margin = g["home_pts"] - g["away_pts"] - hfa
            add(h, a, max(-params.margin_cap, min(params.margin_cap, margin)), params.margin_weight)
        elif sim_margins:
            add(h, a, None, params.margin_weight)
        if g["line"] is not None:
            add(h, a, g["line"] - hfa, params.line_weight)
    return A, b, index


def fit_ratings(state: SeasonState, params: RatingParams | None = None) -> dict[str, float]:
    params = params or RatingParams()
    A, b, index = ridge_system(state, params)
    solved = np.linalg.solve(A, b)
    solved = solved - solved.mean()
    return {t: float(solved[i]) for t, i in index.items()}
