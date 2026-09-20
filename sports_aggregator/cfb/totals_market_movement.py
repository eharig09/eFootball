"""Football Lab totals vs opening-to-closing market movement.

Purpose:
- Test whether Football Lab disagreement with the opening total predicts the
  direction and magnitude of subsequent closing-line movement.
- Separate model-vs-market information quality from final-game outcome.
- Use the stored provider consensus snapshot as the operative close.

No outcome-derived thresholds are selected. Fixed opening-edge buckets:
<1, 1-1.99, 2-2.99, 3-4.99, 5-7.99, 8+.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import market_ats_totals as mat
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.repository import CFBRepository

EDGE_BUCKETS = mat.EDGE_BUCKETS


def _market_open_close(repository: CFBRepository) -> dict[int, dict[str, Any]]:
    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                """SELECT game_id,
                          AVG(over_under_open) AS open_total,
                          AVG(over_under) AS close_total,
                          COUNT(CASE WHEN over_under_open IS NOT NULL THEN 1 END) AS open_books,
                          COUNT(CASE WHEN over_under IS NOT NULL THEN 1 END) AS close_books,
                          MIN(over_under) AS min_close_total,
                          MAX(over_under) AS max_close_total
                   FROM game_lines
                   GROUP BY game_id"""
            )
        ]
    return {int(r["game_id"]): r for r in rows}


def _projection_game_rows(repository: CFBRepository) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                """SELECT game_id,side,season,week,kickoff,
                          projected_offensive_points,actual_score_points
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
        vals = (
            home.get("projected_offensive_points"),
            away.get("projected_offensive_points"),
            home.get("actual_score_points"),
            away.get("actual_score_points"),
        )
        if any(v is None for v in vals):
            continue
        out.append({
            "game_id": gid,
            "season": int(home["season"]),
            "week": int(home["week"]) if home.get("week") is not None else None,
            "kickoff": home["kickoff"],
            "projected_offensive_total": (
                float(home["projected_offensive_points"])
                + float(away["projected_offensive_points"])
            ),
            "actual_score_total": (
                float(home["actual_score_points"])
                + float(away["actual_score_points"])
            ),
        })
    return out


def _calibrations(rows: list[dict[str, Any]], target_season: int) -> dict[str, float] | None:
    train = [
        (float(r["projected_offensive_total"]), float(r["actual_score_total"]))
        for r in rows if int(r["season"]) < int(target_season)
    ]
    return mat._linear_fit(train)


def build_rows(repository: CFBRepository, *, test_season: int = 2025) -> list[dict[str, Any]]:
    projections = _projection_game_rows(repository)
    market = _market_open_close(repository)
    seasons = sorted({
        int(r["season"]) for r in projections if int(r["season"]) <= int(test_season)
    })
    out = []
    if not seasons:
        return out

    fits = {season: _calibrations(projections, season) for season in seasons}

    for row in projections:
        season = int(row["season"])
        if season > int(test_season):
            continue
        fit = fits.get(season)
        m = market.get(int(row["game_id"]))
        if not fit or not m:
            continue
        if m.get("open_total") is None or m.get("close_total") is None:
            continue

        projected_total = (
            float(fit["intercept"])
            + float(fit["slope"]) * float(row["projected_offensive_total"])
        )
        open_total = float(m["open_total"])
        close_total = float(m["close_total"])
        actual_total = float(row["actual_score_total"])

        model_vs_open = projected_total - open_total
        model_direction = 1 if model_vs_open > 0 else -1 if model_vs_open < 0 else 0
        if model_direction == 0:
            continue

        market_move = close_total - open_total
        aligned_market_move = market_move * model_direction
        close_moved_toward_model = aligned_market_move > 0
        close_moved_away = aligned_market_move < 0
        unchanged = aligned_market_move == 0

        open_distance = abs(projected_total - open_total)
        close_distance = abs(projected_total - close_total)
        distance_improvement = open_distance - close_distance

        actual_vs_open = actual_total - open_total
        actual_vs_close = actual_total - close_total
        open_result_aligned = actual_vs_open * model_direction
        close_result_aligned = actual_vs_close * model_direction

        out.append({
            "game_id": int(row["game_id"]),
            "season": season,
            "week": row["week"],
            "kickoff": row["kickoff"],
            "projected_total": projected_total,
            "opening_total": open_total,
            "closing_total": close_total,
            "actual_total": actual_total,
            "model_vs_open": model_vs_open,
            "abs_model_vs_open": abs(model_vs_open),
            "model_direction": "over" if model_direction > 0 else "under",
            "market_move": market_move,
            "aligned_market_move": aligned_market_move,
            "close_moved_toward_model": close_moved_toward_model,
            "close_moved_away_from_model": close_moved_away,
            "close_unchanged": unchanged,
            "open_distance_to_model": open_distance,
            "close_distance_to_model": close_distance,
            "distance_improvement_toward_model": distance_improvement,
            "open_result_aligned": open_result_aligned,
            "close_result_aligned": close_result_aligned,
            "opening_edge_bucket": mat._bucket(abs(model_vs_open), EDGE_BUCKETS),
            "close_provider_range": (
                float(m["max_close_total"]) - float(m["min_close_total"])
                if m.get("max_close_total") is not None
                and m.get("min_close_total") is not None else None
            ),
            "open_books": int(m["open_books"] or 0),
            "close_books": int(m["close_books"] or 0),
            "calibration_n": int(fit["n"]),
        })
    return out


