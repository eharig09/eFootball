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
           value_format: str = "f1", limit: int = DISPLAY_WEEKS, color: str | None = None) -> dict[str, Any]:
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
    return {"key": key, "label": label, "format": value_format, "color": color, "values": values}


def _charts(rows: list[dict[str, Any]],
            definitions: tuple[tuple[str, str, str], ...], *, limit: int = DISPLAY_WEEKS) -> list[dict[str, Any]]:
    output = []
    for index, (key, label, value_format) in enumerate(definitions):
        chart = series(rows, key, label, value_format=value_format, limit=limit,
                       color=COLORS[index % len(COLORS)])
        if chart["values"]:
            output.append(chart)
    return output


def team_charts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _charts(rows, (
        ("point_margin", "Point margin", "signed"), ("points", "Points scored", "f1"),
        ("points_allowed", "Points allowed", "f1"), ("epa_per_play", "EPA per play", "signed2"),
        ("success_rate", "Success rate", "rate"), ("pass_epa_per_play", "Dropback EPA", "signed2"),
        ("rush_epa_per_play", "Rush EPA", "signed2"), ("explosive_rate", "Explosive rate", "rate"),
        ("defensive_epa_allowed", "EPA/play allowed", "signed2"),
        ("defensive_success_allowed", "Success rate allowed", "rate"),
        ("defensive_pass_epa_allowed", "Dropback EPA allowed", "signed2"),
        ("defensive_rush_epa_allowed", "Rush EPA allowed", "signed2"),
        ("defensive_explosive_allowed", "Explosive rate allowed", "rate"),
    ))


def player_charts(rows: list[dict[str, Any]], position: str | None) -> list[dict[str, Any]]:
    position = (position or "").upper()
    if position == "QB":
        definitions = (("passing_yards", "Passing yards", "big"), ("passing_epa", "Passing EPA", "signed2"),
                       ("passing_air_yards", "Air yards", "big"), ("attempts", "Attempts", "int"),
                       ("completions", "Completions", "int"), ("passing_tds", "Passing TDs", "int"),
                       ("passing_interceptions", "Interceptions", "int"),
                       ("rushing_yards", "Rushing yards", "big"), ("rushing_epa", "Rushing EPA", "signed2"))
    elif position in {"RB", "FB"}:
        definitions = (("rushing_yards", "Rushing yards", "big"), ("carries", "Carries", "int"),
                       ("targets", "Targets", "int"), ("receiving_yards", "Receiving yards", "big"),
                       ("rushing_epa", "Rushing EPA", "signed2"), ("rushing_tds", "Rushing TDs", "int"),
                       ("rushing_first_downs", "Rushing 1st downs", "int"),
                       ("receptions", "Receptions", "int"), ("receiving_epa", "Receiving EPA", "signed2"))
    elif position in {"WR", "TE"}:
        definitions = (("receiving_yards", "Receiving yards", "big"), ("targets", "Targets", "int"),
                       ("receptions", "Receptions", "int"), ("receiving_air_yards", "Air yards", "big"),
                       ("receiving_yards_after_catch", "Yards after catch", "big"),
                       ("receiving_epa", "Receiving EPA", "signed2"),
                       ("receiving_tds", "Receiving TDs", "int"),
                       ("rushing_yards", "Rushing yards", "big"))
    else:
        definitions = (("def_tackles_solo", "Solo tackles", "int"), ("def_qb_hits", "QB hits", "int"),
                       ("def_sacks", "Sacks", "f1"), ("def_tackles_for_loss", "Tackles for loss", "f1"),
                       ("def_interceptions", "Interceptions", "int"),
                       ("def_pass_defended", "Passes defended", "int"),
                       ("def_fumbles_forced", "Forced fumbles", "int"),
                       ("def_tackle_assists", "Assisted tackles", "int"))
    return _charts(rows, definitions)
