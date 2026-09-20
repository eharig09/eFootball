"""Market diagnostics, ATS economics, and walk-forward totals analysis.

Spread terminology:
- game_lines.spread is treated as the closing spread currently stored
  by the project.
- It is NOT called a true closing line unless the underlying table exposes an
  explicit close/closing field. The report audits the schema and says which
  semantics are available.

Totals:
- Football Lab projected offensive points are summed by game.
- A prior-season-only linear calibration maps projected offensive total to final
  scoreboard total.
- The calibrated total is compared with the closing market total.
- Evaluation reports W/L/P, win rate excluding pushes, -110/-105/even-money ROI,
  residual error, year splits, market-total buckets, and fixed edge buckets.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import convergence_action_policy as cap
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.repository import CFBRepository


EDGE_BUCKETS = (
    (0.0, 1.0, "<1"),
    (1.0, 2.0, "1-1.99"),
    (2.0, 3.0, "2-2.99"),
    (3.0, 5.0, "3-4.99"),
    (5.0, 8.0, "5-7.99"),
    (8.0, float("inf"), "8+"),
)

TOTAL_BUCKETS = (
    (0.0, 45.0, "<45"),
    (45.0, 55.0, "45-54.5"),
    (55.0, 65.0, "55-64.5"),
    (65.0, float("inf"), "65+"),
)


def _linear_fit(pairs: list[tuple[float, float]]) -> dict[str, float] | None:
    if len(pairs) < 50:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    denom = sum((x - mx) ** 2 for x, _ in pairs)
    slope = (
        sum((x - mx) * (y - my) for x, y in pairs) / denom
        if denom else 0.0
    )
    return {"intercept": my - slope * mx, "slope": slope, "n": len(pairs)}


def _bucket(value: float, definitions) -> str:
    for low, high, label in definitions:
        if low <= value < high:
            return label
    return "unknown"


def _market_schema_audit(repository: CFBRepository) -> dict[str, Any]:
    with repository._reader() as connection:
        columns = [
            str(r["name"])
            for r in connection.execute("PRAGMA table_info(game_lines)")
        ]
    normalized = {c.casefold(): c for c in columns}
    close_candidates = [
        c for c in columns if "close" in c.casefold() or "closing" in c.casefold()
    ]
    open_candidates = [
        c for c in columns if "open" in c.casefold() or "opening" in c.casefold()
    ]
    return {
        "columns": columns,
        "spread_column": normalized.get("spread"),
        "total_column": normalized.get("over_under"),
        "explicit_close_columns": close_candidates,
        "explicit_open_columns": open_candidates,
        "line_semantics": (
            "explicit_close_available"
            if close_candidates
            else "assumed_closing_line_from_consensus_snapshot"
        ),
    }


def _market_by_game(repository: CFBRepository) -> dict[int, dict[str, Any]]:
    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                """SELECT game_id,
                          AVG(spread) AS spread,
                          AVG(over_under) AS total,
                          COUNT(*) AS books,
                          MIN(spread) AS min_spread,
                          MAX(spread) AS max_spread,
                          MIN(over_under) AS min_total,
                          MAX(over_under) AS max_total
                   FROM game_lines
                   GROUP BY game_id"""
            )
        ]
    return {int(r["game_id"]): r for r in rows}


def _games(repository: CFBRepository) -> dict[int, dict[str, Any]]:
    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                """SELECT game_id,season,week,start_date,
                          home_team,away_team,home_points,away_points
                   FROM games
                   WHERE home_points IS NOT NULL AND away_points IS NOT NULL"""
            )
        ]
    return {int(r["game_id"]): r for r in rows}


def _projection_game_rows(repository: CFBRepository) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                """SELECT game_id,side,season,projected_offensive_points,
                          actual_score_points
                   FROM cfb_projection_backtest
                   WHERE backtest_version=?""",
                (BACKTEST_VERSION,),
            )
        ]
    grouped: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        grouped[int(row["game_id"])][str(row["side"])] = row

    out = []
    for gid, sides in grouped.items():
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        hp = home.get("projected_offensive_points")
        ap = away.get("projected_offensive_points")
        ha = home.get("actual_score_points")
        aa = away.get("actual_score_points")
        if hp is None or ap is None or ha is None or aa is None:
            continue
        out.append({
            "game_id": gid,
            "season": int(home["season"]),
            "projected_offensive_total": float(hp) + float(ap),
            "actual_score_total": float(ha) + float(aa),
        })
    return out


def _totals_rows(repository: CFBRepository, *, test_season: int) -> list[dict[str, Any]]:
    projections = _projection_game_rows(repository)
    market = _market_by_game(repository)
    games = _games(repository)
    seasons = sorted({
        int(r["season"]) for r in projections if int(r["season"]) <= int(test_season)
    })
    out = []
    if not seasons:
        return out

    for season in seasons:
        if season == min(seasons):
            continue
        train = [
            (float(r["projected_offensive_total"]), float(r["actual_score_total"]))
            for r in projections if int(r["season"]) < season
        ]
        calibration = _linear_fit(train)
        if not calibration:
            continue

        for row in projections:
            if int(row["season"]) != season:
                continue
            gid = int(row["game_id"])
            m = market.get(gid)
            g = games.get(gid)
            if not m or not g or m.get("total") is None:
                continue
            market_total = float(m["total"])
            projected_total = (
                float(calibration["intercept"])
                + float(calibration["slope"]) * float(row["projected_offensive_total"])
            )
            actual_total = float(row["actual_score_total"])
            edge = projected_total - market_total
            actual_residual = actual_total - market_total
            direction = 1 if edge > 0 else -1 if edge < 0 else 0
            if not direction:
                continue
            aligned = actual_residual * direction
            result = "win" if aligned > 0 else "loss" if aligned < 0 else "push"
            out.append({
                "game_id": gid,
                "season": season,
                "week": int(g["week"]) if g.get("week") is not None else None,
                "start_date": g.get("start_date"),
                "home_team": g.get("home_team"),
                "away_team": g.get("away_team"),
                "books": int(m["books"]) if m.get("books") is not None else None,
                "market_total": market_total,
                "min_market_total": float(m["min_total"]) if m.get("min_total") is not None else None,
                "max_market_total": float(m["max_total"]) if m.get("max_total") is not None else None,
                "market_total_range": (
                    float(m["max_total"]) - float(m["min_total"])
                    if m.get("max_total") is not None and m.get("min_total") is not None
                    else None
                ),
                "projected_offensive_total_raw": float(row["projected_offensive_total"]),
                "projected_scoreboard_total": projected_total,
                "actual_score_total": actual_total,
                "total_edge": edge,
                "abs_total_edge": abs(edge),
                "direction": "over" if direction > 0 else "under",
                "actual_total_residual": actual_residual,
                "aligned_total_residual": aligned,
                "result": result,
                "edge_bucket": _bucket(abs(edge), EDGE_BUCKETS),
                "market_total_bucket": _bucket(market_total, TOTAL_BUCKETS),
                "calibration_intercept": float(calibration["intercept"]),
                "calibration_slope": float(calibration["slope"]),
                "calibration_n": int(calibration["n"]),
            })
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _bet_summary(rows: list[dict[str, Any]], *, residual_key: str) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    pushes = sum(1 for r in rows if r["result"] == "push")
    decisions = wins + losses
    win_rate = wins / decisions if decisions else None

    def roi(price: int) -> float | None:
        if not decisions:
            return None
        if price == -110:
            profit = wins * (100 / 110) - losses
        elif price == -105:
            profit = wins * (100 / 105) - losses
        elif price == 100:
            profit = wins - losses
        else:
            return None
        return profit / decisions

    residuals = [float(r[residual_key]) for r in rows]
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "decisions": decisions,
        "win_rate_ex_pushes": round(win_rate, 4) if win_rate is not None else None,
        "roi_minus_110": round(roi(-110), 4) if decisions else None,
        "roi_minus_105": round(roi(-105), 4) if decisions else None,
        "roi_even_money": round(roi(100), 4) if decisions else None,
        "mean_aligned_residual": round(sum(residuals) / len(residuals), 3) if residuals else None,
        "median_aligned_residual": round(_median(residuals), 3) if residuals else None,
    }


def _spread_action_rows(repository: CFBRepository, *, test_season: int) -> list[dict[str, Any]]:
    rows = cap._full_rows(repository, test_season=int(test_season))
    out = []
    for row in rows:
        residual = float(row["aligned_residual"])
        result = "win" if residual > 0 else "loss" if residual < 0 else "push"
        spread = row.get("market_spread_home")
        selected_side = row.get("selected_side")
        selected_spread = None
        if spread is not None:
            selected_spread = float(spread) if selected_side == "home" else -float(spread)
        cooked = dict(row)
        cooked.update({
            "result": result,
            "selected_market_spread": selected_spread,
            "abs_market_spread": abs(float(spread)) if spread is not None else None,
        })
        out.append(cooked)
    return out


def _spread_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _bet_summary(rows, residual_key="aligned_residual")
    spreads = [float(r["selected_market_spread"]) for r in rows if r.get("selected_market_spread") is not None]
    abs_spreads = [float(r["abs_market_spread"]) for r in rows if r.get("abs_market_spread") is not None]
    summary.update({
        "average_selected_market_spread": round(sum(spreads) / len(spreads), 3) if spreads else None,
        "average_absolute_market_spread": round(sum(abs_spreads) / len(abs_spreads), 3) if abs_spreads else None,
    })
    return summary


def _group_summary(rows: list[dict[str, Any]], key: str, summary_fn) -> list[dict[str, Any]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key, "unknown"))].append(row)
    return [{"value": value, **summary_fn(grouped[value])} for value in sorted(grouped)]


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    schema_audit = _market_schema_audit(repository)

    spread_rows = _spread_action_rows(repository, test_season=int(test_season))
    spread_lt14 = [r for r in spread_rows if r.get("spread_bucket") != "14+"]
    spread_14 = [r for r in spread_rows if r.get("spread_bucket") == "14+"]

    totals = _totals_rows(repository, test_season=int(test_season))

    return {
        "version": "market-ats-totals-v1",
        "test_through_season": int(test_season),
        "market_line_schema_audit": schema_audit,
        "spread": {
            "line_label": "closing_line",
            "full_convergence": _spread_summary(spread_rows),
            "full_convergence_spread_lt14": _spread_summary(spread_lt14),
            "full_convergence_spread_14_plus": _spread_summary(spread_14),
            "by_season": _group_summary(spread_rows, "season", _spread_summary),
            "by_spread_bucket": _group_summary(spread_rows, "spread_bucket", _spread_summary),
            "notes": [
                "Average selected market spread is signed from the selected team's perspective; negative means that side was favored on average.",
                "Average absolute market spread measures matchup line magnitude regardless of selected side.",
                "Pushes are excluded from win-rate and ROI denominators.",
            ],
        },
        "totals": {
            "method": "prior-season-only calibration of Football Lab projected offensive total to final scoreboard total",
            "overall": _bet_summary(totals, residual_key="aligned_total_residual"),
            "by_season": _group_summary(
                totals, "season",
                lambda rows: _bet_summary(rows, residual_key="aligned_total_residual")),
            "by_direction": _group_summary(
                totals, "direction",
                lambda rows: _bet_summary(rows, residual_key="aligned_total_residual")),
            "by_edge_bucket": _group_summary(
                totals, "edge_bucket",
                lambda rows: _bet_summary(rows, residual_key="aligned_total_residual")),
            "by_market_total_bucket": _group_summary(
                totals, "market_total_bucket",
                lambda rows: _bet_summary(rows, residual_key="aligned_total_residual")),
            "game_rows": totals,
            "notes": [
                "Over/under direction is chosen solely from calibrated Football Lab total minus the closing market total.",
                "No total-edge cutoff is selected in this report; fixed buckets are descriptive.",
                "Market-total provider range is exported to expose disagreement among books.",
            ],
        },
    }


def export_report(payload: dict[str, Any], output_dir: str) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "market_ats_totals.json")
    totals_path = os.path.join(output_dir, "market_totals_games.csv")
    summary_path = os.path.join(output_dir, "market_totals_summary.csv")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    games = payload["totals"]["game_rows"]
    if games:
        with open(totals_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(games[0].keys()))
            writer.writeheader()
            writer.writerows(games)
    else:
        with open(totals_path, "w", encoding="utf-8") as handle:
            handle.write("")

    summary_rows = []
    for scope in ("by_season", "by_direction", "by_edge_bucket", "by_market_total_bucket"):
        for row in payload["totals"][scope]:
            summary_rows.append({"slice": scope, **row})
    fields = [
        "slice", "value", "n", "wins", "losses", "pushes", "decisions",
        "win_rate_ex_pushes", "roi_minus_110", "roi_minus_105",
        "roi_even_money", "mean_aligned_residual", "median_aligned_residual",
    ]
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary_rows)

    return {"json": json_path, "totals_games_csv": totals_path, "totals_summary_csv": summary_path}


def compact_console_summary(payload: dict[str, Any], paths: dict[str, str]) -> dict[str, Any]:
    spread = payload["spread"]
    return {
        "version": payload["version"],
        "line_semantics": payload["market_line_schema_audit"]["line_semantics"],
        "spread_full_convergence": spread["full_convergence"],
        "spread_lt14": spread["full_convergence_spread_lt14"],
        "totals_overall": payload["totals"]["overall"],
        "files": paths,
    }
