"""League-wide player stat exploration: a sortable table plus a scatter builder.

`player_season_stats` in repository.py pivots the long weekly-stat table into
one wide row per player; everything here just formats that for the page --
picking which columns to show, deriving the handful of rate stats that can't
be summed across weeks, and projecting two chosen metrics into scatter
coordinates.
"""

from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.charts import COLORS
from sports_aggregator.tables import Column, Table

# (key, label, format, category). Category groups the metric picker; rate
# metrics are derived after aggregation (see with_rates), not summed --
# a per-game rate summed across weeks is not the season rate.
#
# This is nflverse's full weekly-stats metric set (player_weekly_stats
# stores ~138 distinct metric names), minus a handful that a straight
# SUM() across weeks would misrepresent and that don't have a clean
# season-level recomputation from other stored sums:
#   - passing_cpoe, target_share, air_yards_share, wopr: already
#     per-game ratios/composites; nflverse computes them from play-level
#     or team-level context this table doesn't retain, so there is no
#     honest way to turn them into a season number here (a naive sum
#     would just be wrong, not merely imprecise).
#   - fg_made_list/fg_missed_list/fg_blocked_list and the *_distance
#     variants: these carry one made/missed/blocked kick's distance per
#     row, not a summable count or total -- "sum of distances across a
#     season" isn't a real stat. fg_made_0_19..60_ (below) are the
#     honest summable version: a count of kicks in that distance band.
METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("games", "Games", "int", "General"),

    ("attempts", "Att", "int", "Passing"),
    ("completions", "Cmp", "int", "Passing"),
    ("completion_pct", "Cmp %", "rate", "Passing"),
    ("passing_yards", "Pass yds", "big", "Passing"),
    ("yards_per_attempt", "Yds/att", "f1", "Passing"),
    ("passing_tds", "Pass TD", "int", "Passing"),
    ("passing_interceptions", "Pass INT", "int", "Passing"),
    ("passing_epa", "Pass EPA", "signed2", "Passing"),
    ("passing_air_yards", "Pass air yds", "big", "Passing"),
    ("passing_yards_after_catch", "Pass YAC", "big", "Passing"),
    ("passing_first_downs", "Pass 1D", "int", "Passing"),
    ("passing_2pt_conversions", "Pass 2PT", "int", "Passing"),
    ("pacr", "PACR", "rate", "Passing"),
    ("passing_10", "Pass 10+ yd", "int", "Passing"),
    ("passing_16", "Pass 16+ yd", "int", "Passing"),
    ("passing_20", "Pass 20+ yd", "int", "Passing"),
    ("passing_40", "Pass 40+ yd", "int", "Passing"),

    ("carries", "Car", "int", "Rushing"),
    ("rushing_yards", "Rush yds", "big", "Rushing"),
    ("yards_per_carry", "Yds/car", "f1", "Rushing"),
    ("rushing_tds", "Rush TD", "int", "Rushing"),
    ("rushing_epa", "Rush EPA", "signed2", "Rushing"),
    ("rushing_first_downs", "Rush 1D", "int", "Rushing"),
    ("rushing_fumbles", "Rush Fum", "int", "Rushing"),
    ("rushing_fumbles_lost", "Rush Fum Lost", "int", "Rushing"),
    ("rushing_2pt_conversions", "Rush 2PT", "int", "Rushing"),
    ("rushing_10", "Rush 10+ yd", "int", "Rushing"),
    ("rushing_12", "Rush 12+ yd", "int", "Rushing"),
    ("rushing_20", "Rush 20+ yd", "int", "Rushing"),
    ("rushing_40", "Rush 40+ yd", "int", "Rushing"),

    ("targets", "Tgt", "int", "Receiving"),
    ("receptions", "Rec", "int", "Receiving"),
    ("catch_rate", "Catch %", "rate", "Receiving"),
    ("receiving_yards", "Rec yds", "big", "Receiving"),
    ("yards_per_reception", "Yds/rec", "f1", "Receiving"),
    ("receiving_tds", "Rec TD", "int", "Receiving"),
    ("receiving_epa", "Rec EPA", "signed2", "Receiving"),
    ("receiving_air_yards", "Rec air yds", "big", "Receiving"),
    ("receiving_yards_after_catch", "YAC", "big", "Receiving"),
    ("receiving_first_downs", "Rec 1D", "int", "Receiving"),
    ("receiving_fumbles", "Rec Fum", "int", "Receiving"),
    ("receiving_fumbles_lost", "Rec Fum Lost", "int", "Receiving"),
    ("receiving_2pt_conversions", "Rec 2PT", "int", "Receiving"),
    ("racr", "RACR", "rate", "Receiving"),
    ("receiving_10", "Rec 10+ yd", "int", "Receiving"),
    ("receiving_16", "Rec 16+ yd", "int", "Receiving"),
    ("receiving_20", "Rec 20+ yd", "int", "Receiving"),
    ("receiving_40", "Rec 40+ yd", "int", "Receiving"),

    ("def_tackles_solo", "Solo", "int", "Defense"),
    ("def_tackle_assists", "Ast", "int", "Defense"),
    ("def_tackles_with_assist", "Tkl w/ Ast", "int", "Defense"),
    ("def_tackles_for_loss", "TFL", "int", "Defense"),
    ("def_tackles_for_loss_yards", "TFL yds", "int", "Defense"),
    ("def_sacks", "Sacks", "f1", "Defense"),
    ("def_sack_yards", "Sack yds", "int", "Defense"),
    ("def_qb_hits", "QB hits", "int", "Defense"),
    ("def_interceptions", "Def INT", "int", "Defense"),
    ("def_interception_yards", "Def INT yds", "int", "Defense"),
    ("def_pass_defended", "PD", "int", "Defense"),
    ("def_fumbles", "Def Fum", "int", "Defense"),
    ("def_fumbles_forced", "FF", "int", "Defense"),
    ("def_tds", "Def TD", "int", "Defense"),
    ("def_safeties", "Safeties", "int", "Defense"),
    ("def_pat_blocks", "PAT Blk", "int", "Defense"),
    ("def_fg_blocks", "FG Blk", "int", "Defense"),
    ("def_punt_blocks", "Punt Blk", "int", "Defense"),
    ("def_2pt_atts", "Def 2PT Att", "int", "Defense"),
    ("def_2pt_made", "Def 2PT Made", "int", "Defense"),

    ("fg_made", "FGM", "int", "Kicking"),
    ("fg_att", "FGA", "int", "Kicking"),
    ("fg_pct", "FG %", "rate", "Kicking"),
    ("fg_missed", "FG Missed", "int", "Kicking"),
    ("fg_blocked", "FG Blocked", "int", "Kicking"),
    ("fg_long", "FG Long", "int", "Kicking"),
    ("fg_made_0_19", "FGM 0-19", "int", "Kicking"),
    ("fg_made_20_29", "FGM 20-29", "int", "Kicking"),
    ("fg_made_30_39", "FGM 30-39", "int", "Kicking"),
    ("fg_made_40_49", "FGM 40-49", "int", "Kicking"),
    ("fg_made_50_59", "FGM 50-59", "int", "Kicking"),
    ("fg_made_60_", "FGM 60+", "int", "Kicking"),
    ("fg_missed_0_19", "FG Miss 0-19", "int", "Kicking"),
    ("fg_missed_20_29", "FG Miss 20-29", "int", "Kicking"),
    ("fg_missed_30_39", "FG Miss 30-39", "int", "Kicking"),
    ("fg_missed_40_49", "FG Miss 40-49", "int", "Kicking"),
    ("fg_missed_50_59", "FG Miss 50-59", "int", "Kicking"),
    ("fg_missed_60_", "FG Miss 60+", "int", "Kicking"),
    ("pat_made", "XPM", "int", "Kicking"),
    ("pat_att", "XPA", "int", "Kicking"),
    ("pat_pct", "XP %", "rate", "Kicking"),
    ("pat_missed", "XP Missed", "int", "Kicking"),
    ("pat_blocked", "XP Blocked", "int", "Kicking"),
    ("gwfg_att", "GW FGA", "int", "Kicking"),
    ("gwfg_made", "GW FGM", "int", "Kicking"),
    ("gwfg_missed", "GW FG Missed", "int", "Kicking"),
    ("gwfg_blocked", "GW FG Blocked", "int", "Kicking"),

    ("pt_att", "Punts", "int", "Punting"),
    ("pt_yards", "Punt yds", "big", "Punting"),
    ("pt_net_yards", "Punt Net yds", "big", "Punting"),
    ("pt_long", "Punt Long", "int", "Punting"),
    ("pt_inside_20", "Punt In-20", "int", "Punting"),
    ("pt_touchback", "Punt TB", "int", "Punting"),
    ("pt_downed", "Punt Downed", "int", "Punting"),
    ("pt_fair_caught", "Punt Fair Caught", "int", "Punting"),
    ("pt_out_of_bounds", "Punt OOB", "int", "Punting"),
    ("pt_blocked", "Punt Blocked", "int", "Punting"),
    ("pt_returned", "Punt Returned", "int", "Punting"),
    ("pt_return_yards", "Punt Ret yds allowed", "int", "Punting"),
    ("pt_return_tds", "Punt Ret TD allowed", "int", "Punting"),

    ("kickoff_returns", "KR", "int", "Returns"),
    ("kickoff_return_yards", "KR yds", "big", "Returns"),
    ("punt_returns", "PR", "int", "Returns"),
    ("punt_return_yards", "PR yds", "big", "Returns"),
    ("special_teams_tds", "ST TD", "int", "Returns"),

    ("fumbles_total", "Fumbles", "int", "Fumbles"),
    ("fumbles_lost_total", "Fumbles Lost", "int", "Fumbles"),
    ("fumbles_not_forced", "Fumbles Unforced", "int", "Fumbles"),
    ("fumbles_out_of_bounds", "Fumbles OOB", "int", "Fumbles"),
    ("fumbles_forced_by_opp", "Fumbles Forced (opp)", "int", "Fumbles"),
    ("fumble_recovery_own", "Fum Rec (own)", "int", "Fumbles"),
    ("fumble_recovery_opp", "Fum Rec (opp)", "int", "Fumbles"),
    ("fumble_recovery_yards_own", "Fum Rec yds (own)", "int", "Fumbles"),
    ("fumble_recovery_yards_opp", "Fum Rec yds (opp)", "int", "Fumbles"),
    ("fumble_recovery_tds", "Fum Rec TD", "int", "Fumbles"),
    ("sack_fumbles", "Sack Fum", "int", "Fumbles"),
    ("sack_fumbles_lost", "Sack Fum Lost", "int", "Fumbles"),
    ("sacks_suffered", "Sacks Taken", "int", "Fumbles"),
    ("sack_yards_lost", "Sack yds Lost", "int", "Fumbles"),

    ("penalties", "Penalties", "int", "General"),
    ("penalty_yards", "Penalty yds", "int", "General"),
    ("misc_yards", "Misc yds", "int", "General"),

    ("fantasy_points", "Fantasy", "f1", "Fantasy"),
    ("fantasy_points_ppr", "Fantasy (PPR)", "f1", "Fantasy"),
)
_RATE_METRICS = {"completion_pct", "yards_per_attempt", "yards_per_carry",
                 "yards_per_reception", "catch_rate", "pacr", "racr", "fg_pct", "pat_pct"}
