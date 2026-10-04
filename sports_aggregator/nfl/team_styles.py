"""Team styles: opponent-adjusted offensive and defensive ratings, and the categories built from them.

Margin Power rates a team by its margin against its opponents' ratings. This is the same construction for the pieces
a margin is made of. For each metric m (pass EPA/play, rush EPA/play, explosive rate, neutral pass rate, seconds per
play, points) every team-game is modelled as

    value = mu + home_edge + offence_i + defence_j

so a team's offence rating is what it does against an average defence and its defence rating is what it allows to an
average offence. Ratings are re-solved each week from earlier weeks only, with last season's final ratings, shrunk
toward average, entering as pseudo-games that fade as real games arrive (the Margin Power "carry" mode).

Categories are league terciles of those ratings at the same snapshot, so a label always means "relative to the
league right now":

  offence  approach (pass-heavy / balanced / run-heavy), tempo (up-tempo / average / slow),
           explosiveness (big-play / average / methodical)
  defence  vulnerability (pass-vulnerable / balanced / run-vulnerable), big plays (prone / average / contains)

Nothing here uses the market.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from typing import Any

import numpy as np

from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-team-styles-v1"
PRIOR_K = 6.0          # pseudo-games per team side; the offence and defence ratings are each pulled this hard to their prior
PRIOR_SHRINK = 0.5     # share of last season's final rating carried into the next season
LEAGUE_K = 64.0        # pseudo-observations (about a week of games) holding the league mean and home edge to last season's

#: metric -> (numerator column, denominator column or None, higher means "more of the thing")
METRICS = {
    "pass_epa": ("pass_epa", "pass_plays"),
    "rush_epa": ("rush_epa", "rush_plays"),
    "explosive": ("explosive_plays", "plays"),
    "pass_rate": ("neutral_passes", "neutral_plays"),
    "pace": ("seconds_sum", "clocked_plays"),          # seconds per play: lower is faster
    "points": ("points", None),
}

_SQL = """
SELECT g.game_id,g.season,g.week,g.neutral_site,
       e.team,e.opponent_team AS opp,(e.team=g.home_team) AS is_home,
       CASE WHEN e.team=g.home_team THEN g.home_score ELSE g.away_score END AS points,
       e.plays,e.pass_plays,e.pass_epa,e.rush_plays,e.rush_epa,e.explosive_plays,
       s.neutral_plays,s.neutral_passes,s.seconds_sum,s.clocked_plays
FROM games g
JOIN game_team_efficiency e ON e.game_id=g.game_id
JOIN game_team_situational s ON s.game_id=g.game_id AND s.team=e.team
WHERE g.season BETWEEN ? AND ? AND g.season_type='REG' AND g.completed=1
  AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
ORDER BY g.season,g.week,g.game_id,e.team
"""


def load_observations(repository: NFLRepository, start: int, end: int) -> list[dict[str, Any]]:
    """One row per team-game: the offence's numbers, its opponent, and where it was played."""
    with closing(repository._connect()) as connection:
        rows = [dict(r) for r in connection.execute(_SQL, (int(start), int(end)))]
    for r in rows:
        r["team"], r["opp"] = canon_team(r["team"]), canon_team(r["opp"])
        # +0.5 for the designated home side, -0.5 for the away side, 0 at a neutral site
        r["home_col"] = 0.0 if r["neutral_site"] else (0.5 if r["is_home"] else -0.5)
    return rows


def _value(row: dict[str, Any], metric: str) -> tuple[float, float] | None:
    """(rate, weight) for one team-game, or None when the denominator is empty."""
    numerator, denominator = METRICS[metric]
    if denominator is None:
        return float(row[numerator]), 1.0
    weight = float(row[denominator] or 0)
    if weight <= 0:
        return None
    return float(row[numerator]) / weight, weight


