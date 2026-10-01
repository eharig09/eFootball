"""FBS-wide ranked team panels (percentile bars) and offense-vs-defense rank gaps for college football pages.

Season aggregates come from the per-game advanced, pace, scoring and drive-outcome tables joined to completed games.
A team's defense is what its opponents did against it (their offensive rows where `opponent` is the team), so a
defensive number is always measured against the same plays as the offense it faces. The pool is the 138 FBS teams in
`teams`; ranks are 1-N with the best first, whatever the raw direction of the stat.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from sports_aggregator.ranked_panels import panel_row, rows_for_team
from sports_aggregator.nfl.ranking import rank_within

#: Fewer games than this across the typical team and the season is still "in progress": use the prior season.
SETTLED_GAMES = 3

# (label, key, format, lower_is_better, neutral)
OFFENSE_GROUPS = (
    ("Efficiency", (
        ("EPA / play", "epa_per_play", "signed2", False, False),
        ("Dropback EPA", "pass_epa_per_play", "signed2", False, False),
        ("Rush EPA", "rush_epa_per_play", "signed2", False, False),
        ("Success rate", "success_rate", "rate", False, False),
        ("Explosive rate", "explosive_rate", "rate", False, False),
        ("Havoc allowed", "havoc_rate", "rate", True, False),
    )),
    ("Scoring", (
        ("Points / game", "points_per_game", "f1", False, False),
        ("Points / drive", "points_per_drive", "f2", False, False),
        ("RZ TD rate", "red_zone_td_rate", "rate", False, False),
        ("3-and-out rate", "three_and_out_rate", "rate", True, False),
        ("Giveaways / game", "giveaways_per_game", "f1", True, False),
    )),
    ("Yardage + tempo", (
        ("Yards / dropback", "yards_per_dropback", "f2", False, False),
        ("Yards / rush", "yards_per_rush", "f2", False, False),
        ("Plays / game", "plays_per_game", "f1", False, True),
        ("Neutral pass rate", "neutral_pass_rate", "rate", False, True),
    )),
)
DEFENSE_GROUPS = (
    ("Efficiency allowed", (
        ("EPA / play allowed", "def_epa_per_play", "signed2", True, False),
        ("Pass EPA allowed", "def_pass_epa_per_play", "signed2", True, False),
        ("Rush EPA allowed", "def_rush_epa_per_play", "signed2", True, False),
        ("Success allowed", "def_success_rate", "rate", True, False),
        ("Explosive allowed", "def_explosive_rate", "rate", True, False),
        ("Havoc created", "def_havoc_rate", "rate", False, False),
    )),
    ("Scoring allowed", (
        ("Pts allowed / game", "points_allowed_per_game", "f1", True, False),
        ("Pts / drive allowed", "def_points_per_drive", "f2", True, False),
        ("RZ TD rate allowed", "def_red_zone_td_rate", "rate", True, False),
        ("3-and-outs forced", "def_three_and_out_rate", "rate", False, False),
        ("Takeaways / game", "takeaways_per_game", "f1", False, False),
    )),
    ("Yardage allowed", (
        ("Yds / dropback allowed", "def_yards_per_dropback", "f2", True, False),
        ("Yds / rush allowed", "def_yards_per_rush", "f2", True, False),
    )),
)

# Offense vs defense pairings for the game page: (label, offense key, offense lower?, defense key, defense lower?, format)
GAP_SECTIONS = (
    ("Efficiency", "play-by-play EPA (ep-v2)", (
        ("EPA / play", "epa_per_play", False, "def_epa_per_play", True, "signed2"),
        ("Dropback EPA", "pass_epa_per_play", False, "def_pass_epa_per_play", True, "signed2"),
        ("Rush EPA", "rush_epa_per_play", False, "def_rush_epa_per_play", True, "signed2"),
        ("Success rate", "success_rate", False, "def_success_rate", True, "rate"),
        ("Explosive rate", "explosive_rate", False, "def_explosive_rate", True, "rate"),
        ("Havoc", "havoc_rate", True, "def_havoc_rate", False, "rate"),
    )),
    ("Traditional", "box score + drives", (
        ("Points", "points_per_game", False, "points_allowed_per_game", True, "f1"),
        ("Points / drive", "points_per_drive", False, "def_points_per_drive", True, "f2"),
        ("Red-zone TDs", "red_zone_td_rate", False, "def_red_zone_td_rate", True, "rate"),
        ("Yards / dropback", "yards_per_dropback", False, "def_yards_per_dropback", True, "f2"),
        ("Yards / rush", "yards_per_rush", False, "def_yards_per_rush", True, "f2"),
        ("Ball security vs takeaways", "giveaways_per_game", True, "takeaways_per_game", False, "f1"),
        ("Three-and-outs", "three_and_out_rate", True, "def_three_and_out_rate", False, "rate"),
    )),
)

_POOL_SQL = """
WITH played AS (
    SELECT game_id FROM games WHERE season=? AND completed=1 AND home_points IS NOT NULL AND away_points IS NOT NULL
), unit AS (
    SELECT {side} team,
           SUM(a.scrimmage_plays) plays, SUM(a.total_epa) epa,
           SUM(a.pass_epa_per_play * a.pass_plays) pass_epa, SUM(a.pass_plays) pass_plays,
           SUM(a.rush_epa_per_play * a.rush_plays) rush_epa, SUM(a.rush_plays) rush_plays,
           SUM(a.success_rate * a.scrimmage_plays) succ, SUM(a.explosive_rate * a.scrimmage_plays) expl,
           SUM(a.havoc_allowed_rate * a.scrimmage_plays) havoc,
           SUM(d.meaningful_drives) drives, SUM(d.offensive_points) drive_points,
           SUM(s.red_zone_trips) rz_trips, SUM(s.red_zone_touchdowns) rz_tds, SUM(s.giveaways) giveaways,
           SUM(p.three_and_out_drives) three_outs, SUM(p.meaningful_drives) pace_drives,
           SUM(p.pass_yards) pass_yards, SUM(p.rush_yards) rush_yards,
           SUM(p.neutral_pass_rate * p.scrimmage_plays) neutral_pass, COUNT(*) games
      FROM cfb_team_game_advanced a
      JOIN played USING (game_id)
      LEFT JOIN cfb_team_game_drive_outcomes d ON d.game_id=a.game_id AND d.team=a.team
      LEFT JOIN cfb_team_game_scoring s ON s.game_id=a.game_id AND s.team=a.team
      LEFT JOIN cfb_team_game_pace p ON p.game_id=a.game_id AND p.team=a.team
     GROUP BY {side}
)
SELECT t.school team, o.games, o.plays, o.epa, o.pass_epa, o.pass_plays, o.rush_epa, o.rush_plays, o.succ, o.expl,
       o.havoc, o.drives, o.drive_points, o.rz_trips, o.rz_tds, o.giveaways, o.three_outs, o.pace_drives,
       o.pass_yards, o.rush_yards, o.neutral_pass
  FROM teams t JOIN unit o ON o.team = t.school
