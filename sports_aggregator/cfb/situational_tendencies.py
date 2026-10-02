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

_QUERY = """
SELECT p.offense, p.down, p.distance, p.yards_to_goal, m.rush_pass, e.epa, m.success
FROM cfb_plays p
JOIN games g ON g.game_id = p.game_id AND g.season_type = 'regular'
JOIN cfb_play_metrics m ON m.play_id = p.play_id
LEFT JOIN cfb_play_epa e ON e.play_id = p.play_id
WHERE p.season = ? AND p.down BETWEEN 1 AND 4 AND m.rush_pass IN ('pass', 'rush')
  AND p.play_type <> 'Penalty'
  AND p.offense IN (SELECT school FROM teams WHERE classification = 'fbs')
  AND LOWER(COALESCE(p.play_text, '')) NOT LIKE '%kneel%'
  AND LOWER(COALESCE(p.play_text, '')) NOT LIKE '%spike%' {week_filter}
"""


def _tally(repository, season: int, before_week: int | None = None) -> dict[str, Any]:
    cells: dict[tuple[str, int, str], list[float]] = defaultdict(lambda: [0, 0, 0.0, 0])
    parameters: list[Any] = [int(season)] + ([int(before_week)] if before_week is not None else [])
    query = _QUERY.format(week_filter="AND p.week < ?" if before_week is not None else "")
    with closing(repository._connect()) as connection:
        for offense, down, distance, to_goal, call, epa, success in connection.execute(query, parameters):
            goal_to_go = 1 if (to_goal is not None and distance is not None and to_goal <= distance) else 0
            cell = cells[(offense, int(down), band(int(down), distance, goal_to_go))]
            cell[0] += 1
            cell[1] += 1 if call == "pass" else 0
            cell[2] += epa or 0.0
            cell[3] += success or 0
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
