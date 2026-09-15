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
METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("games", "Games", "int", "General"),
    ("attempts", "Att", "int", "Passing"),
    ("completions", "Cmp", "int", "Passing"),
    ("completion_pct", "Cmp %", "rate", "Passing"),
    ("passing_yards", "Pass yds", "big", "Passing"),
    ("yards_per_attempt", "Yds/att", "f1", "Passing"),
    ("passing_tds", "Pass TD", "int", "Passing"),
    ("passing_interceptions", "INT", "int", "Passing"),
    ("passing_epa", "Pass EPA", "signed2", "Passing"),
    ("passing_air_yards", "Air yds", "big", "Passing"),
    ("carries", "Car", "int", "Rushing"),
    ("rushing_yards", "Rush yds", "big", "Rushing"),
    ("yards_per_carry", "Yds/car", "f1", "Rushing"),
    ("rushing_tds", "Rush TD", "int", "Rushing"),
    ("rushing_epa", "Rush EPA", "signed2", "Rushing"),
    ("rushing_first_downs", "Rush 1D", "int", "Rushing"),
    ("targets", "Tgt", "int", "Receiving"),
    ("receptions", "Rec", "int", "Receiving"),
    ("catch_rate", "Catch %", "rate", "Receiving"),
    ("receiving_yards", "Rec yds", "big", "Receiving"),
    ("yards_per_reception", "Yds/rec", "f1", "Receiving"),
    ("receiving_tds", "Rec TD", "int", "Receiving"),
    ("receiving_epa", "Rec EPA", "signed2", "Receiving"),
    ("receiving_air_yards", "Air yds", "big", "Receiving"),
    ("receiving_yards_after_catch", "YAC", "big", "Receiving"),
    ("def_tackles_solo", "Solo", "int", "Defense"),
    ("def_tackle_assists", "Ast", "int", "Defense"),
    ("def_sacks", "Sacks", "f1", "Defense"),
    ("def_interceptions", "INT", "int", "Defense"),
    ("def_tackles_for_loss", "TFL", "int", "Defense"),
    ("def_qb_hits", "QB hits", "int", "Defense"),
    ("def_pass_defended", "PD", "int", "Defense"),
    ("def_fumbles_forced", "FF", "int", "Defense"),
    ("fg_made", "FGM", "int", "Kicking"),
    ("fg_att", "FGA", "int", "Kicking"),
    ("pat_made", "XPM", "int", "Kicking"),
    ("fantasy_points_ppr", "Fantasy (PPR)", "f1", "Fantasy"),
)
_RATE_METRICS = {"completion_pct", "yards_per_attempt", "yards_per_carry",
                 "yards_per_reception", "catch_rate"}
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