#: Metrics fetched straight from the database via SUM(); rate metrics above
#: are computed from these afterward.
SUM_METRICS = tuple(key for key, *_ in METRICS if key not in _RATE_METRICS and key != "games")
METRIC_LABELS = {key: label for key, label, _fmt, _category in METRICS}
METRIC_FORMATS = {key: fmt for key, _label, fmt, _category in METRICS}
METRIC_CATEGORIES = tuple(dict.fromkeys(category for *_, category in METRICS))

_POSITION_GROUPS = (
    ("QB", {"QB"}), ("RB", {"RB", "FB"}), ("WR/TE", {"WR", "TE"}),
    ("OL", {"OT", "OG", "C", "OL", "LT", "LG", "RG", "RT", "T", "G"}),
    ("DL/EDGE", {"EDGE", "DE", "DT", "NT", "DL"}),
    ("LB", {"LB", "ILB", "OLB", "MLB"}),
    ("DB", {"CB", "DB", "S", "FS", "SS", "NB"}),
    ("ST", {"K", "P", "LS"}),
)


def _position_color(position: str | None) -> str:
    position = (position or "").upper()
    for index, (_label, members) in enumerate(_POSITION_GROUPS):
        if position in members:
            return COLORS[index % len(COLORS)]
    return "#8296a4"


