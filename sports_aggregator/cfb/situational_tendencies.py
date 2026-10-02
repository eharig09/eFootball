"""Offensive tendencies by down and distance for college football teams.

The same 4x3 grid the NFL team page shows: how often a team passes in each down and distance situation, against
the FBS average in that situation. Built from stored scrimmage plays (`cfb_plays` joined to the play metrics'
rush/pass call), regular season only, excluding penalties, kneels and spikes. The grid itself comes from
`nfl.situational_tendencies.build_grid` so both leagues render through one macro.

Pool for the league rate is every FBS offense; a team needs `MIN_SEASON_PLAYS` scrimmage plays this season or the
page falls back to last season and says so.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from typing import Any

from sports_aggregator.cfb.derived_cache import derived
from sports_aggregator.nfl.situational_tendencies import band, build_grid

#: Fewer plays than this is still early September: use the prior season as a baseline.
MIN_SEASON_PLAYS = 150

_CELLS_SQL = """
SELECT p.offense, p.week, p.down,
       CASE
         WHEN p.down = 1 THEN CASE
              WHEN p.yards_to_goal IS NOT NULL AND p.distance IS NOT NULL AND p.yards_to_goal <= p.distance THEN 'short'
              WHEN COALESCE(p.distance, 10) <= 10 THEN 'medium' ELSE 'long' END
         WHEN p.down IN (2, 3) THEN CASE
              WHEN COALESCE(p.distance, 10) <= 3 THEN 'short' WHEN COALESCE(p.distance, 10) <= 6 THEN 'medium' ELSE 'long' END
         ELSE CASE
              WHEN COALESCE(p.distance, 10) <= 2 THEN 'short' WHEN COALESCE(p.distance, 10) <= 5 THEN 'medium' ELSE 'long' END
       END AS band,
       COUNT(*), SUM(CASE WHEN m.rush_pass = 'pass' THEN 1 ELSE 0 END), SUM(COALESCE(e.epa, 0.0)), SUM(COALESCE(m.success, 0))
FROM cfb_plays p
JOIN games g ON g.game_id = p.game_id AND g.season_type = 'regular'
JOIN cfb_play_metrics m ON m.play_id = p.play_id
LEFT JOIN cfb_play_epa e ON e.play_id = p.play_id
WHERE p.season = ? AND p.down BETWEEN 1 AND 4 AND m.rush_pass IN ('pass', 'rush')
  AND p.play_type <> 'Penalty'
  AND p.offense IN (SELECT school FROM teams WHERE classification = 'fbs')
  AND LOWER(COALESCE(p.play_text, '')) NOT LIKE '%kneel%'
  AND LOWER(COALESCE(p.play_text, '')) NOT LIKE '%spike%'
GROUP BY p.offense, p.week, p.down, band
"""


def season_cells(repository, season: int) -> list[tuple]:
    """(team, week, down, band, plays, passes, epa, successes) for a whole season, aggregated by SQLite.

    One grouped query replaces a Python loop over ~190,000 plays, and keeping the week lets every "games before week N"
    request be a cheap sum over this list instead of its own scan. `band` is the same rule as `situational_tendencies.band`
    (spelled out in SQL; tests pin the two together).
    """
    def build() -> list[tuple]:
        with closing(repository._connect()) as connection:
            return [tuple(row) for row in connection.execute(_CELLS_SQL, (int(season),))]
    return derived(repository, "cfb_situational_cells", build, int(season))


def _tally(repository, season: int, before_week: int | None = None) -> dict[str, Any]:
    cells: dict[tuple[str, int, str], list[float]] = defaultdict(lambda: [0, 0, 0.0, 0])
    for team, week, down, situation_band, plays, passes, epa, successes in season_cells(repository, season):
        if before_week is not None and (week is None or int(week) >= int(before_week)):
            continue
        cell = cells[(team, int(down), situation_band)]
        cell[0] += plays
        cell[1] += passes
        cell[2] += epa or 0.0
        cell[3] += successes or 0
    totals: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
    teams: dict[str, dict[tuple[int, str], list[float]]] = defaultdict(dict)
    for (team, down, situation_band), values in cells.items():
        teams[team][(down, situation_band)] = values
        totals[(down, situation_band)][0] += values[0]
        totals[(down, situation_band)][1] += values[1]
    return {"teams": dict(teams),
            "league": {key: (passes / plays if plays else None) for key, (plays, passes) in totals.items()}}


def league_tendencies(repository, season: int, before_week: int | None = None) -> dict[str, Any]:
    """{'teams': {school: {(down, band): [plays, passes, epa, successes]}}, 'league': {(down, band): pass_rate}}.

    `before_week` keeps only earlier weeks, so a past game's page never sees what happened after it.
    """
    return derived(repository, "cfb_situational_tendencies", lambda: _tally(repository, season, before_week),
                   int(season), before_week)


def team_tendencies(repository, season: int, school: str, before_week: int | None = None) -> dict[str, Any] | None:
    """The grid for one team this season, or None when it has no stored plays."""
    data = league_tendencies(repository, season, before_week)
    mine = data["teams"].get(school)
    if not mine:
        return None
    return build_grid(mine, data["league"], school, season)


def team_view(repository, season: int, school: str) -> dict[str, Any] | None:
    """The grid to show: this season once it has enough plays, otherwise last season as a labelled baseline."""
    current = team_tendencies(repository, season, school)
    if current and current["plays"] >= MIN_SEASON_PLAYS:
        return {"profile": current, "note": ""}
    prior = team_tendencies(repository, season - 1, school)
    if prior:
        return {"profile": prior, "note": f"{season - 1} baseline" + (
            f" (only {current['plays']} plays in {season} so far)" if current else f" (no {season} plays yet)")}
    return {"profile": current, "note": "early-season sample"} if current else None


def matchup_view(repository, game: dict[str, Any]) -> dict[str, Any] | None:
    """Both offenses' call mix side by side for a game page, using only games played before it.

    Both teams must have a real sample this season; otherwise both fall back to last season in full, so the two
    columns always describe the same period. Postseason weeks restart the numbering, so those games see the whole
    regular season.
    """
    from sports_aggregator.nfl.situational_tendencies import compare
    season, away, home = int(game["season"]), game["away_team"], game["home_team"]
    before = int(game["week"]) if game.get("season_type", "regular") == "regular" else None
    current = {team: team_tendencies(repository, season, team, before) for team in (away, home)}
    if all(profile and profile["plays"] >= MIN_SEASON_PLAYS for profile in current.values()):
        chosen, note = current, (f"through week {before - 1}" if before else "regular season")
    else:
        chosen = {team: team_tendencies(repository, season - 1, team) for team in (away, home)}
        if not all(chosen.values()):
            return None
        note = f"{season - 1} baseline"
    return {"season": next(iter(chosen.values()))["season"], "note": note, "away": chosen[away], "home": chosen[home],
            "rows": compare(chosen[away], chosen[home])}
