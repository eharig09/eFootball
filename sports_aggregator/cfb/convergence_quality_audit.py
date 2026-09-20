"""Input-quality and key-number diagnostics for frozen Full Convergence.

This is descriptive research only. It does not alter the frozen Full
Convergence definition or optimize thresholds from outcomes.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import convergence_action_policy as cap
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.repository import CFBRepository

CORE_LENSES = (
    "margin_power_edge",
    "football_lab_edge",
    "elo_edge",
    "efficiency_power_edge",
    "line_elo_edge",
)


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _key_number_region(abs_spread: float | None) -> str:
    if abs_spread is None:
        return "unknown"
    value = float(abs_spread)
    if value < 2.75:
        return "below_3"
    if value <= 3.25:
        return "around_3"
    if value < 6.75:
        return "between_3_and_7"
    if value <= 7.25:
        return "around_7"
    if value < 14.0:
        return "7_to_13.5"
    return "14_plus"


def _half_point_spread(abs_spread: float | None) -> str:
    if abs_spread is None:
        return "unknown"
    rounded = round(float(abs_spread) * 2.0) / 2.0
    return f"{rounded:.1f}"


def _result_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    wins = sum(float(r["aligned_residual"]) > 0 for r in rows)
    losses = sum(float(r["aligned_residual"]) < 0 for r in rows)
    pushes = len(rows) - wins - losses
    decisions = wins + losses
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_ex_pushes": round(wins / decisions, 4) if decisions else None,
        "mean_aligned_residual": round(
            sum(float(r["aligned_residual"]) for r in rows) / len(rows), 3
        ),
    }


def _group_summary(rows: list[dict[str, Any]], key) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(key(row))].append(row)
    return [
        {"value": value, **_result_summary(groups[value])}
        for value in sorted(groups)
    ]


def report(
    repository: CFBRepository,
    *,
    from_season: int = 2021,
    to_season: int = 2025,
) -> dict[str, Any]:
    raw = ipl.build_lens_rows(repository, test_season=int(to_season))
    raw = [
        r for r in raw
        if int(from_season) <= int(r["season"]) <= int(to_season)
    ]

    full = cap._full_rows(repository, test_season=int(to_season))
    full = [
        r for r in full
        if int(from_season) <= int(r["season"]) <= int(to_season)
    ]
    for row in full:
        spread = row.get("market_home_margin")
        row["abs_market_spread"] = abs(float(spread)) if spread is not None else None
        row["key_number_region"] = _key_number_region(row["abs_market_spread"])
        row["rounded_half_point_spread"] = _half_point_spread(row["abs_market_spread"])
        actual_margin = row.get("actual_home_margin")
        row["abs_actual_margin"] = (
            abs(float(actual_margin)) if actual_margin is not None else None
        )
        row["actual_margin_landed_key"] = (
            "3" if row["abs_actual_margin"] == 3.0
            else "7" if row["abs_actual_margin"] == 7.0
            else "other"
        )

    season_quality = []
    for season in range(int(from_season), int(to_season) + 1):
        season_rows = [r for r in raw if int(r["season"]) == season]
        full_rows = [r for r in full if int(r["season"]) == season]
        lens_coverage = {}
        for lens in CORE_LENSES:
            n = sum(r.get(lens) is not None for r in season_rows)
            lens_coverage[lens] = {
                "rows": n,
                "coverage_rate": _rate(n, len(season_rows)),
            }
        all_core = sum(
            all(r.get(lens) is not None for lens in CORE_LENSES)
            for r in season_rows
        )
        all_structural = sum(
            all(r.get(lens) is not None for lens in (
                "football_lab_edge", "elo_edge", "efficiency_power_edge"
            ))
            for r in season_rows
        )
        full_all_structural = sum(
            all(r.get(lens) is not None for lens in (
                "football_lab_edge", "elo_edge", "efficiency_power_edge"
            ))
            for r in full_rows
        )
        season_quality.append({
            "season": season,
            "lens_rows": len(season_rows),
            "lens_coverage": lens_coverage,
            "all_core_lenses_rows": all_core,
            "all_core_lenses_rate": _rate(all_core, len(season_rows)),
            "all_three_structural_rows": all_structural,
            "all_three_structural_rate": _rate(all_structural, len(season_rows)),
            "full_convergence": {
                **_result_summary(full_rows),
                "all_three_structural_rows": full_all_structural,
                "all_three_structural_rate": _rate(
                    full_all_structural, len(full_rows)
                ),
            },
        })

    between = [r for r in full if r["key_number_region"] == "between_3_and_7"]

    return {
        "version": "convergence-quality-key-number-audit-v1",
        "window": [int(from_season), int(to_season)],
        "frozen_definition_unchanged": {
            "minimum_abs_margin_power_z": cap.FROZEN_THRESHOLD,
            "required_confirmations": 2,
        },
        "season_input_quality": season_quality,
        "full_convergence_overall": _result_summary(full),
        "full_convergence_by_key_number_region": _group_summary(
            full, lambda r: r["key_number_region"]
        ),
        "between_3_and_7": {
            "overall": _result_summary(between),
            "by_season": _group_summary(between, lambda r: r["season"]),
            "by_market_role": _group_summary(
                between, lambda r: r.get("market_role", "unknown")
            ),
            "by_selected_side": _group_summary(
                between, lambda r: r.get("selected_side", "unknown")
            ),
            "by_half_point_band": _group_summary(
                between,
                lambda r: (
                    "3.5-4.5" if float(r["abs_market_spread"]) < 4.75
                    else "4.75-5.5" if float(r["abs_market_spread"]) < 5.75
                    else "5.75-6.5"
                ),
            ),
            "by_rounded_half_point_spread": _group_summary(
                between, lambda r: r["rounded_half_point_spread"]
            ),
            "by_actual_margin_landed_key": _group_summary(
                between, lambda r: r["actual_margin_landed_key"]
            ),
        },
        "notes": [
            "Key-number regions use a +/-0.25 window around 3 and 7 because provider consensus spreads can average across books.",
            "The between-3-and-7 diagnostic is descriptive and does not create a new betting rule.",
            "Consensus spreads are also rounded to the nearest half point for micro-bucket inspection; raw consensus values remain unchanged for classification.",
            "Actual final margins landing exactly on 3 or 7 are reported to test whether key-number outcomes disproportionately drive the ATS result.",
            "Input-quality rates are computed on the same historical lens rows used by the convergence research.",
        ],
    }
