"""CFB team styles: opponent-adjusted offence and defence ratings, and the categories built from them.

The construction is the NFL one (nfl/team_styles.py): for each metric every team-game is modelled as

    value = mu + home_edge + offence_i + defence_j

so a team's offence rating is what it does against an average defence and its defence rating is what it
allows to an average offence, solved only from games before the one being viewed, with the two prior
seasons' final ratings (shrunk toward average) entering as pseudo-games that fade as real games arrive.

What differs from the NFL:
- Observations come from the in-house play-by-play team-game tables (advanced, pace) and the game scores.
- The field is about 230 teams once FCS opponents are included, so the normal equations are built with a
  matrix product instead of a loop over observations; the maths is identical (tested against the NFL solver).
- Categories are league terciles among FBS teams only. FCS opponents are rated, because they are in FBS
  teams' schedules, but they are not part of the league the labels are relative to.
- A game is "before" by kickoff time, not week number.

Nothing here uses the market.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from typing import Any

import numpy as np

from sports_aggregator.nfl import team_styles as nfl_styles

MODEL_VERSION = "cfb-team-styles-v1"
#: Tuned by walk-forward on 2023-2025 expected-margin error (13.79 at the NFL's 6 and 0.5, 12.81 here). CFB
#: rosters turn over more, but a team's last season is still the best guide to its first few games, and the
#: real games should take over quickly because a season is only twelve games long.
PRIOR_K = 3.0          # pseudo-games per team side
PRIOR_SHRINK = 1.0     # share of the earlier season's final rating carried forward
LEAGUE_K = nfl_styles.LEAGUE_K
#: The metrics the panel shows; each is a key of nfl_styles.METRICS so the NFL value definitions are shared.
METRICS = ("points", "pass_epa", "rush_epa", "explosive", "pass_rate", "pace")

FBS_CONFERENCES = frozenset({
    "ACC", "American Athletic", "Big 12", "Big Ten", "Conference USA", "FBS Independents",
    "Mid-American", "Mountain West", "Pac-12", "SEC", "Sun Belt",
})

_SQL = """
SELECT g.game_id,g.season,g.start_date,g.neutral_site,g.home_team,g.away_team,
       g.home_points,g.away_points,g.home_conference,g.away_conference,
       a.team,a.opponent,a.scrimmage_plays,a.pass_plays,a.rush_plays,
       a.pass_epa_per_play,a.rush_epa_per_play,a.explosive_rate,
       p.neutral_pass_rate,p.seconds_per_play
FROM games g
JOIN cfb_team_game_advanced a ON a.game_id=g.game_id AND a.model_version=? AND a.metric_version=?
JOIN cfb_team_game_pace p ON p.game_id=g.game_id AND p.team=a.team AND p.metric_version=?
WHERE g.season BETWEEN ? AND ? AND g.completed=1
  AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL
ORDER BY g.start_date,g.game_id,a.team
"""


def _times(rate: float | None, count: float | None) -> float | None:
    return None if rate is None or count is None else float(rate) * float(count)


def load_observations(repository, start: int, end: int) -> list[dict[str, Any]]:
    """One row per team-game, in the column shape nfl_styles.METRICS reads, plus the team's conference."""
    from sports_aggregator.cfb.team_game_advanced import METRIC_VERSION as ADVANCED, MODEL_VERSION as EP
    from sports_aggregator.cfb.team_game_pace import METRIC_VERSION as PACE
    try:
        with closing(repository._connect()) as connection:
            records = [dict(r) for r in connection.execute(
                _SQL, (EP, ADVANCED, PACE, int(start), int(end)))]
    except Exception:  # the charted tables were never built here: there is nothing to rate
        return []
    rows = []
    for r in records:
        is_home = r["team"] == r["home_team"]
        plays = float(r["scrimmage_plays"] or 0)
        pass_plays, rush_plays = float(r["pass_plays"] or 0), float(r["rush_plays"] or 0)
        pass_epa, rush_epa = _times(r["pass_epa_per_play"], pass_plays), _times(r["rush_epa_per_play"], rush_plays)
        explosive = _times(r["explosive_rate"], plays)
        neutral_passes = _times(r["neutral_pass_rate"], plays)
        seconds = _times(r["seconds_per_play"], plays)
        # A missing value gets a zero denominator, which nfl_styles._value skips instead of treating as a zero.
        rows.append({
            "game_id": r["game_id"], "season": r["season"], "start_date": r["start_date"],
            "team": r["team"], "opp": r["opponent"],
            "conference": r["home_conference"] if is_home else r["away_conference"],
            "home_col": 0.0 if r["neutral_site"] else (0.5 if is_home else -0.5),
            "points": float(r["home_points"] if is_home else r["away_points"]),
            "plays": plays if explosive is not None else 0.0, "explosive_plays": explosive or 0.0,
            "pass_plays": pass_plays if pass_epa is not None else 0.0, "pass_epa": pass_epa or 0.0,
            "rush_plays": rush_plays if rush_epa is not None else 0.0, "rush_epa": rush_epa or 0.0,
            "neutral_plays": plays if neutral_passes is not None else 0.0, "neutral_passes": neutral_passes or 0.0,
            "clocked_plays": plays if seconds is not None else 0.0, "seconds_sum": seconds or 0.0,
        })
    return rows


