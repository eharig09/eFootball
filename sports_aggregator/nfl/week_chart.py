"""Server-side SVG geometry for the weekly EPA/play line chart with a point-differential strip."""

from __future__ import annotations

from typing import Any

WIDTH, HEIGHT = 760, 300
LEFT, RIGHT, TOP = 46, 16, 14
LINE_BOTTOM = 214        # bottom of the EPA plot
BARS_TOP, BARS_BOTTOM = 232, 278


def _nice_limit(peak: float) -> float:
    for limit in (0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0):
        if peak <= limit:
            return limit
    return float(int(peak) + 1)


def epa_week_chart(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Offense EPA/play and EPA/play allowed by week, plus point margin bars.

    `rows` are team_weekly_performance() rows. Weeks are placed by week number so a bye
    shows as a gap rather than being closed up.
    """
    usable = [row for row in rows if row.get("epa_per_play") is not None
              and row.get("defensive_epa_allowed") is not None]
    if not usable:
        return {"has_data": False}
    weeks = [int(row["week"]) for row in usable]
    first, last = min(weeks), max(weeks)
    span = max(last - first, 1)
    plot_width = WIDTH - LEFT - RIGHT

    def x_of(week: int) -> float:
        return round(LEFT + (week - first) / span * plot_width if last != first else LEFT + plot_width / 2, 1)

    peak = max(abs(row["epa_per_play"]) for row in usable)
    peak = max(peak, max(abs(row["defensive_epa_allowed"]) for row in usable))
    limit = _nice_limit(peak)
    plot_height = LINE_BOTTOM - TOP

    def y_of(value: float) -> float:
        return round(TOP + (limit - value) / (2 * limit) * plot_height, 1)

    def series(key: str) -> tuple[str, list[dict[str, Any]]]:
        points = [{"x": x_of(int(row["week"])), "y": y_of(row[key]), "week": int(row["week"]),
                   "value": row[key], "opponent": row.get("opponent"),
                   "url": f"/nfl/games/{row['game_id']}/" if row.get("game_id") else None}
                  for row in usable]
        return " ".join(f"{p['x']},{p['y']}" for p in points), points

    offense_path, offense_points = series("epa_per_play")
    defense_path, defense_points = series("defensive_epa_allowed")

    margins = [row.get("point_margin") for row in usable if row.get("point_margin") is not None]
    max_margin = max((abs(margin) for margin in margins), default=0) or 1
    zero_y = round((BARS_TOP + BARS_BOTTOM) / 2, 1)
    half = (BARS_BOTTOM - BARS_TOP) / 2
    bar_width = round(min(26, plot_width / max(len(usable), 1) * 0.55), 1)
    bars = []
    for row in usable:
        margin = row.get("point_margin")
        if margin is None:
            continue
        height = max(round(abs(margin) / max_margin * half, 1), 1.5)
        x = x_of(int(row["week"]))
        bars.append({"x": round(x - bar_width / 2, 1), "y": zero_y - height if margin > 0 else zero_y,
                     "w": bar_width, "h": height, "positive": margin > 0, "margin": margin,
                     "week": int(row["week"]), "opponent": row.get("opponent")})

    ticks = [{"y": y_of(value), "label": f"{value:+.2f}" if value else "0.00"}
             for value in (limit, limit / 2, 0.0, -limit / 2, -limit)]
    return {
        "has_data": True, "width": WIDTH, "height": HEIGHT, "left": LEFT, "right": WIDTH - RIGHT,
        "zero_y": y_of(0.0), "bars_zero_y": zero_y, "ticks": ticks,
        "week_labels": [{"x": x_of(week), "label": week} for week in sorted(set(weeks))],
        "offense_path": offense_path, "defense_path": defense_path,
        "offense_points": offense_points, "defense_points": defense_points,
        "bars": bars, "label_y": HEIGHT - 4,
    }
