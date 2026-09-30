"""Multi-season, banded tables for NFL team and player pages.

One row per season with grouped column bands (RECORD, VOLUME & PACE, EFFICIENCY ...),
replacing stacks of single-number cards. The selected season's row is highlighted.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.tables import Column, Table


def _selected(row: dict[str, Any], season: int, selected: int) -> dict[str, Any]:
    if season == selected:
        row["_row_class"] = "is-selected"
    return row


def team_history_seasons(repository: NFLRepository, team: str, selected: int, limit: int = 6) -> list[int]:
    """Seasons at or before `selected` in which the team actually played, newest first."""
    seasons = []
    for season in repository.available_seasons():
        if season > selected:
            continue
        record = repository.team_record(season, team)
        if record and (record["wins"] + record["losses"] + record["ties"]):
            seasons.append(season)
        if len(seasons) >= limit:
            break
    return seasons


OFFENSE_COLUMNS = (
    Column("season", "Season", "text", emphasis=True, group="Record"),
    Column("games", "G", "int", group="Record"),
    Column("record", "W-L-T", "text", group="Record"),
    Column("win_pct", "Win %", "rate", group="Record"),
    Column("pf", "PF", "int", group="Record"),
    Column("pa", "PA", "int", group="Record"),
    Column("diff", "Diff", "signed", group="Record"),
    Column("plays_per_game", "Plays/G", "f1", group="Volume & pace"),
    Column("drives_per_game", "Drives/G", "f1", group="Volume & pace"),
    Column("seconds_per_play", "Sec/play", "f1", group="Volume & pace"),
    Column("third_down_rate", "3rd %", "rate", group="Volume & pace"),
    Column("red_zone_success_rate", "RZ succ %", "rate", group="Volume & pace"),
    Column("epa_per_play", "EPA/P", "signed2", group="Offensive efficiency"),
    Column("pass_epa_per_play", "EPA/DB", "signed2", group="Offensive efficiency"),
    Column("rush_epa_per_play", "EPA/RU", "signed2", group="Offensive efficiency"),
    Column("success_rate", "Succ %", "rate", group="Offensive efficiency"),
    Column("explosive_rate", "Expl %", "rate", group="Offensive efficiency"),
    Column("turnovers_per_game", "TO/G", "f1", group="Offensive efficiency"),
)

DEFENSE_COLUMNS = (
    Column("season", "Season", "text", emphasis=True, group="Record"),
    Column("games", "G", "int", group="Record"),
    Column("pa_per_game", "PA/G", "f1", group="Record"),
    Column("takeaways_per_game", "TKA/G", "f1", group="Record"),
    Column("pass_yards", "Pass yd/G", "f1", group="Yards allowed"),
    Column("rush_yards", "Rush yd/G", "f1", group="Yards allowed"),
    Column("ypa", "Y/A", "f2", group="Yards allowed"),
    Column("ypc", "Y/C", "f2", group="Yards allowed"),
    Column("first_downs", "1st/G", "f1", group="Yards allowed"),
    Column("epa_allowed", "EPA/P", "signed2", group="Efficiency allowed"),
    Column("pass_epa_allowed", "EPA/DB", "signed2", group="Efficiency allowed"),
    Column("rush_epa_allowed", "EPA/RU", "signed2", group="Efficiency allowed"),
    Column("success_allowed", "Succ %", "rate", group="Efficiency allowed"),
    Column("explosive_allowed", "Expl %", "rate", group="Efficiency allowed"),
    Column("sacks_per_game", "Sack/G", "f1", group="Disruption"),
    Column("qb_hits_per_game", "QBH/G", "f1", group="Disruption"),
    Column("tfl_per_game", "TFL/G", "f1", group="Disruption"),
    Column("pd_per_game", "PD/G", "f1", group="Disruption"),
)


def team_history_tables(repository: NFLRepository, team: str, selected: int,
                        limit: int = 6) -> tuple[Table, Table]:
    offense_rows: list[dict[str, Any]] = []
    defense_rows: list[dict[str, Any]] = []
    for season in team_history_seasons(repository, team, selected, limit):
        record = repository.team_record(season, team) or {}
        summary = repository.team_season_summary(season, team) or {}
        situational = dict(repository.team_situational_profile(season, team) or {})
        efficiency = next((row for row in repository.league_efficiency(season) if row["team"] == team), {})
        defensive = next((row for row in repository.league_defensive_summary(season) if row["team"] == team), {})
        games = record.get("wins", 0) + record.get("losses", 0) + record.get("ties", 0)
        offense_rows.append(_selected({
            "season": str(season), "games": games, "record": record.get("record"),
            "win_pct": record.get("win_pct"), "pf": record.get("points_for"),
            "pa": record.get("points_against"), "diff": record.get("point_diff"),
            "plays_per_game": situational.get("plays_per_game"),
            "drives_per_game": situational.get("drives_per_game"),
            "seconds_per_play": situational.get("seconds_per_play"),
            "third_down_rate": situational.get("third_down_rate"),
            "red_zone_success_rate": situational.get("red_zone_success_rate"),
            "epa_per_play": efficiency.get("epa_per_play"),
            "pass_epa_per_play": efficiency.get("pass_epa_per_play"),
            "rush_epa_per_play": efficiency.get("rush_epa_per_play"),
            "success_rate": efficiency.get("success_rate"),
            "explosive_rate": efficiency.get("explosive_rate"),
            "turnovers_per_game": summary.get("turnovers_per_game"),
        }, season, selected))
        defense_rows.append(_selected({
            "season": str(season), "games": games,
            "pa_per_game": defensive.get("points_allowed_per_game"),
            "takeaways_per_game": defensive.get("takeaways_per_game"),
            "pass_yards": defensive.get("pass_yards_allowed_per_game"),
            "rush_yards": defensive.get("rush_yards_allowed_per_game"),
            "ypa": defensive.get("pass_yards_per_attempt_allowed"),
            "ypc": defensive.get("rush_yards_per_carry_allowed"),
            "first_downs": defensive.get("first_downs_allowed_per_game"),
            "epa_allowed": efficiency.get("defensive_epa_allowed"),
            "pass_epa_allowed": efficiency.get("defensive_pass_epa_allowed"),
            "rush_epa_allowed": efficiency.get("defensive_rush_epa_allowed"),
            "success_allowed": efficiency.get("defensive_success_allowed"),
            "explosive_allowed": efficiency.get("defensive_explosive_allowed"),
            "sacks_per_game": defensive.get("sacks_per_game"),
            "qb_hits_per_game": defensive.get("qb_hits_per_game"),
            "tfl_per_game": defensive.get("tackles_for_loss_per_game"),
            "pd_per_game": defensive.get("passes_defended_per_game"),
        }, season, selected))
    return (Table(OFFENSE_COLUMNS, offense_rows, empty="No completed seasons are stored for this team.", dense=True),
            Table(DEFENSE_COLUMNS, defense_rows, empty="No completed seasons are stored for this team.", dense=True))


# ---- player season bands -----------------------------------------------------------------------

# band -> (label, [(column key, label, format, source metric or callable)])
def _div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


PLAYER_BANDS = (
    ("Passing", "attempts", (
        ("cmp", "CMP", "int", lambda t: t["completions"]),
        ("att", "ATT", "int", lambda t: t["attempts"]),
        ("cmp_pct", "CMP %", "rate", lambda t: _div(t["completions"], t["attempts"])),
        ("pass_yds", "YDS", "big", lambda t: t["passing_yards"]),
        ("ypa", "Y/A", "f2", lambda t: _div(t["passing_yards"], t["attempts"])),
        ("pass_td", "TD", "int", lambda t: t["passing_tds"]),
        ("int", "INT", "int", lambda t: t["passing_interceptions"]),
        ("sacks", "SACK", "int", lambda t: t["sacks_suffered"]),
        ("pass_epa", "EPA", "f1", lambda t: t["passing_epa"]),
    )),
    ("Rushing", "carries", (
        ("car", "ATT", "int", lambda t: t["carries"]),
        ("rush_yds", "YDS", "big", lambda t: t["rushing_yards"]),
        ("ypc", "Y/C", "f2", lambda t: _div(t["rushing_yards"], t["carries"])),
        ("rush_td", "TD", "int", lambda t: t["rushing_tds"]),
        ("rush_epa", "EPA", "f1", lambda t: t["rushing_epa"]),
    )),
    ("Receiving", "targets", (
        ("tgt", "TGT", "int", lambda t: t["targets"]),
        ("rec", "REC", "int", lambda t: t["receptions"]),
        ("rec_yds", "YDS", "big", lambda t: t["receiving_yards"]),
        ("ypr", "Y/R", "f1", lambda t: _div(t["receiving_yards"], t["receptions"])),
        ("rec_td", "TD", "int", lambda t: t["receiving_tds"]),
        ("yac", "YAC", "big", lambda t: t["receiving_yards_after_catch"]),
    )),
    ("Fantasy (PPR)", "fantasy_points_ppr", (
        ("fp", "FP", "f1", lambda t: t["fantasy_points_ppr"]),
        ("fpg", "FP/G", "f1", lambda t: _div(t["fantasy_points_ppr"], t["games"])),
    )),
)

_SUMMED = ("completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions",
           "sacks_suffered", "passing_epa", "carries", "rushing_yards", "rushing_tds", "rushing_epa",
           "targets", "receptions", "receiving_yards", "receiving_tds", "receiving_yards_after_catch",
           "fantasy_points_ppr")


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {key: 0.0 for key in _SUMMED}
    for row in rows:
        for key in _SUMMED:
            totals[key] += float(row.get(key) or 0)
    totals["games"] = float(len({row.get("game_id") for row in rows}))
    return totals


def player_season_table(weekly_rows: list[dict[str, Any]], selected: int,
                        *, playoffs: bool = False) -> Table:
    """All-bands season table from a player's career weekly rows (regular season by default)."""
    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in weekly_rows:
        if not playoffs and (row.get("season_type") or "REG") != "REG":
            continue
        by_season[int(row["season"])].append(row)
    if not by_season:
        return Table((), [], empty="No season statistics are stored for this player.")
    totals = {season: _aggregate(rows) for season, rows in by_season.items()}
    career = _aggregate([row for rows in by_season.values() for row in rows])
    columns = [Column("season", "Season", "text", emphasis=True, group="Season"),
               Column("team", "Team", "text", group="Season"),
               Column("games", "G", "int", group="Season")]
    bands = []
    for label, gate, spec in PLAYER_BANDS:
        if not any(total.get(gate) for total in (*totals.values(), career)):
            continue
        bands.append((label, spec))
        columns.extend(Column(key, name, fmt, group=label) for key, name, fmt, _ in spec)
    table_rows = []
    for season in sorted(totals, reverse=True):
        teams = list(dict.fromkeys(row.get("team") for row in by_season[season] if row.get("team")))
        row = {"season": str(season), "team": "/".join(teams), "games": int(totals[season]["games"])}
        for _, spec in bands:
            for key, _, _, compute in spec:
                row[key] = compute(totals[season])
        table_rows.append(_selected(row, season, selected))
    total_row = {"season": "Career", "team": "", "games": int(career["games"])}
    for _, spec in bands:
        for key, _, _, compute in spec:
            total_row[key] = compute(career)
    return Table(columns, table_rows, total_row=total_row, dense=True,
                 empty="No season statistics are stored for this player.")
