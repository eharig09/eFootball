"""Crisp, dependency-free SVG chart packets for NFL pages."""

from __future__ import annotations

from typing import Any, Iterable

WIDTH, HEIGHT = 720, 260
# RIGHT leaves room for a second labeled axis when two metrics are overlaid;
# templates/_nfl_charts.html mirrors LEFT + PLOT_W as the plot's right edge.
LEFT, RIGHT, TOP, BOTTOM = 58, 54, 22, 38
PLOT_W, PLOT_H = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
COLORS = ("#69c5ff", "#ffb45f", "#78d69b", "#d49cff", "#ff7f8a", "#e7dc68")


def _path(values: list[dict[str, Any]], key: str = "y") -> str:
    return " ".join(("M" if index == 0 else "L") + f" {item['x']:.1f} {item[key]:.1f}"
                    for index, item in enumerate(values))


def with_last_season(current: list[dict[str, Any]], previous: list[dict[str, Any]], *,
                     limit: int = 10) -> list[dict[str, Any]]:
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
           value_format: str = "f1", limit: int = 10, color: str | None = None) -> dict[str, Any]:
    values = [{"week": row.get("week"), "season": row.get("season"),
               "opponent": row.get("opponent") or row.get("opponent_team"),
               "game_id": row.get("game_id"), "value": row.get(key)}
              for row in list(rows)[-limit:] if row.get(key) is not None]
    if not values:
        return {"key": key, "label": label, "format": value_format, "path": "", "values": []}
    # Each metric keeps its own scale -- points scored and, say, EPA per play
    # are not comparable magnitudes, so forcing them onto one shared axis
    # would misrepresent one or both. When several metrics are shown at once
    # the page gives each its own labeled axis instead (see chart_workbench).
    raw_low = min(item["value"] for item in values)
    raw_high = max(item["value"] for item in values)
    padding = max((raw_high - raw_low) * .12, abs(raw_high) * .04, .01)
    low, high = raw_low - padding, raw_high + padding
    spread = high - low or 1
    count = len(values)
    for index, item in enumerate(values):
        item["x"] = LEFT + PLOT_W * index / max(1, count - 1)
        item["y"] = TOP + PLOT_H * (high - item["value"]) / spread
    ticks = [{"value": high - (high - low) * index / 4, "y": TOP + PLOT_H * index / 4}
             for index in range(5)]
    baseline = TOP + PLOT_H * high / spread if low <= 0 <= high else None
    return {"key": key, "label": label, "format": value_format, "path": _path(values),
            "values": values, "low": raw_low, "high": raw_high, "ticks": ticks,
            "baseline": baseline, "color": color}


def _charts(rows: list[dict[str, Any]],
            definitions: tuple[tuple[str, str, str], ...], *, limit: int = 10) -> list[dict[str, Any]]:
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
    ))


def player_charts(rows: list[dict[str, Any]], position: str | None) -> list[dict[str, Any]]:
    position = (position or "").upper()
    if position == "QB":
        definitions = (("passing_yards", "Passing yards", "big"), ("passing_epa", "Passing EPA", "signed2"),
                       ("passing_air_yards", "Air yards", "big"), ("attempts", "Attempts", "int"),
                       ("completions", "Completions", "int"))
    elif position in {"RB", "FB"}:
        definitions = (("rushing_yards", "Rushing yards", "big"), ("carries", "Carries", "int"),
                       ("targets", "Targets", "int"), ("receiving_yards", "Receiving yards", "big"),
                       ("rushing_epa", "Rushing EPA", "signed2"))
    elif position in {"WR", "TE"}:
        definitions = (("receiving_yards", "Receiving yards", "big"), ("targets", "Targets", "int"),
                       ("receptions", "Receptions", "int"), ("receiving_air_yards", "Air yards", "big"),
                       ("receiving_yards_after_catch", "Yards after catch", "big"),
                       ("receiving_epa", "Receiving EPA", "signed2"))
    else:
        definitions = (("def_tackles_solo", "Solo tackles", "int"), ("def_qb_hits", "QB hits", "int"),
                       ("def_sacks", "Sacks", "f1"), ("def_tackles_for_loss", "Tackles for loss", "f1"),
                       ("def_interceptions", "Interceptions", "int"))
    return _charts(rows, definitions)