def solve(rows: list[dict[str, Any]], metric: str, teams: list[str],
          prior: dict[str, tuple[float, float]] | None = None, prior_k: float = PRIOR_K,
          league_prior: tuple[float, float] | None = None) -> dict[str, Any]:
    """Weighted ridge for mu, home edge, offence_i, defence_j, shrunk toward `prior` = {team: (off, def)}.

    Weights are scaled to mean 1 so `prior_k` reads as pseudo-games whatever the metric's denominator is.
    """
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    obs = [(r, _value(r, metric)) for r in rows if r["team"] in index and r["opp"] in index]
    obs = [(r, v) for r, v in obs if v is not None]
    size = 2 + 2 * n
    gram, rhs = np.zeros((size, size)), np.zeros(size)
    if obs:
        scale = float(np.mean([v[1] for _, v in obs]))
        for r, (value, weight) in obs:
            x = np.zeros(size)
            x[0], x[1] = 1.0, r["home_col"]
            x[2 + index[r["team"]]] = 1.0
            x[2 + n + index[r["opp"]]] = 1.0
            w = weight / scale
            gram += w * np.outer(x, x)
            rhs += w * value * x
    for team, i in index.items():
        p_off, p_def = (prior or {}).get(team, (0.0, 0.0))
        for offset, p in ((2, p_off), (2 + n, p_def)):
            gram[offset + i, offset + i] += prior_k
            rhs[offset + i] += prior_k * p
    gram[0, 0] += 1e-9
    gram[1, 1] += 1e-9
    if league_prior is not None:                # without games (week 1) the league mean is last season's
        for i, p in enumerate(league_prior):
            gram[i, i] += LEAGUE_K
            rhs[i] += LEAGUE_K * p
    theta = np.linalg.lstsq(gram, rhs, rcond=None)[0]
    off = {t: float(theta[2 + i]) for t, i in index.items()}
    dfn = {t: float(theta[2 + n + i]) for t, i in index.items()}
    # centre the sides so a rating of 0 is the league average; the shift moves into mu
    off_mean, dfn_mean = float(np.mean(list(off.values()))), float(np.mean(list(dfn.values())))
    return {"mu": float(theta[0]) + off_mean + dfn_mean, "home": float(theta[1]),
            "off": {t: v - off_mean for t, v in off.items()},
            "def": {t: v - dfn_mean for t, v in dfn.items()}, "games": len(obs) // 2}


def snapshots(rows: list[dict[str, Any]], start: int, metrics=tuple(METRICS), mode: str = "carry"
              ) -> dict[tuple[int, int], dict[str, dict[str, Any]]]:
    """(season, week) -> metric -> solved ratings using only games before that week.

    `rows` must include the season before `start` so the carry-over prior exists for the first season scored.
    """
    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_season[r["season"]].append(r)
    out: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
    previous: dict[str, dict[str, tuple[float, float]]] = {}
    league: dict[str, tuple[float, float]] = {}
    for season in sorted(by_season):
        games = by_season[season]
        teams = sorted({r["team"] for r in games})
        done: list[dict[str, Any]] = []
        for week in sorted({r["week"] for r in games}):
            if season >= start:
                out[(season, week)] = {
                    m: solve(done, m, teams, previous.get(m) if mode == "carry" else None,
                             league_prior=league.get(m) if mode == "carry" else None) for m in metrics}
            done.extend(r for r in games if r["week"] == week)
        final = {m: solve(games, m, teams, previous.get(m), league_prior=league.get(m)) for m in metrics}
        league = {m: (s["mu"], s["home"]) for m, s in final.items()}
        previous = {m: {t: (PRIOR_SHRINK * s["off"][t], PRIOR_SHRINK * s["def"][t]) for t in teams}
                    for m, s in final.items()}
    return out


# ---------------------------------------------------------------- categories

def _terciles(values: dict[str, float]) -> tuple[float, float]:
    ordered = np.array(sorted(values.values()))
    return float(np.quantile(ordered, 1 / 3)), float(np.quantile(ordered, 2 / 3))


def _bucket(value: float, cuts: tuple[float, float], labels: tuple[str, str, str]) -> str:
    return labels[0] if value <= cuts[0] else labels[2] if value >= cuts[1] else labels[1]


def classify(ratings: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    """team -> {approach, tempo, explosive, vulnerability, big_plays} from one snapshot's ratings."""
    off, dfn = (lambda m: ratings[m]["off"]), (lambda m: ratings[m]["def"])
    teams = sorted(off("pass_rate"))

    def z(values: dict[str, float]) -> dict[str, float]:
        mean, spread = float(np.mean(list(values.values()))), float(np.std(list(values.values()))) or 1.0
        return {t: (v - mean) / spread for t, v in values.items()}

    pass_z, rush_z = z(dfn("pass_epa")), z(dfn("rush_epa"))
    vulnerability_raw = {t: pass_z[t] - rush_z[t] for t in teams}
    cuts = {
        "approach": _terciles(off("pass_rate")), "tempo": _terciles(off("pace")),
        "explosive": _terciles(off("explosive")), "vulnerability": _terciles(vulnerability_raw),
        "big_plays": _terciles(dfn("explosive")),
    }
    out: dict[str, dict[str, str]] = {}
    for t in teams:
        out[t] = {
            "approach": _bucket(off("pass_rate")[t], cuts["approach"], ("Run-heavy", "Balanced", "Pass-heavy")),
            # seconds per play: the smallest values are the fastest teams
            "tempo": _bucket(off("pace")[t], cuts["tempo"], ("Up-tempo", "Average tempo", "Slow")),
            "explosive": _bucket(off("explosive")[t], cuts["explosive"], ("Methodical", "Average", "Big-play")),
            "vulnerability": _bucket(vulnerability_raw[t], cuts["vulnerability"],
                                     ("Run-vulnerable", "Balanced", "Pass-vulnerable")),
            "big_plays": _bucket(dfn("explosive")[t], cuts["big_plays"], ("Contains", "Average", "Big-play prone")),
        }
    return out


# ---------------------------------------------------------------- one game's snapshot (for pages)

_SNAPSHOT_CACHE: dict[tuple, dict[str, dict[str, Any]]] = {}


def snapshot_for(repository: NFLRepository, season: int, week: int) -> dict[str, dict[str, Any]]:
    """Pregame ratings for one game: this season's games before `week`, with the two prior seasons as the carry.

    Solves each metric a handful of times instead of replaying every week since 2010, so it is cheap enough for a
    page view. A deployment that only holds the current season simply has no prior: the ratings come from the games
    played so far and `games` on each metric says how thin that is.
    """
    rows = load_observations(repository, int(season) - 2, int(season))
    current = [r for r in rows if r["season"] == season and r["week"] < week]
    key = (int(season), int(week), len(rows), len(current))
    if key in _SNAPSHOT_CACHE:
        return _SNAPSHOT_CACHE[key]
    previous: dict[str, dict[str, tuple[float, float]]] = {}
    league: dict[str, tuple[float, float]] = {}
    for year in (season - 2, season - 1):
        games = [r for r in rows if r["season"] == year]
        if not games:
            continue
        teams = sorted({r["team"] for r in games})
        final = {m: solve(games, m, teams, previous.get(m), league_prior=league.get(m)) for m in METRICS}
        league = {m: (s["mu"], s["home"]) for m, s in final.items()}
        previous = {m: {t: (PRIOR_SHRINK * s["off"][t], PRIOR_SHRINK * s["def"][t]) for t in teams}
                    for m, s in final.items()}
    teams = sorted({r["team"] for r in rows if r["season"] == season} | {t for p in previous.values() for t in p})
    out = {m: solve(current, m, teams, previous.get(m), league_prior=league.get(m)) for m in METRICS} if teams else {}
    if len(_SNAPSHOT_CACHE) > 64:
        _SNAPSHOT_CACHE.clear()
    _SNAPSHOT_CACHE[key] = out
    return out
