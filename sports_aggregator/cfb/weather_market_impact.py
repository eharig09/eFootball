"""Does weather actually move the total-points market, and does the game
then actually finish under (or over) what the market expected?

Two separate questions, kept separate on purpose:
1. Does the *market* react to bad weather -- does the total line move down
   (toward an under) between open and close in windy/wet/extreme-temperature
   games more than it does in ordinary ones?
2. Does the *game itself* actually finish lower than the opening total in
   those conditions -- which is really asking whether the market is already
   pricing weather in by kickoff, or whether there's a systematic edge left
   on the table.

Every game is bucketed along one weather dimension at a time (wind,
precipitation, temperature) against a fixed, non-outcome-derived threshold
set, the same "no threshold selected from outcomes" discipline
totals_market_movement.py already uses. A game can appear in more than one
dimension's buckets (a cold, windy game is "cold" in the temperature
buckets and "windy" in the wind buckets) -- these are marginal looks at
one variable at a time, not a joint model.

Reads the closest-to-kickoff game_weather snapshot for each game regardless
of source (a live pregame forecast for recent games, an
open-meteo-archive reading for historical ones -- see weather_backfill.py),
so this naturally covers whatever depth of weather history has actually
been backfilled.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.cfb.repository import CFBRepository

#: Fixed, not outcome-selected. Sustained wind, mph.
WIND_BUCKETS = (
    ("<10", lambda w: w < 10),
    ("10-14.9", lambda w: 10 <= w < 15),
    ("15-19.9", lambda w: 15 <= w < 20),
    ("20-24.9", lambda w: 20 <= w < 25),
    ("25+", lambda w: w >= 25),
)
#: Precipitation at the kickoff-hour reading, inches.
PRECIP_BUCKETS = (
    ("none (<0.01in)", lambda p: p < 0.01),
    ("light (0.01-0.099in)", lambda p: 0.01 <= p < 0.10),
    ("moderate (0.10-0.249in)", lambda p: 0.10 <= p < 0.25),
    ("heavy (0.25in+)", lambda p: p >= 0.25),
)
#: Temperature at kickoff, degrees F.
TEMP_BUCKETS = (
    ("extreme cold (<20F)", lambda t: t < 20),
    ("cold (20-31.9F)", lambda t: 20 <= t < 32),
    ("cool (32-49.9F)", lambda t: 32 <= t < 50),
    ("mild (50-84.9F)", lambda t: 50 <= t < 85),
    ("hot (85-94.9F)", lambda t: 85 <= t < 95),
    ("extreme heat (95F+)", lambda t: t >= 95),
)


def _game_rows(repository: CFBRepository, *, start_season: int, end_season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT g.game_id,g.season,g.week,g.home_team,g.away_team,
                      g.home_points,g.away_points,
                      w.temperature,w.sustained_wind,w.wind_gust,
                      w.precipitation_amount,w.condition,w.source AS weather_source,
                      AVG(gl.over_under_open) AS open_total,
                      AVG(gl.over_under) AS close_total
               FROM games g
               JOIN (
                   SELECT gw.*, ROW_NUMBER() OVER (
                       PARTITION BY gw.game_id
                       ORDER BY ABS(strftime('%s', gw.forecast_generated_at) - strftime('%s', gw.kickoff_time))
                   ) AS rn
                   FROM game_weather gw
               ) w ON w.game_id = g.game_id AND w.rn = 1
               LEFT JOIN game_lines gl ON gl.game_id = g.game_id
               WHERE g.season BETWEEN ? AND ? AND g.completed = 1
                 AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL
               GROUP BY g.game_id""",
            (int(start_season), int(end_season)),
        )]
    out = []
    for row in rows:
        if row["open_total"] is None or row["close_total"] is None:
            continue
        actual_total = float(row["home_points"]) + float(row["away_points"])
        open_total, close_total = float(row["open_total"]), float(row["close_total"])
        row["actual_total"] = actual_total
        row["open_total"] = open_total
        row["close_total"] = close_total
        row["line_movement"] = close_total - open_total  # negative = market moved toward the under
        row["actual_vs_open"] = actual_total - open_total  # negative = game went under the opener
        row["actual_vs_close"] = actual_total - close_total
        out.append(row)
    return out


def _bucket_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    movement = [r["line_movement"] for r in rows]
    vs_open = [r["actual_vs_open"] for r in rows]
    return {
        "n": n,
        "mean_open_total": round(sum(r["open_total"] for r in rows) / n, 2),
        "mean_actual_total": round(sum(r["actual_total"] for r in rows) / n, 2),
        "mean_line_movement": round(sum(movement) / n, 3),
        "pct_line_moved_toward_under": round(sum(1 for m in movement if m < 0) / n, 4),
        "pct_line_moved_toward_over": round(sum(1 for m in movement if m > 0) / n, 4),
        "mean_actual_minus_open": round(sum(vs_open) / n, 3),
        "pct_finished_under_open": round(sum(1 for v in vs_open if v < 0) / n, 4),
        "pct_finished_over_open": round(sum(1 for v in vs_open if v > 0) / n, 4),
    }