"""


def _ratio(numerator: Any, denominator: Any) -> float | None:
    if numerator is None or not denominator:
        return None
    return float(numerator) / float(denominator)


def _unit_stats(row: dict[str, Any], prefix: str) -> dict[str, Any]:
    games = row["games"]
    return {
        f"{prefix}epa_per_play": _ratio(row["epa"], row["plays"]),
        f"{prefix}pass_epa_per_play": _ratio(row["pass_epa"], row["pass_plays"]),
        f"{prefix}rush_epa_per_play": _ratio(row["rush_epa"], row["rush_plays"]),
        f"{prefix}success_rate": _ratio(row["succ"], row["plays"]),
        f"{prefix}explosive_rate": _ratio(row["expl"], row["plays"]),
        f"{prefix}havoc_rate": _ratio(row["havoc"], row["plays"]),
        f"{prefix}points_per_drive": _ratio(row["drive_points"], row["drives"]),
        f"{prefix}red_zone_td_rate": _ratio(row["rz_tds"], row["rz_trips"]),
        f"{prefix}three_and_out_rate": _ratio(row["three_outs"], row["pace_drives"]),
        f"{prefix}yards_per_dropback": _ratio(row["pass_yards"], row["pass_plays"]),
        f"{prefix}yards_per_rush": _ratio(row["rush_yards"], row["rush_plays"]),
        f"{prefix}plays_per_game": _ratio(row["plays"], games),
        f"{prefix}neutral_pass_rate": _ratio(row["neutral_pass"], row["plays"]),
        f"{prefix}giveaways_per_game": _ratio(row["giveaways"], games),
    }


def season_pool(repository, season: int) -> list[dict[str, Any]]:
    """One dict per FBS team with offense keys plain and defense keys prefixed `def_`."""
    def build() -> list[dict[str, Any]]:
        repository.initialize()
        with repository._reader() as connection:
            offense = {row["team"]: dict(row) for row in connection.execute(
                _POOL_SQL.format(side="a.team"), (season,))}
            defense = {row["team"]: dict(row) for row in connection.execute(
                _POOL_SQL.format(side="a.opponent"), (season,))}
            points = {row["team"]: dict(row) for row in connection.execute(
                """SELECT team, SUM(pf) pf, SUM(pa) pa, SUM(games) games FROM (
                       SELECT home_team team, home_points pf, away_points pa, 1 games FROM games
                        WHERE season=? AND completed=1 AND home_points IS NOT NULL AND away_points IS NOT NULL
                       UNION ALL
                       SELECT away_team, away_points, home_points, 1 FROM games
                        WHERE season=? AND completed=1 AND home_points IS NOT NULL AND away_points IS NOT NULL
                   ) GROUP BY team""", (season, season))}
        pool = []
        for team, row in offense.items():
            stats: dict[str, Any] = {"team": team, "games": row["games"]}
            stats.update(_unit_stats(row, ""))
            against = defense.get(team)
            if against:
                stats.update(_unit_stats(against, "def_"))
                stats["takeaways_per_game"] = stats.pop("def_giveaways_per_game")
                for key in ("def_plays_per_game", "def_neutral_pass_rate"):
                    stats.pop(key, None)
            scored = points.get(team)
            if scored and scored["games"]:
                stats["points_per_game"] = scored["pf"] / scored["games"]
                stats["points_allowed_per_game"] = scored["pa"] / scored["games"]
            pool.append(stats)
        return pool
    from sports_aggregator.cfb import derived_cache

    def safe() -> list[dict[str, Any]]:
        try:
            return build()
        except sqlite3.OperationalError:       # the per-game advanced tables are built by a separate job; absent means "no panels"
            return []
    return derived_cache.derived(repository, "team_panel_pool", safe, int(season))


def panel_season(repository, season: int) -> tuple[int, bool]:
    """(season to show, whether it is the prior-season baseline): a season is shown once the typical team has
    played enough games for its rates to mean something."""
    pool = season_pool(repository, season)
    counts = sorted(row["games"] for row in pool)
    if counts and counts[len(counts) // 2] >= SETTLED_GAMES:
        return season, False
    prior = season_pool(repository, season - 1)
    return (season - 1, True) if prior else (season, False)


def _panel(pool: list[dict[str, Any]], team: str, groups, season: int) -> dict[str, Any]:
    output = []
    for label, definitions in groups:
        rows = rows_for_team(pool, team, "team", definitions)
        if rows:
            output.append({"label": label, "rows": rows})
    return {"season": season, "groups": output, "has_data": bool(output)}


def team_panels(repository, season: int, team: str) -> dict[str, Any] | None:
    """Offense and defense percentile panels for one team, or None if it has no stored plays that season."""
    used, baseline = panel_season(repository, season)
    pool = season_pool(repository, used)
    if not any(row["team"] == team for row in pool):
        return None
    return {"season": used, "baseline": baseline, "games": next(row["games"] for row in pool if row["team"] == team),
            "offense": _panel(pool, team, OFFENSE_GROUPS, used), "defense": _panel(pool, team, DEFENSE_GROUPS, used)}


def matchup_cards(repository, season: int, away: str, home: str) -> dict[str, Any]:
    """Two offense-versus-defense rank-gap cards (shaped like the NFL's `matchup_defense_cards`)."""
    used, baseline = panel_season(repository, season)
    pool = season_pool(repository, used)
    names = {row["team"] for row in pool}
    cards = []
    for offense, defense in ((away, home), (home, away)):
        if offense not in names or defense not in names:
            cards.append({"offense": offense, "defense": defense, "sections": [], "has_data": False})
            continue
        attack = next(row for row in pool if row["team"] == offense)
        resist = next(row for row in pool if row["team"] == defense)
        sections = []
        for label, source, definitions in GAP_SECTIONS:
            rows = []
            for row_label, off_key, off_lower, def_key, def_lower, fmt in definitions:
                if attack.get(off_key) is None or resist.get(def_key) is None:
                    continue
                off_rank = rank_within(pool, id_key="team", value_key=off_key, lower_is_better=off_lower).get(offense, {})
                def_rank = rank_within(pool, id_key="team", value_key=def_key, lower_is_better=def_lower).get(defense, {})
                gap = (def_rank["rank"] - off_rank["rank"]) if off_rank and def_rank else None
                rows.append({
                    "label": row_label, "format": fmt, "offense_value": attack[off_key], "defense_value": resist[def_key],
                    "offense_rank": off_rank.get("rank"), "offense_of": off_rank.get("of"),
                    "defense_rank": def_rank.get("rank"), "defense_of": def_rank.get("of"),
                    "separation": abs(gap) if gap is not None else None,
                    # Rank 1 is best, so the offense has the edge when its rank is the smaller number.
                    "lean": (offense if gap is not None and gap >= 5 else defense if gap is not None and gap <= -5 else None),
                })
            if rows:
                sections.append({"label": label, "source": source, "rows": rows})
        cards.append({"offense": offense, "defense": defense, "sections": sections, "has_data": bool(sections)})
    return {"season": used, "baseline": baseline, "cards": cards}
