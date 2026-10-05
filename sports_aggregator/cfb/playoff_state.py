"""Leak-safe season snapshots and team ratings for the playoff simulator.

`load_state` rebuilds what was knowable after a given week -- played results,
the Elo each team carried into its next game, and market lines that were posted
by then -- so the same code drives the live forecast (as_of = latest completed
week) and the historical backtests.

Ratings are in points of scoreboard margin vs a league-average FBS team. They
come from a ridge fit that combines three signals: actual margins, market lines
(the sharpest single input we have), and an Elo prior that carries the offseason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

ELO_PER_POINT = 24.35          # fit on 2022-25 completed games (see playoff docs)
DEFAULT_HFA = 3.0              # points for the home team off neutral sites
FCS_RATING = -24.0             # stand-in rating for non-FBS opponents
CHAMPIONSHIP_NOTE = "championship"


@dataclass
class RatingParams:
    prior_weight: float = 2.0    # ridge strength, in "games worth" of Elo prior
    margin_weight: float = 1.0
    line_weight: float = 2.0
    hfa: float = DEFAULT_HFA
    margin_cap: float = 28.0     # blowouts past this say nothing extra about strength
    elo_scale: float = ELO_PER_POINT


@dataclass
class SeasonState:
    season: int
    as_of_week: int
    conference: dict[str, str]                 # FBS team -> conference
    games: list[dict[str, Any]]                # every game in scope, see load_state
    elo: dict[str, float] = field(default_factory=dict)
    division: dict[str, str] = field(default_factory=dict)   # team -> division ("" if none)

    @property
    def teams(self) -> list[str]:
        return sorted(self.conference)


def _mean_line(rows: list[Any]) -> float | None:
    values = [float(r["spread"]) for r in rows if r["spread"] is not None]
    return sum(values) / len(values) if values else None


def load_state(repository, season: int, *, as_of_week: int | None = None,
               line_horizon_weeks: int = 1,
               through_championships: bool = False) -> SeasonState:
    """Snapshot `season` as of the end of `as_of_week` (default: latest completed week).

    Each game dict has: id, week, home, away, neutral, conf_game, home_pts/away_pts
    (None unless played as of the snapshot), line (home expected margin from the
    market, only if posted by then), home_elo, away_elo.

    Conference championship games and postseason games are dropped: the simulator
    creates those itself from the final standings. `through_championships` is the
    training view instead: it keeps the title games (flagged `title`) and drops
    anything played after them, i.e. exactly what the committee saw for its final
    ranking.
    """
    season = int(season)
    with repository._reader() as connection:
        records = list(connection.execute(
            "SELECT team, conference, division FROM team_records WHERE season=?", (season,)))
        conference = {r["team"]: r["conference"] for r in records}
        division = {r["team"]: (r["division"] or "") for r in records}
        raw = [dict(r) for r in connection.execute(
            """SELECT game_id, week, season_type, start_date, neutral_site, conference_game,
                      completed, home_team, away_team, home_points, away_points,
                      home_pregame_elo, away_pregame_elo, notes
               FROM games WHERE season=? AND season_type='regular'
               ORDER BY week, start_date, game_id""", (season,))]
        lines: dict[int, list[Any]] = {}
        for r in connection.execute(
                "SELECT game_id, spread FROM game_lines WHERE season=?", (season,)):
            lines.setdefault(int(r["game_id"]), []).append(r)
    is_title = lambda g: CHAMPIONSHIP_NOTE in (g["notes"] or "").lower()
    if through_championships:
        last_title = max((g["start_date"] for g in raw if is_title(g)), default=None)
        raw = [g for g in raw if last_title is None or g["start_date"] <= last_title]
        as_of_week = max(int(g["week"]) for g in raw)
    else:
        raw = [g for g in raw if not is_title(g)]
    if as_of_week is None:
        done = [int(g["week"]) for g in raw if g["completed"]]
        as_of_week = max(done) if done else 0
    as_of_week = int(as_of_week)

    games: list[dict[str, Any]] = []
    for g in raw:
        week = int(g["week"])
        played = bool(g["completed"]) and week <= as_of_week and g["home_points"] is not None
        market = _mean_line(lines.get(int(g["game_id"]), []))
        # A line only counts if it would have been posted by now: it is already
        # known for played games, and for the next `line_horizon_weeks` weeks.
        usable = market is not None and (played or week <= as_of_week + line_horizon_weeks)
        games.append({
            "id": int(g["game_id"]), "week": week,
            "home": g["home_team"], "away": g["away_team"],
            "neutral": bool(g["neutral_site"]), "conf_game": bool(g["conference_game"]),
            "home_pts": g["home_points"] if played else None,
            "away_pts": g["away_points"] if played else None,
            "line": -market if usable else None,
            "home_elo": g["home_pregame_elo"], "away_elo": g["away_pregame_elo"],
            "title": is_title(g),
        })

    # Elo each team carries today: its pregame Elo in the first unplayed game,
    # falling back to the last game it played (one game stale, never leaky).
    elo: dict[str, float] = {}
    for g in games:
        if g["home_pts"] is None:
            for team, value in ((g["home"], g["home_elo"]), (g["away"], g["away_elo"])):
                if value is not None and team not in elo:
                    elo[team] = float(value)
    for g in games:
        if g["home_pts"] is not None:
            for team, value in ((g["home"], g["home_elo"]), (g["away"], g["away_elo"])):
                if value is not None:
                    elo.setdefault(team, float(value))
    return SeasonState(season, as_of_week, conference, games, elo, division)


def prior_ratings(state: SeasonState, params: RatingParams) -> dict[str, float]:
    """Elo converted to points and centred on the FBS average."""
    teams = state.teams
    values = {t: state.elo[t] for t in teams if t in state.elo}
    if not values:
        return {t: 0.0 for t in teams}
    centre = sum(values.values()) / len(values)
    return {t: (values[t] - centre) / params.elo_scale if t in values else -8.0
            for t in teams}


def ridge_system(state: SeasonState, params: RatingParams, *, sim_margins: bool = False
                 ) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """Normal equations (A, b) of the rating fit, plus the team index.

    With `sim_margins` the unplayed FBS-vs-FBS games also enter A with the margin
    weight (their outcomes are not in b yet): that is the matrix the simulator
    needs to turn a scenario's results into end-of-season ratings exactly.
    """
    teams = state.teams
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    prior = prior_ratings(state, params)
    prior_vec = np.array([prior[t] for t in teams])
    A = np.eye(n) * params.prior_weight
    b = prior_vec * params.prior_weight

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
            margin = float(g["home_pts"]) - float(g["away_pts"]) - hfa
            cap = params.margin_cap
            add(h, a, max(-cap, min(cap, margin)), params.margin_weight)
        elif sim_margins:
            add(h, a, None, params.margin_weight)
        if g["line"] is not None:
            add(h, a, float(g["line"]) - hfa, params.line_weight)
    return A, b, index


def fit_ratings(state: SeasonState, params: RatingParams | None = None) -> dict[str, float]:
    """Weighted ridge: margins + market lines pulled toward the Elo prior."""
    params = params or RatingParams()
    A, b, index = ridge_system(state, params)
    solved = np.linalg.solve(A, b)
    solved = solved - solved.mean()
    return {t: float(solved[i]) for t, i in index.items()}


def fcs_rating(state: SeasonState, team: str, ratings: dict[str, float]) -> float:
    """Rating for a non-FBS opponent: its Elo if known, else the FCS default."""
    value = state.elo.get(team)
    if value is None or not ratings:
        return FCS_RATING
    mean_elo = sum(state.elo[t] for t in ratings if t in state.elo) / max(
        1, sum(1 for t in ratings if t in state.elo))
    return min(FCS_RATING + 6.0, (value - mean_elo) / ELO_PER_POINT)
