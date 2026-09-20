"""Market-divergence matrix for Football Lab totals.

This study treats the Football Lab opening-total edge as the initial signal and
classifies the subsequent closing move relative to that signal:

- toward_1_plus: market moves >= 1 point toward Football Lab
- toward_lt1: market moves > 0.25 and < 1 point toward Football Lab
- unchanged: absolute aligned move <= 0.25 points
- away_lt1: market moves > 0.25 and < 1 point away from Football Lab
- away_1_plus: market moves >= 1 point away from Football Lab

The matrix evaluates closing-line outcomes, residuals, and how much of the
original model edge remains after the market move. No state is promoted or
filtered based on results.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import totals_market_movement as tmm
from sports_aggregator.cfb.repository import CFBRepository


def _movement_state(aligned_move: float) -> str:
    value = float(aligned_move)
    if value >= 1.0:
        return "toward_1_plus"
    if value > 0.25:
        return "toward_lt1"
    if value <= -1.0:
        return "away_1_plus"
    if value < -0.25:
        return "away_lt1"
    return "unchanged"


def _result(aligned_residual: float) -> str:
    if aligned_residual > 0:
        return "win"
    if aligned_residual < 0:
        return "loss"
    return "push"


def build_rows(repository: CFBRepository, *, test_season: int = 2025) -> list[dict[str, Any]]:
    rows = tmm.build_rows(repository, test_season=int(test_season))
    out = []
    for row in rows:
        direction = 1 if row["model_direction"] == "over" else -1
        model_vs_close = float(row["projected_total"]) - float(row["closing_total"])
        aligned_close_edge = model_vs_close * direction
        original_edge = abs(float(row["model_vs_open"]))
        retained_ratio = (
            aligned_close_edge / original_edge if original_edge > 0 else None
        )
        cooked = dict(row)
        cooked.update({
            "movement_state": _movement_state(float(row["aligned_market_move"])),
            "model_vs_close": model_vs_close,
            "aligned_model_edge_at_close": aligned_close_edge,
            "edge_retained_ratio": retained_ratio,
            "closing_result": _result(float(row["close_result_aligned"])),
        })
        out.append(cooked)
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for r in rows if r["closing_result"] == "win")
    losses = sum(1 for r in rows if r["closing_result"] == "loss")
    pushes = sum(1 for r in rows if r["closing_result"] == "push")
    decisions = wins + losses
    retained = [
        float(r["edge_retained_ratio"])
        for r in rows if r.get("edge_retained_ratio") is not None
    ]
    close_edges = [float(r["aligned_model_edge_at_close"]) for r in rows]
    residuals = [float(r["close_result_aligned"]) for r in rows]
    moves = [float(r["aligned_market_move"]) for r in rows]
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "decisions": decisions,
        "win_rate_ex_pushes": round(wins / decisions, 4) if decisions else None,
        "mean_closing_aligned_residual": (
            round(sum(residuals) / n, 3) if n else None
        ),
        "median_closing_aligned_residual": (
            round(_median(residuals), 3) if residuals else None
        ),
        "mean_aligned_market_move": (
            round(sum(moves) / n, 3) if n else None
        ),
        "mean_aligned_model_edge_at_close": (
            round(sum(close_edges) / n, 3) if n else None
        ),
        "mean_edge_retained_ratio": (
            round(sum(retained) / len(retained), 3) if retained else None
        ),
        "mean_close_provider_range": (
            round(
                sum(float(r["close_provider_range"]) for r in rows
                    if r.get("close_provider_range") is not None)
                / sum(1 for r in rows if r.get("close_provider_range") is not None),
                3,
            )
            if any(r.get("close_provider_range") is not None for r in rows)
            else None
        ),
    }


def _group(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[tuple(str(row.get(key, "unknown")) for key in keys)].append(row)
    out = []
    for values in sorted(grouped):
        item = {key: value for key, value in zip(keys, values)}
        item.update(_summary(grouped[values]))
        out.append(item)
    return out


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    rows = build_rows(repository, test_season=int(test_season))
    return {
        "version": "totals-divergence-matrix-v1",
        "test_through_season": int(test_season),
        "movement_state_definitions": {
            "toward_1_plus": "aligned market move >= +1.0",
            "toward_lt1": "+0.25 < aligned market move < +1.0",
            "unchanged": "absolute aligned market move <= 0.25",
            "away_lt1": "-1.0 < aligned market move < -0.25",
            "away_1_plus": "aligned market move <= -1.0",
        },
        "overall_by_movement_state": _group(rows, ("movement_state",)),
        "edge_x_movement": _group(
            rows, ("opening_edge_bucket", "movement_state")),
        "season_x_movement": _group(
            rows, ("season", "movement_state")),
        "direction_x_movement": _group(
            rows, ("model_direction", "movement_state")),
        "season_x_direction_x_movement": _group(
            rows, ("season", "model_direction", "movement_state")),
        "season_x_edge_x_movement": _group(
            rows, ("season", "opening_edge_bucket", "movement_state")),
        "game_rows": rows,
        "notes": [
            "Closing-line W/L/P is always graded in Football Lab's original opening direction.",
            "Aligned model edge at close measures how much of the original Football Lab disagreement remains after market movement.",
            "Edge retained ratio >1 means the market widened the disagreement; 0 to 1 means it narrowed but did not cross the model; negative means the close crossed beyond the model projection.",
            "Movement states are fixed before reviewing matrix outcomes.",
            "No divergence state is promoted to a production rule in this report.",
        ],
    }


def export_report(payload: dict[str, Any], output_dir: str) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "totals_divergence_matrix.json")
    summary_path = os.path.join(output_dir, "totals_divergence_matrix_summary.csv")
    games_path = os.path.join(output_dir, "totals_divergence_matrix_games.csv")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    summary_rows = []
    scopes = (
        "overall_by_movement_state",
        "edge_x_movement",
        "season_x_movement",
        "direction_x_movement",
        "season_x_direction_x_movement",
        "season_x_edge_x_movement",
    )
    for scope in scopes:
        for row in payload[scope]:
            summary_rows.append({"scope": scope, **row})
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


def compact_console_summary(payload: dict[str, Any],
                            paths: dict[str, str]) -> dict[str, Any]:
    return {
        "version": payload["version"],
        "overall_by_movement_state": payload["overall_by_movement_state"],
        "edge_x_movement": payload["edge_x_movement"],
        "files": paths,
    }