def solve(rows: list[dict[str, Any]], metric: str, teams: list[str],
          prior: dict[str, tuple[float, float]] | None = None, prior_k: float = PRIOR_K,
          league_prior: tuple[float, float] | None = None) -> dict[str, Any]:
    """The NFL weighted ridge (nfl_styles.solve) with the normal equations built by a matrix product."""
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    values = [(r, nfl_styles._value(r, metric)) for r in rows if r["team"] in index and r["opp"] in index]
    values = [(r, v) for r, v in values if v is not None]
    size = 2 + 2 * n
    gram, rhs = np.zeros((size, size)), np.zeros(size)
    if values:
        count = len(values)
        design = np.zeros((count, size))
        design[:, 0] = 1.0
        design[:, 1] = [r["home_col"] for r, _ in values]
        design[np.arange(count), [2 + index[r["team"]] for r, _ in values]] = 1.0
        design[np.arange(count), [2 + n + index[r["opp"]] for r, _ in values]] = 1.0
        target = np.array([v[0] for _, v in values])
        weight = np.array([v[1] for _, v in values])
        weight = weight / float(weight.mean())
        weighted = design * weight[:, None]
        gram += design.T @ weighted
        rhs += weighted.T @ target
    for team, i in index.items():
        p_off, p_def = (prior or {}).get(team, (0.0, 0.0))
        for offset, p in ((2, p_off), (2 + n, p_def)):
            gram[offset + i, offset + i] += prior_k
            rhs[offset + i] += prior_k * p
    gram[0, 0] += 1e-9
    gram[1, 1] += 1e-9
    if league_prior is not None:
        for i, p in enumerate(league_prior):
            gram[i, i] += LEAGUE_K
            rhs[i] += LEAGUE_K * p
    theta = np.linalg.lstsq(gram, rhs, rcond=None)[0]
    off = {t: float(theta[2 + i]) for t, i in index.items()}
    dfn = {t: float(theta[2 + n + i]) for t, i in index.items()}
    off_mean, dfn_mean = float(np.mean(list(off.values()))), float(np.mean(list(dfn.values())))
    return {"mu": float(theta[0]) + off_mean + dfn_mean, "home": float(theta[1]),
            "off": {t: v - off_mean for t, v in off.items()},
            "def": {t: v - dfn_mean for t, v in dfn.items()}, "games": len(values) // 2}


def _carry(rows: list[dict[str, Any]], shrink: float = PRIOR_SHRINK):
    """Final ratings of each earlier season, chained, as (per-metric prior, per-metric league mean/home)."""
    previous: dict[str, dict[str, tuple[float, float]]] = {}
    league: dict[str, tuple[float, float]] = {}
    for year in sorted({r["season"] for r in rows}):
        games = [r for r in rows if r["season"] == year]
        teams = sorted({r["team"] for r in games} | {r["opp"] for r in games})
        final = {m: solve(games, m, teams, previous.get(m), league_prior=league.get(m)) for m in METRICS}
        league = {m: (s["mu"], s["home"]) for m, s in final.items()}
        previous = {m: {t: (shrink * s["off"][t], shrink * s["def"][t]) for t in teams} for m, s in final.items()}
    return previous, league


def ratings_before(rows: list[dict[str, Any]], season: int, before: str, shrink: float = PRIOR_SHRINK
                   ) -> dict[str, dict[str, Any]]:
    """Ratings from `season`'s games strictly before `before` (a start_date), carrying the earlier seasons in `rows`."""
    earlier = [r for r in rows if r["season"] < season]
    current = [r for r in rows if r["season"] == season and r["start_date"] < before]
    previous, league = _carry(earlier, shrink)
    teams = sorted({r["team"] for r in rows if r["season"] == season} | {r["opp"] for r in rows if r["season"] == season}
                   | {t for p in previous.values() for t in p})
    return {m: solve(current, m, teams, previous.get(m), league_prior=league.get(m)) for m in METRICS} if teams else {}


def fbs_teams(rows: list[dict[str, Any]], season: int) -> set[str]:
    """Teams whose conference, in `season`'s games, is an FBS one."""
    return {r["team"] for r in rows if r["season"] == season and r["conference"] in FBS_CONFERENCES}


def only(ratings: dict[str, dict[str, Any]], teams: set[str]) -> dict[str, dict[str, Any]]:
    """The same ratings restricted to `teams`, so ranks and category cut-offs are relative to that league."""
    return {m: {**s, "off": {t: v for t, v in s["off"].items() if t in teams},
                "def": {t: v for t, v in s["def"].items() if t in teams}} for m, s in ratings.items()}


def classify(ratings: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    """team -> {approach, tempo, explosive, vulnerability, big_plays}; the NFL's tercile rules."""
    return nfl_styles.classify(ratings)


# One snapshot per (season, kickoff day, rows loaded): a slate's pages share it.
_CACHE: dict[tuple, dict[str, Any]] = {}


def snapshot_for(repository, season: int, before: str) -> dict[str, Any]:
    """{"ratings": FBS-restricted ratings, "labels": ..., "fbs": set} for games before `before`, or {}."""
    rows = load_observations(repository, int(season) - 2, int(season))
    key = (getattr(repository, "path", None), int(season), str(before)[:10], len(rows))
    if key in _CACHE:
        return _CACHE[key]
    result: dict[str, Any] = {}
    fbs = fbs_teams(rows, int(season))
    if rows and fbs:
        ratings = ratings_before(rows, int(season), str(before))
        if ratings:
            ratings = only(ratings, fbs)
            result = {"ratings": ratings, "labels": classify(ratings), "fbs": fbs}
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[key] = result
    return result
