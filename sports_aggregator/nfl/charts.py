"""NFL chart data packets.

Both the weekly-form line charts and the stat explorer's scatter plot render
client-side with Chart.js (see templates/_nfl_charts.html), which handles
axis scaling and tick density natively -- so this module's job is just
shaping values, not computing pixel geometry.
"""

from __future__ import annotations

from typing import Any, Iterable

COLORS = ("#69c5ff", "#ffb45f", "#78d69b", "#d49cff", "#ff7f8a", "#e7dc68")
# A full NFL regular season, so a mature season's chart reads as one
# continuous trend instead of a rolling 10-game window.
DISPLAY_WEEKS = 17

# Axis families are deliberately narrower than data types. Attempts and
# touchdowns are both integer counts, for example, but putting 35 attempts
# and two touchdowns on one scale would flatten the touchdown series. Metrics
# share an axis only when their units and useful game-to-game ranges align.
AXES = {
    "points": {"key": "points", "label": "Points", "format": "int"},
    "epa_play": {"key": "epa_play", "label": "EPA / play", "format": "signed2"},
    "rate": {"key": "rate", "label": "Rate", "format": "rate"},
    "yards": {"key": "yards", "label": "Yards", "format": "big"},
    "epa_total": {"key": "epa_total", "label": "Total EPA", "format": "signed2"},
    "workload": {"key": "workload", "label": "Opportunities", "format": "int"},
    "scoring_events": {"key": "scoring_events", "label": "TDs / turnovers", "format": "int"},
    "percentage": {"key": "percentage", "label": "Percentage", "format": "pct"},
    "seconds": {"key": "seconds", "label": "Seconds", "format": "f1"},
    "tracking_yards": {"key": "tracking_yards", "label": "Tracking yards", "format": "signed2"},
    "defensive_events": {"key": "defensive_events", "label": "Defensive events", "format": "f1"},
}


def with_last_season(current: list[dict[str, Any]], previous: list[dict[str, Any]], *,
                     limit: int = DISPLAY_WEEKS) -> list[dict[str, Any]]:
    """Splice in trailing games from last season until there are `limit` points.

    Early in a season a form chart otherwise has one or two points -- not
    enough to show a trend. This keeps the display window full by borrowing
    from the end of last season's series (already in chronological order)
    until this season's own games catch up.
    """
    needed = limit - len(current)
    if needed <= 0 or not previous:
        return current
    return previous[-needed:] + current


def series(rows: Iterable[dict[str, Any]], key: str, label: str, *,
           value_format: str = "f1", axis: dict[str, str] | None = None,
           limit: int = DISPLAY_WEEKS, color: str | None = None) -> dict[str, Any]:
    values = [{"week": row.get("week"), "season": row.get("season"),
               "opponent": row.get("opponent") or row.get("opponent_team"),
               "game_id": row.get("game_id"), "value": row.get(key)}
              for row in list(rows)[-limit:] if row.get(key) is not None]
    # The most recent point's season is "this season" for labeling purposes;
    # an older point gets a "25·" year prefix so a chart that reaches back
    # into last season's tail (see with_last_season) still reads clearly.
    reference_season = values[-1]["season"] if values else None
    for item in values:
        prefix = (f"{item['season'] % 100}·" if item.get("season") and reference_season
                  and item["season"] != reference_season else "")
        item["week_label"] = f"{prefix}W{item['week']}"
    return {"key": key, "label": label, "format": value_format, "axis": axis,
            "color": color, "values": values}


def _charts(rows: list[dict[str, Any]],
            definitions: tuple[tuple[str, str, str, str], ...], *,
            limit: int = DISPLAY_WEEKS) -> list[dict[str, Any]]:
    output = []
    for index, (key, label, value_format, axis_key) in enumerate(definitions):
        chart = series(rows, key, label, value_format=value_format, limit=limit,
                       axis=AXES[axis_key], color=COLORS[index % len(COLORS)])
        if chart["values"]:
            output.append(chart)
    return output


