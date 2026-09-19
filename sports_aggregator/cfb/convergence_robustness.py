"""Robustness and export layer for the frozen convergence hypothesis.

No slice is used to select a new production rule. These are descriptive
stress tests of the already-frozen >=1.0 sigma Full Convergence state.

Exports:
- full JSON report
- compact summary CSV
- game-level Full Convergence CSV

Console output is intentionally short to avoid Render log truncation.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import conditional_convergence as cc
from sports_aggregator.cfb import convergence_validation as cv
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.repository import CFBRepository

FROZEN_THRESHOLD = 1.0
FROZEN_CONFIRMATIONS = 2


def _metadata(repository: CFBRepository) -> dict[int, dict[str, Any]]:
    with repository._reader() as connection:
        games = {
            int(r["game_id"]): dict(r)
            for r in connection.execute(
                """SELECT game_id,season,week,start_date,home_team,away_team,
                          home_points,away_points
                   FROM games"""
            )
        }
        lines = {
            int(r["game_id"]): dict(r)
            for r in connection.execute(
                """SELECT game_id,AVG(spread) AS spread,AVG(over_under) AS total,
                          COUNT(*) AS books
                   FROM game_lines
                   GROUP BY game_id"""
            )
        }
    for gid, game in games.items():
        game["line"] = lines.get(gid, {})
    return games


def _classified_with_context(repository: CFBRepository, *,
                             test_season: int) -> list[dict[str, Any]]:
    raw = ipl.build_lens_rows(repository, test_season=int(test_season))
    seasons = sorted({int(r["season"]) for r in raw})
    meta = _metadata(repository)
    output = []
    for season in [s for s in seasons if s > min(seasons) and s <= int(test_season)]:
        train = [r for r in raw if int(r["season"]) < season]
        test = [r for r in raw if int(r["season"]) == season]
        scales = cc._lens_scales(train)
        for row in test:
            state = cc._state(row, scales)
            if state is None or state["available_confirmations"] < 2:
                continue
            gid = int(row["game_id"])
            game = meta.get(gid, {})
            line = game.get("line", {})
            spread = line.get("spread")
            total = line.get("total")
            primary_direction = int(state["primary_direction"])
            aligned = float(row["market_margin_residual"]) * primary_direction
            home_points = game.get("home_points")
            away_points = game.get("away_points")
            actual_home_margin = (
                float(home_points) - float(away_points)
                if home_points is not None and away_points is not None else None
            )
            home_market_margin = -float(spread) if spread is not None else None
            selected_side = "home" if primary_direction > 0 else "away"
            selected_market_margin = (
                home_market_margin * primary_direction
                if home_market_margin is not None else None
            )
            if selected_market_margin is None:
                market_role = "unknown"
            elif selected_market_margin > 0:
                market_role = "favorite"
            elif selected_market_margin < 0:
                market_role = "underdog"
            else:
                market_role = "pickem"

            abs_spread = abs(float(home_market_margin)) if home_market_margin is not None else None
            if abs_spread is None:
                spread_bucket = "unknown"
            elif abs_spread < 3:
                spread_bucket = "<3"
            elif abs_spread < 7:
                spread_bucket = "3-6.5"
            elif abs_spread < 14:
                spread_bucket = "7-13.5"
            else:
                spread_bucket = "14+"

            week = game.get("week")
            if week is None:
                season_phase = "unknown"
            elif int(week) <= 4:
                season_phase = "early_1_4"
            elif int(week) <= 9:
                season_phase = "mid_5_9"
            else:
                season_phase = "late_10_plus"

            if total is None:
                total_bucket = "unknown"
            elif float(total) < 45:
                total_bucket = "<45"
            elif float(total) < 55:
                total_bucket = "45-54.5"
            elif float(total) < 65:
                total_bucket = "55-64.5"
            else:
                total_bucket = "65+"

            output.append({
                "game_id": gid,
                "season": season,
                "week": int(week) if week is not None else None,
                "start_date": game.get("start_date"),
                "home_team": game.get("home_team"),
                "away_team": game.get("away_team"),
                "selected_side": selected_side,
                "selected_team": (
                    game.get("home_team") if primary_direction > 0
                    else game.get("away_team")
                ),
                "market_role": market_role,
                "spread_bucket": spread_bucket,
                "total_bucket": total_bucket,
                "season_phase": season_phase,
                "market_spread_home": float(spread) if spread is not None else None,
                "market_home_margin": home_market_margin,
                "market_total": float(total) if total is not None else None,
                "books": int(line["books"]) if line.get("books") is not None else None,
                "actual_home_margin": actual_home_margin,
                "market_margin_residual_home": float(row["market_margin_residual"]),
                "aligned_residual": aligned,
                "hit": aligned > 0,
                "margin_power_edge": row.get("margin_power_edge"),
                "margin_power_z": float(state["margin_z"]),
                "abs_margin_power_z": abs(float(state["margin_z"])),
                "structural_z": state.get("structural_z"),
                "structural_z_aligned": (
                    float(state["structural_z"]) * primary_direction
                    if state.get("structural_z") is not None else None
                ),
                "structural_members": "|".join(state.get("structural_members") or []),
                "line_elo_z": state.get("market_z"),
                "line_elo_z_aligned": (
                    float(state["market_z"]) * primary_direction
                    if state.get("market_z") is not None else None
                ),
                "confirmation_count": int(state["confirmation_count"]),
                "confirmation_combination": state["confirmation_combination"],
                "narrative_state": state["narrative_state"],
                "narrative_z": state.get("narrative_z"),
                "football_lab_edge": row.get("football_lab_edge"),
                "elo_edge": row.get("elo_edge"),
                "efficiency_power_edge": row.get("efficiency_power_edge"),
                "line_elo_edge": row.get("line_elo_edge"),
                "narrative_interaction_edge": row.get("narrative_interaction_edge"),
            })
    return output


def _summary(rows: list[dict[str, Any]], *, seed: int) -> dict[str, Any]:
    projected = [
        {
            "aligned_residual": float(r["aligned_residual"]),
            "hit": bool(r["hit"]),
        }
        for r in rows
    ]
    return cv._summary(projected, seed=seed)


def _slice(rows: list[dict[str, Any]], key: str, *,
           seed_base: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) if row.get(key) is not None else "unknown")].append(row)
    result = []
    for index, value in enumerate(sorted(grouped)):
        result.append({
            "value": value,
            **_summary(grouped[value], seed=seed_base + index),
        })
    return result


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    all_classified = _classified_with_context(
        repository, test_season=int(test_season))
    frozen = [
        r for r in all_classified
        if float(r["abs_margin_power_z"]) >= FROZEN_THRESHOLD
        and int(r["confirmation_count"]) == FROZEN_CONFIRMATIONS
    ]
    context_supported = [
        r for r in frozen if r["narrative_state"] == "agrees"
    ]

    slices = {}
    for index, key in enumerate((
        "season",
        "selected_side",
        "market_role",
        "spread_bucket",
        "total_bucket",
        "season_phase",
        "narrative_state",
    )):
        slices[key] = _slice(
            frozen, key, seed_base=810000 + index * 1000)

    return {
        "version": "convergence-robustness-v1",
        "test_through_season": int(test_season),
        "frozen_state": {
            "minimum_abs_margin_power_z": FROZEN_THRESHOLD,
            "required_confirmations": FROZEN_CONFIRMATIONS,
            "confirmation_definition": "structural cluster + Line Elo",
        },
        "all_classified_games": len(all_classified),
        "full_convergence_games": len(frozen),
        "full_convergence": _summary(frozen, seed=820001),
        "context_supported_full_convergence_games": len(context_supported),
        "context_supported_full_convergence": _summary(
            context_supported, seed=820002),
        "robustness_slices": slices,
        "game_rows": frozen,
        "notes": [
            "Slices are descriptive robustness checks; they do not select a new rule.",
            "Favorite/underdog is defined from the selected Margin Power side versus the consensus spread.",
            "Spread and total buckets are fixed before inspecting slice results.",
            "Game rows include every >=1.0 sigma Full Convergence observation for manual audit.",
        ],
    }


def _flatten_summary_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    overall = payload["full_convergence"]
    rows.append({
        "slice": "overall",
        "value": "full_convergence",
        **overall,
    })
    rows.append({
        "slice": "overall",
        "value": "context_supported",
        **payload["context_supported_full_convergence"],
    })
    for slice_name, items in payload["robustness_slices"].items():
        for item in items:
            rows.append({
                "slice": slice_name,
                **item,
            })
    return rows


def export_report(payload: dict[str, Any], output_dir: str) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "convergence_robustness.json")
    summary_path = os.path.join(output_dir, "convergence_robustness_summary.csv")
    games_path = os.path.join(output_dir, "full_convergence_games.csv")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    summary_rows = _flatten_summary_rows(payload)
    summary_fields = [
        "slice", "value", "n", "wins", "hit_rate", "hit_rate_ci95",
        "mean_aligned_residual", "mean_aligned_residual_bootstrap_ci95",
        "median_aligned_residual", "worst_aligned_miss", "best_aligned_result",
    ]
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_fields, extrasaction="ignore")
        writer.writeheader()
        for row in summary_rows:
            cooked = dict(row)
            for field in ("hit_rate_ci95", "mean_aligned_residual_bootstrap_ci95"):
                if isinstance(cooked.get(field), list):
                    cooked[field] = "|".join(
                        "" if v is None else str(v) for v in cooked[field])
            writer.writerow(cooked)

    game_rows = payload["game_rows"]
    if game_rows:
        fields = list(game_rows[0].keys())
        with open(games_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(game_rows)
    else:
        with open(games_path, "w", newline="", encoding="utf-8") as handle:
            handle.write("")

    return {
        "json": json_path,
        "summary_csv": summary_path,
        "games_csv": games_path,
    }


def compact_console_summary(payload: dict[str, Any],
                            paths: dict[str, str] | None = None) -> dict[str, Any]:
    overall = payload["full_convergence"]
    context = payload["context_supported_full_convergence"]
    return {
        "version": payload["version"],
        "test_through_season": payload["test_through_season"],
        "full_convergence": {
            "n": overall.get("n"),
            "hit_rate": overall.get("hit_rate"),
            "hit_rate_ci95": overall.get("hit_rate_ci95"),
            "mean_aligned_residual": overall.get("mean_aligned_residual"),
            "mean_ci95": overall.get("mean_aligned_residual_bootstrap_ci95"),
        },
        "context_supported": {
            "n": context.get("n"),
            "hit_rate": context.get("hit_rate"),
            "mean_aligned_residual": context.get("mean_aligned_residual"),
        },
        "files": paths or {},
    }