def _safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    return numerator / denominator if numerator is not None and denominator else None


def with_rates(row: dict[str, Any]) -> dict[str, Any]:
    """Add the derived rate columns a straight SUM() cannot produce."""
    row["completion_pct"] = _safe_divide(row.get("completions"), row.get("attempts"))
    row["yards_per_attempt"] = _safe_divide(row.get("passing_yards"), row.get("attempts"))
    row["yards_per_carry"] = _safe_divide(row.get("rushing_yards"), row.get("carries"))
    row["yards_per_reception"] = _safe_divide(row.get("receiving_yards"), row.get("receptions"))
    row["catch_rate"] = _safe_divide(row.get("receptions"), row.get("targets"))
    # Recomputed from the season's own summed yardage rather than averaging
    # nflverse's per-game ratios -- the same reasoning as every rate above.
    row["pacr"] = _safe_divide(row.get("passing_yards"), row.get("passing_air_yards"))
    row["racr"] = _safe_divide(row.get("receiving_yards"), row.get("receiving_air_yards"))
    row["fg_pct"] = _safe_divide(row.get("fg_made"), row.get("fg_att"))
    row["pat_pct"] = _safe_divide(row.get("pat_made"), row.get("pat_att"))
    return row


def player_stat_table(rows: list[dict[str, Any]], *, season: int) -> Table:
    columns = [Column("player_name", "Player"), Column("team", "Team"), Column("position", "Pos.")]
    columns += [Column(key, label, fmt) for key, label, fmt, _category in METRICS]
    projected = []
    for row in rows:
        item = dict(row)
        item["player_name_url"] = f"/nfl/players/{item['player_id']}/?season={season}"
        item["team_url"] = f"/nfl/teams/{item['team']}/?season={season}"
        projected.append(item)
    return Table(tuple(columns), projected, caption=f"{season} player stats", dense=True,
                note="Every metric is a season total except the rate columns, which are computed "
                     "from the underlying totals rather than averaged week to week.",
                empty="No players match this filter.")