def _movement_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    toward = sum(1 for r in rows if r["close_moved_toward_model"])
    away = sum(1 for r in rows if r["close_moved_away_from_model"])
    unchanged = sum(1 for r in rows if r["close_unchanged"])
    moved = toward + away
    return {
        "n": n,
        "toward_model": toward,
        "away_from_model": away,
        "unchanged": unchanged,
        "directional_move_rate_ex_unchanged": (
            round(toward / moved, 4) if moved else None
        ),
        "mean_aligned_market_move": round(
            sum(float(r["aligned_market_move"]) for r in rows) / n, 3),
        "median_aligned_market_move": round(
            mat._median([float(r["aligned_market_move"]) for r in rows]), 3),
        "mean_distance_improvement_toward_model": round(
            sum(float(r["distance_improvement_toward_model"]) for r in rows) / n, 3),
        "mean_open_edge": round(
            sum(abs(float(r["model_vs_open"])) for r in rows) / n, 3),
        "mean_close_provider_range": round(
            sum(float(r["close_provider_range"]) for r in rows
                if r.get("close_provider_range") is not None)
            / max(1, sum(1 for r in rows if r.get("close_provider_range") is not None)),
            3,
        ),
    }


def _outcome_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    wins = sum(1 for r in rows if float(r[key]) > 0)
    losses = sum(1 for r in rows if float(r[key]) < 0)
    pushes = sum(1 for r in rows if float(r[key]) == 0)
    decisions = wins + losses
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "decisions": decisions,
        "win_rate_ex_pushes": round(wins / decisions, 4) if decisions else None,
        "mean_aligned_residual": round(
            sum(float(r[key]) for r in rows) / len(rows), 3) if rows else None,
    }


def _group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key, "unknown"))].append(row)
    return [
        {
            "value": value,
            "movement": _movement_summary(grouped[value]),
            "opening_outcome": _outcome_summary(grouped[value], "open_result_aligned"),
            "closing_outcome": _outcome_summary(grouped[value], "close_result_aligned"),
        }
        for value in sorted(grouped)
    ]


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    rows = build_rows(repository, test_season=int(test_season))
    return {
        "version": "totals-market-movement-v1",
        "test_through_season": int(test_season),
        "overall": {
            "movement": _movement_summary(rows),
            "opening_outcome": _outcome_summary(rows, "open_result_aligned"),
            "closing_outcome": _outcome_summary(rows, "close_result_aligned"),
        },
        "by_season": _group(rows, "season"),
        "by_opening_edge_bucket": _group(rows, "opening_edge_bucket"),
        "by_direction": _group(rows, "model_direction"),
        "game_rows": rows,
        "notes": [
            "The stored provider consensus over_under_open is treated as opening total.",
            "The stored provider consensus over_under is treated as closing total.",
            "Market movement is evaluated before final-game outcome, so it is a separate model-information test.",
            "Directional move rate excludes unchanged totals from its denominator.",
            "No opening-edge threshold is selected from outcomes in this report.",
            "Closing provider range is retained as a market-disagreement diagnostic.",
        ],
    }


def export_report(payload: dict[str, Any], output_dir: str) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "totals_market_movement.json")
    summary_path = os.path.join(output_dir, "totals_market_movement_summary.csv")
    games_path = os.path.join(output_dir, "totals_market_movement_games.csv")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    summary_rows = []
    for scope, blocks in (
        ("by_season", payload["by_season"]),
        ("by_opening_edge_bucket", payload["by_opening_edge_bucket"]),
        ("by_direction", payload["by_direction"]),
    ):
        for block in blocks:
            summary_rows.append({
                "scope": scope,
                "value": block["value"],
                **{f"movement_{k}": v for k, v in block["movement"].items()},
                **{f"open_{k}": v for k, v in block["opening_outcome"].items()},
                **{f"close_{k}": v for k, v in block["closing_outcome"].items()},
            })

    fields = sorted({key for row in summary_rows for key in row})
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary_rows)

    games = payload["game_rows"]
    if games:
        with open(games_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(games[0].keys()))
            writer.writeheader()
            writer.writerows(games)
    else:
        with open(games_path, "w", encoding="utf-8") as handle:
            handle.write("")

    return {
        "json": json_path,
        "summary_csv": summary_path,
        "games_csv": games_path,
    }


def compact_console_summary(payload: dict[str, Any], paths: dict[str, str]) -> dict[str, Any]:
    return {
        "version": payload["version"],
        "overall": payload["overall"],
        "opening_edge_buckets": [
            {
                "bucket": row["value"],
                "movement": row["movement"],
                "closing_outcome": row["closing_outcome"],
            }
            for row in payload["by_opening_edge_bucket"]
        ],
        "files": paths,
    }