def team_charts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _charts(rows, (
        ("point_margin", "Point margin", "signed", "points"),
        ("points", "Points scored", "f1", "points"),
        ("points_allowed", "Points allowed", "f1", "points"),
        ("epa_per_play", "EPA per play", "signed2", "epa_play"),
        ("success_rate", "Success rate", "rate", "rate"),
        ("pass_epa_per_play", "Dropback EPA", "signed2", "epa_play"),
        ("rush_epa_per_play", "Rush EPA", "signed2", "epa_play"),
        ("explosive_rate", "Explosive rate", "rate", "rate"),
        ("defensive_epa_allowed", "EPA/play allowed", "signed2", "epa_play"),
        ("defensive_success_allowed", "Success rate allowed", "rate", "rate"),
        ("defensive_pass_epa_allowed", "Dropback EPA allowed", "signed2", "epa_play"),
        ("defensive_rush_epa_allowed", "Rush EPA allowed", "signed2", "epa_play"),
        ("defensive_explosive_allowed", "Explosive rate allowed", "rate", "rate"),
    ))


def player_charts(rows: list[dict[str, Any]], position: str | None) -> list[dict[str, Any]]:
    position = (position or "").upper()
    if position == "QB":
        definitions = (("passing_yards", "Passing yards", "big", "yards"),
                       ("passing_epa", "Passing EPA", "signed2", "epa_total"),
                       ("passing_air_yards", "Air yards", "big", "yards"),
                       ("attempts", "Attempts", "int", "workload"),
                       ("completions", "Completions", "int", "workload"),
                       ("passing_tds", "Passing TDs", "int", "scoring_events"),
                       ("passing_interceptions", "Interceptions", "int", "scoring_events"),
                       ("rushing_yards", "Rushing yards", "big", "yards"),
                       ("rushing_epa", "Rushing EPA", "signed2", "epa_total"),
                       ("ngs_pass_cpoe", "CPOE (NGS)", "pct", "percentage"),
                       ("ngs_pass_time_to_throw", "Time to throw", "f1", "seconds"),
                       ("ngs_pass_aggressiveness", "Aggressiveness", "pct", "percentage"))
    elif position in {"RB", "FB"}:
        definitions = (("rushing_yards", "Rushing yards", "big", "yards"),
                       ("carries", "Carries", "int", "workload"),
                       ("targets", "Targets", "int", "workload"),
                       ("receiving_yards", "Receiving yards", "big", "yards"),
                       ("rushing_epa", "Rushing EPA", "signed2", "epa_total"),
                       ("rushing_tds", "Rushing TDs", "int", "scoring_events"),
                       ("rushing_first_downs", "Rushing 1st downs", "int", "workload"),
                       ("receptions", "Receptions", "int", "workload"),
                       ("receiving_epa", "Receiving EPA", "signed2", "epa_total"),
                       ("ngs_rush_yards_over_expected", "Rush yds over expected", "signed2", "tracking_yards"),
                       ("ngs_rush_stacked_box_pct", "Stacked box %", "pct", "percentage"))
    elif position in {"WR", "TE"}:
        definitions = (("receiving_yards", "Receiving yards", "big", "yards"),
                       ("targets", "Targets", "int", "workload"),
                       ("receptions", "Receptions", "int", "workload"),
                       ("receiving_air_yards", "Air yards", "big", "yards"),
                       ("receiving_yards_after_catch", "Yards after catch", "big", "yards"),
                       ("receiving_epa", "Receiving EPA", "signed2", "epa_total"),
                       ("receiving_tds", "Receiving TDs", "int", "scoring_events"),
                       ("rushing_yards", "Rushing yards", "big", "yards"),
                       ("ngs_rec_separation", "Separation", "f1", "tracking_yards"),
                       ("ngs_rec_cushion", "Cushion", "f1", "tracking_yards"),
                       ("ngs_rec_yac_above_expectation", "YAC over expected", "signed2", "tracking_yards"))
    else:
        definitions = (("def_tackles_solo", "Solo tackles", "int", "defensive_events"),
                       ("def_qb_hits", "QB hits", "int", "defensive_events"),
                       ("def_sacks", "Sacks", "f1", "defensive_events"),
                       ("def_tackles_for_loss", "Tackles for loss", "f1", "defensive_events"),
                       ("def_interceptions", "Interceptions", "int", "defensive_events"),
                       ("def_pass_defended", "Passes defended", "int", "defensive_events"),
                       ("def_fumbles_forced", "Forced fumbles", "int", "defensive_events"),
                       ("def_tackle_assists", "Assisted tackles", "int", "defensive_events"))
    return _charts(rows, definitions)