def scatter_plot(rows: list[dict[str, Any]], x_key: str, y_key: str, *,
                 color_by: str = "position",
                 team_colors: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """Pair two chosen metrics into one point per player for a Chart.js scatter.

    `color_by="team"` needs `team_colors`: {abbreviation: (color, alternate)}.
    Each dot then fills with the team's primary color and rings with its
    alternate, instead of the default position-group coloring. Axis scaling
    and tick density are left to Chart.js -- this just shapes the points.
    """
    x_label, y_label = METRIC_LABELS.get(x_key, x_key), METRIC_LABELS.get(y_key, y_key)
    x_format, y_format = METRIC_FORMATS.get(x_key, "f1"), METRIC_FORMATS.get(y_key, "f1")
    points = [row for row in rows if row.get(x_key) is not None and row.get(y_key) is not None]
    empty = {"points": [], "x_key": x_key, "y_key": y_key, "x_label": x_label,
             "y_label": y_label, "x_format": x_format, "y_format": y_format}
    if not points:
        return empty
    team_colors = team_colors or {}

    def dot_colors(point: dict[str, Any]) -> tuple[str, str]:
        if color_by == "team":
            fill, ring = team_colors.get(point["team"], ("#8296a4", "#0b1319"))
            return fill, ring or fill
        color = _position_color(point.get("position"))
        return color, "#0b1319"

    plotted = []
    for point in points:
        fill, ring = dot_colors(point)
        plotted.append({
            "player_id": point["player_id"], "player_name": point["player_name"],
            "team": point["team"], "position": point.get("position"),
            "color": fill, "ring_color": ring,
            "x": point[x_key], "y": point[y_key],
        })
    return {"points": plotted, "x_key": x_key, "y_key": y_key, "x_label": x_label,
            "y_label": y_label, "x_format": x_format, "y_format": y_format}
