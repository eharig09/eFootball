"""Public track record for Football Lab's frozen NFL picks.

Everything here reads the immutable issuance ledger (forecast_ledger.py), so a
number on the record page is a pick that existed before kickoff -- never one
rebuilt after the result. The ledger keeps every re-issuance of a game; the
record counts each game once, at its *first* issuance that carried that kind
of pick, because that is the price a reader could actually have taken.

Rates, ROI and charts stay hidden until MIN_GRADED picks have settled, so a
three-game hot streak never reads as an edge (see the season-settling rule).
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.nfl.forecast_ledger import grading_report
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.tables import Column, Table

MIN_GRADED = 20
BREAKEVEN = 110.0 / 210.0  # flat -110
WIN_UNITS = 100.0 / 110.0

#: Calibration buckets by absolute model-minus-market gap, in points.
ATS_BUCKETS = ((0.0, 1.5), (1.5, 3.0), (3.0, 5.0), (5.0, math.inf))
TOTAL_BUCKETS = ((0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, math.inf))


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a win rate; honest at small n."""
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _first_per_game(issuances: list[dict[str, Any]], grade_key: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    picks = []
    for row in issuances:  # already ordered by generated_at, forecast_id
        if not row.get(grade_key) or row["game_id"] in seen:
            continue
        seen.add(row["game_id"])
        picks.append(row)
    return picks


def _tally(picks: list[dict[str, Any]], grade_key: str) -> dict[str, Any]:
    wins = sum(p[grade_key] == "win" for p in picks)
    losses = sum(p[grade_key] == "loss" for p in picks)
    pushes = sum(p[grade_key] == "push" for p in picks)
    decisions = wins + losses
    units = wins * WIN_UNITS - losses
    interval = wilson(wins, decisions)
    return {
        "wins": wins, "losses": losses, "pushes": pushes, "n": wins + losses + pushes,
        "record": f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
        "win_rate": wins / decisions if decisions else None,
        "interval": interval,
        "units": round(units, 2),
        "roi": units / (wins + losses + pushes) if (wins + losses + pushes) else None,
    }


def _clv(picks: list[dict[str, Any]], *, kind: str) -> dict[str, Any]:
    """Closing-line value: positive means the market later moved toward our side."""
    values = []
    for pick in picks:
        if kind == "ats":
            issue, close = pick.get("spread_at_issue"), pick.get("close_spread")
            direction = 1.0 if pick["ats_pick_team"] == pick["home_team"] else -1.0
        else:
            issue, close = pick.get("total_at_issue"), pick.get("close_total")
            direction = 1.0 if str(pick["total_pick"]).startswith("Over") else -1.0
        if issue is None or close is None:
            continue
        values.append(direction * (float(close) - float(issue)))
    n = len(values)
    return {
        "n": n,
        "mean": sum(values) / n if n else None,
        "beat_close": sum(v > 0 for v in values),
        "lost_to_close": sum(v < 0 for v in values),
        "unchanged": sum(v == 0 for v in values),
    }


def _calibration(picks: list[dict[str, Any]], grade_key: str, edge_of, buckets) -> list[dict[str, Any]]:
    rows = []
    for low, high in buckets:
        chosen = [p for p in picks if low <= abs(edge_of(p)) < high]
        tally = _tally(chosen, grade_key)
        decisions = tally["wins"] + tally["losses"]
        label = f"{low:g}+" if math.isinf(high) else f"{low:g}–{high:g}"
        interval = tally["interval"]
        rows.append({
            "bucket": label, **tally, "decisions": decisions,
            "mean_edge": (sum(abs(edge_of(p)) for p in chosen) / len(chosen)) if chosen else None,
            "low": interval[0] if interval else None,
            "high": interval[1] if interval else None,
        })
    return rows


def _weekly(ats: list[dict[str, Any]], totals: list[dict[str, Any]],
            su: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_week: dict[int, dict[str, list]] = defaultdict(lambda: {"ats": [], "total": [], "su": []})
    for key, group in (("ats", ats), ("total", totals), ("su", su)):
        for pick in group:
            by_week[int(pick["week"])][key].append(pick)
    rows, running = [], 0.0
    for week in sorted(by_week):
        group = by_week[week]
        a = _tally(group["ats"], "ats_grade")
        running += a["units"]
        t = _tally(group["total"], "total_grade")
        rows.append({
            "week": week, "ats": a["record"], "ats_units": a["units"],
            "ats_cumulative": round(running, 2),
            "totals": t["record"], "su": _tally(group["su"], "straight_up_grade")["record"],
            "games": len({p["game_id"] for key in group.values() for p in key}),
        })
    return rows


def _error_series(ats_picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Weekly MAE of Football Lab's margin vs. the market's, same games."""
    by_week: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for pick in ats_picks:
        if pick.get("spread_at_issue") is None:
            continue
        actual = float(pick["home_score"] - pick["away_score"])
        by_week[int(pick["week"])].append(
            (abs(actual - float(pick["model_margin"])), abs(actual - float(pick["spread_at_issue"]))))
    return [{"week": week, "games": len(values),
             "model_mae": sum(v[0] for v in values) / len(values),
             "market_mae": sum(v[1] for v in values) / len(values)}
            for week, values in sorted(by_week.items())]


# ---------------------------------------------------------------- geometry

WIDTH, HEIGHT = 760, 240
LEFT, RIGHT, TOP, BOTTOM = 46, 16, 14, 206


def _scale(low: float, high: float):
    span = (high - low) or 1.0
    return lambda value: BOTTOM - (value - low) / span * (BOTTOM - TOP)


def _nice_bounds(values: list[float], *, include: float | None = None) -> tuple[float, float]:
    pool = list(values) + ([include] if include is not None else [])
    low, high = min(pool), max(pool)
    pad = max((high - low) * 0.12, 0.5)
    return low - pad, high + pad


def _ticks(low: float, high: float, y, *, fmt: str, count: int = 4) -> list[dict[str, Any]]:
    step = (high - low) / count
    return [{"y": round(y(low + step * i), 1), "label": format(low + step * i, fmt)}
            for i in range(count + 1)]


def line_chart(xs: list, series: list[dict[str, Any]], *, zero: bool = False,
               fmt: str = ".1f", label_of=lambda x: f"W{x}",
               label_every: int = 1) -> dict[str, Any]:
    """series: [{key, label, values: {x: value}, dashed?, markers?}] -> SVG geometry.

    `xs` is the ordered list of x keys (weeks, snapshot indexes...); `label_of`
    turns one into its axis/tooltip text. A series with markers=False draws as
    a bare reference line.
    """
    values = [v for s in series for v in s["values"].values()]
    if not xs or not values:
        return {"has_data": False}
    low, high = _nice_bounds(values, include=0.0 if zero else None)
    y = _scale(low, high)
    right = WIDTH - RIGHT
    step = (right - LEFT) / max(len(xs) - 1, 1)
    x_of = {x: (LEFT + step * i if len(xs) > 1 else (LEFT + right) / 2)
            for i, x in enumerate(xs)}
    built = []
    for s in series:
        pts = [{"x": round(x_of[k], 1), "y": round(y(v), 1), "x_label": label_of(k), "value": v}
               for k, v in sorted(s["values"].items(), key=lambda item: xs.index(item[0]))]
        built.append({"key": s["key"], "label": s["label"], "points": pts,
                      "dashed": bool(s.get("dashed")), "markers": s.get("markers", True),
                      "path": " ".join(f"{p['x']},{p['y']}" for p in pts)})
    return {
        "has_data": True, "width": WIDTH, "height": HEIGHT, "left": LEFT, "right": right,
        "ticks": _ticks(low, high, y, fmt=fmt), "series": built,
        "zero_y": round(y(0.0), 1) if low < 0 < high else None,
        "week_labels": [{"x": round(x_of[x], 1), "label": label_of(x)}
                        for i, x in enumerate(xs)
                        if i % label_every == 0 or i == len(xs) - 1],
        "label_y": BOTTOM + 18,
    }


def interval_chart(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Cover rate with its 95% range per edge bucket, against 50% and breakeven."""
    usable = [r for r in rows if r["low"] is not None]
    if not usable:
        return {"has_data": False}
    y = _scale(0.0, 1.0)
    right = WIDTH - RIGHT
    step = (right - LEFT) / len(rows)
    points = []
    for i, row in enumerate(rows):
        if row["low"] is None:
            continue
        points.append({
            "x": round(LEFT + step * (i + 0.5), 1), "bucket": row["bucket"], "n": row["decisions"],
            "rate": row["win_rate"], "y": round(y(row["win_rate"]), 1),
            "y_low": round(y(row["low"]), 1), "y_high": round(y(row["high"]), 1),
        })
    return {
        "has_data": True, "width": WIDTH, "height": HEIGHT, "left": LEFT, "right": right,
        "ticks": [{"y": round(y(v), 1), "label": f"{v:.0%}"} for v in (0.0, 0.25, 0.5, 0.75, 1.0)],
        "half_y": round(y(0.5), 1), "breakeven_y": round(y(BREAKEVEN), 1),
        "points": points,
        "labels": [{"x": round(LEFT + step * (i + 0.5), 1), "label": row["bucket"]}
                   for i, row in enumerate(rows)],
        "label_y": BOTTOM + 18,
    }


# ------------------------------------------------------------------ report

def build(repository: NFLRepository, season: int) -> dict[str, Any]:
    report = grading_report(repository, season=int(season))
    issuances = report["issuances"]
    ats = _first_per_game(issuances, "ats_grade")
    totals = _first_per_game(issuances, "total_grade")
    su = _first_per_game(issuances, "straight_up_grade")

    ats_tally = _tally(ats, "ats_grade")
    total_tally = _tally(totals, "total_grade")
    su_tally = _tally(su, "straight_up_grade")
    settled = ats_tally["n"] >= MIN_GRADED or total_tally["n"] >= MIN_GRADED

    weekly = _weekly(ats, totals, su)
    weeks = [row["week"] for row in weekly]
    errors = _error_series(ats)
    ats_edge = lambda p: float(p["model_margin"]) - float(p["spread_at_issue"])  # noqa: E731
    total_edge = lambda p: float(p["model_total"]) - float(p["total_at_issue"])  # noqa: E731
    calibration_ats = _calibration(ats, "ats_grade", ats_edge, ATS_BUCKETS)
    calibration_total = _calibration(totals, "total_grade", total_edge, TOTAL_BUCKETS)

    return {
        "season": int(season),
        "frozen": report["frozen"],
        "graded_issuances": report["completed_issuances"],
        "min_graded": MIN_GRADED,
        "settled": settled,
        "straight_up": su_tally, "ats": ats_tally, "totals": total_tally,
        "clv": {"ats": _clv(ats, kind="ats"), "totals": _clv(totals, kind="totals")},
        "weekly": weekly,
        "units_chart": line_chart(weeks, [{
            "key": "off", "label": "Cumulative ATS units",
            "values": {r["week"]: r["ats_cumulative"] for r in weekly}}], zero=True, fmt="+.1f"),
        "error_chart": line_chart(
            [r["week"] for r in errors],
            [{"key": "off", "label": "Football Lab",
              "values": {r["week"]: r["model_mae"] for r in errors}},
             {"key": "def", "label": "Market",
              "values": {r["week"]: r["market_mae"] for r in errors}}]),
        "errors": errors,
        "calibration": {"ats": calibration_ats, "totals": calibration_total},
        "calibration_chart": {"ats": interval_chart(calibration_ats),
                              "totals": interval_chart(calibration_total)},
        "policy": {
            "pricing": "Flat -110.",
            "counting": "Each game is counted once, at its first stored pick, before kickoff.",
            "start": "Tracking begins with the first stored forecast; earlier seasons are not back-filled.",
        },
    }


# ------------------------------------------------------------------ tables

def _signed_class(value: float | None) -> str:
    return "" if value in (None, 0) else ("win" if value > 0 else "loss")


def tables(record: dict[str, Any]) -> dict[str, Table]:
    """Shared-kit tables for the record page; rates stay blank until settled."""
    settled = record["settled"]
    weekly = Table([
        Column("week", "Wk", "int", emphasis=True),
        Column("games", "Games", "int"),
        Column("ats", "ATS", "text", align="right"),
        Column("ats_units", "Units", "signed2", title="Units at flat -110 for the week"),
        Column("ats_cumulative", "Season", "signed2", title="Cumulative ATS units"),
        Column("totals", "O/U", "text", align="right"),
        Column("su", "SU", "text", align="right"),
    ], [{**row, "ats_units_class": _signed_class(row["ats_units"]),
         "ats_cumulative_class": _signed_class(row["ats_cumulative"])}
        for row in record["weekly"]], dense=True,
        empty="No graded picks yet. Weeks appear here once frozen picks have final scores.")

    def calibration(rows: list[dict[str, Any]], unit: str) -> Table:
        shown = [{**row,
                  "range": (f"{row['low']:.0%}–{row['high']:.0%}" if row["low"] is not None else None),
                  "win_rate": row["win_rate"] if settled else None}
                 for row in rows if row["n"]]
        return Table([
            Column("bucket", f"Gap ({unit})", "text", emphasis=True,
                   title="Absolute distance between Football Lab and the market line when the pick was stored"),
            Column("record", "Record", "text", align="right"),
            Column("n", "N", "int"),
            Column("win_rate", "Win %", "rate"),
            Column("range", "95% range", "text", align="right",
                   title="Wilson interval -- how much a rate this size could move by chance"),
            Column("mean_edge", "Avg gap", "f1"),
        ], shown, dense=True, sortable=False,
            empty="Calibration buckets fill in as picks settle.")

    errors = Table([
        Column("week", "Wk", "int", emphasis=True),
        Column("games", "Games", "int"),
        Column("model_mae", "Lab MAE", "f1", title="Mean absolute error of Football Lab's margin"),
        Column("market_mae", "Market MAE", "f1", title="Mean absolute error of the stored spread"),
    ], [{**row, "gap": row["model_mae"] - row["market_mae"]} for row in record["errors"]],
        dense=True, empty="Margin errors appear once frozen picks have final scores.")
    return {"weekly": weekly, "errors": errors,
            "calibration_ats": calibration(record["calibration"]["ats"], "pts"),
            "calibration_totals": calibration(record["calibration"]["totals"], "pts")}