def _dimension(rows: list[dict[str, Any]], field: str, buckets: tuple) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        for label, predicate in buckets:
            if predicate(value):
                grouped[label].append(row)
                break
    return {label: _bucket_stats(grouped.get(label, [])) for label, _ in buckets}


def _extremes(rows: list[dict[str, Any]], field: str, *, top: int = 10, reverse: bool = True) -> list[dict[str, Any]]:
    ranked = sorted((r for r in rows if r.get(field) is not None),
                    key=lambda r: r[field], reverse=reverse)[:top]
    return [
        {
            "game_id": r["game_id"], "season": r["season"], "week": r["week"],
            "matchup": f"{r['away_team']} at {r['home_team']}",
            field: r[field], "open_total": r["open_total"], "close_total": r["close_total"],
            "actual_total": r["actual_total"], "actual_vs_open": round(r["actual_vs_open"], 2),
            "weather_source": r["weather_source"],
        }
        for r in ranked
    ]


#: Static historical context for the live weather card, one entry per flag
#: sports_aggregator.providers.weather.weather_flags() can raise. Computed
#: from report(repository, start_season=2015, end_season=2025)'s output at
#: exactly the same thresholds weather_flags() itself fires on (HIGH_WIND_MPH,
#: HEAVY_GUST_MPH, EXTREME_HEAT_F, EXTREME_COLD_F) -- a hand-tuned separate
#: bucket boundary would talk about a different population than the one the
#: badge is actually flagging. Kept as a checked-in static lookup rather than
#: recomputed per page load, the same choice NFL's Football Lab panel made
#: for its own backtest summary: this is a settled research finding, not a
#: live number, and the query behind it scans the whole games/weather/lines
#: history. RAIN_RISK intentionally has no entry -- the same backtest, run at
#: RAIN_RISK's own 0.15in threshold, found only 14 qualifying games and no
#: reliable under bias among them, too thin to say anything a reader should
#: trust. Showing a number there would overstate a pattern that isn't
#: actually supported; re-add it if a future rerun (more seasons, or a
#: rethought threshold) finds a real one.
FLAG_HISTORICAL_CONTEXT: dict[str, str] = {
    "HIGH_WIND": (
        "Since 2015, games with sustained wind at or above 15 mph (138 games) "
        "have finished 1.2 points under the opening total on average, and the "
        "total line has moved down before kickoff 80% of the time."
    ),
    "HEAVY_GUSTS": (
        "Since 2015, games with gusts at or above 25 mph (449 games) have "
        "finished 1.1 points under the opening total on average, and the "
        "total line has moved down before kickoff 71% of the time."
    ),
    "EXTREME_HEAT": (
        "Since 2015, games at or above 88°F at kickoff (197 games) have "
        "finished 0.6 points under the opening total on average -- a real but "
        "modest effect the market only partly prices in before kickoff."
    ),
    "EXTREME_COLD": (
        "Since 2015, games at or below 28°F at kickoff (37 games) have "
        "finished 1.1 points under the opening total on average, and the "
        "total line has moved down before kickoff 78% of the time."
    ),
}


def report(repository: CFBRepository, *, start_season: int = 2015, end_season: int = 2025) -> dict[str, Any]:
    rows = _game_rows(repository, start_season=start_season, end_season=end_season)
    return {
        "version": "cfb-weather-market-impact-v1",
        "start_season": start_season, "end_season": end_season,
        "games_with_weather_and_market_lines": len(rows),
        "baseline_all_games": _bucket_stats(rows),
        "by_wind": _dimension(rows, "sustained_wind", WIND_BUCKETS),
        "by_precipitation": _dimension(rows, "precipitation_amount", PRECIP_BUCKETS),
        "by_temperature": _dimension(rows, "temperature", TEMP_BUCKETS),
        "extremes": {
            "highest_wind": _extremes(rows, "sustained_wind", reverse=True),
            "heaviest_precipitation": _extremes(rows, "precipitation_amount", reverse=True),
            "coldest": _extremes(rows, "temperature", reverse=False),
            "hottest": _extremes(rows, "temperature", reverse=True),
        },
        "notes": [
            "Buckets are fixed thresholds chosen for football, not selected from outcomes.",
            "A negative line_movement means the closing total moved below the opener (toward an under).",
            "A negative actual_minus_open means the final score finished under the opening total.",
            "weather_source distinguishes a live pregame forecast from a retrospective open-meteo-archive reading.",
        ],
    }
