"""Conditional convergence around Margin Power.

Primary hypothesis:
A genuinely independent Margin Power edge should become more predictive when
separate information families confirm it.

Primary signal:
- Margin Power

Independent confirmations:
- Structural cluster = mean standardized Football Lab / Elo / Efficiency Power
- Market state = standardized Line Elo

Context only:
- Narrative interaction, reported as agrees/opposes/missing rather than counted
  as a power-rating confirmation.

Everything is walk-forward: each target season is standardized from prior
seasons only. No threshold is selected on the target season.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any

from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.repository import CFBRepository

MARGIN_BUCKETS = (
    (0.0, 0.50, "<0.50"),
    (0.50, 1.00, "0.50-1.00"),
    (1.00, 1.50, "1.00-1.50"),
    (1.50, float("inf"), ">=1.50"),
)
MARGIN_THRESHOLDS = (0.50, 1.00, 1.50)
STRUCTURAL_KEYS = ("football_lab_edge", "elo_edge", "efficiency_power_edge")
MIN_SCALE_ROWS = 50
MIN_STRUCTURAL_COMPONENTS = 2


def _std(values) -> float:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 1.0
    avg = sum(vals) / len(vals)
    return math.sqrt(sum((v - avg) ** 2 for v in vals) / len(vals)) or 1.0


def _lens_scales(train: list[dict[str, Any]]) -> dict[str, float]:
    keys = (
        "margin_power_edge",
        "football_lab_edge",
        "elo_edge",
        "efficiency_power_edge",
        "line_elo_edge",
        "narrative_interaction_edge",
    )
    out = {}
    for key in keys:
        vals = [float(r[key]) for r in train if r.get(key) is not None]
        if len(vals) >= MIN_SCALE_ROWS:
            out[key] = _std(vals)
    return out


def _sign(value: float | None) -> int:
    if value is None:
        return 0
    return 1 if float(value) > 0 else -1 if float(value) < 0 else 0


def _state(row: dict[str, Any], scales: dict[str, float]) -> dict[str, Any] | None:
    if "margin_power_edge" not in scales or row.get("margin_power_edge") is None:
        return None
    margin_z = float(row["margin_power_edge"]) / float(scales["margin_power_edge"])
    primary_direction = _sign(margin_z)
    if not primary_direction:
        return None

    structural_zs = []
    structural_members = []
    for key in STRUCTURAL_KEYS:
        if key in scales and row.get(key) is not None:
            structural_zs.append(float(row[key]) / float(scales[key]))
            structural_members.append(key)
    structural_z = (
        sum(structural_zs) / len(structural_zs)
        if len(structural_zs) >= MIN_STRUCTURAL_COMPONENTS else None
    )
    structural_direction = _sign(structural_z)
    structural_confirms = (
        structural_direction == primary_direction
        if structural_direction else None
    )

    market_z = (
        float(row["line_elo_edge"]) / float(scales["line_elo_edge"])
        if "line_elo_edge" in scales and row.get("line_elo_edge") is not None
        else None
    )
    market_direction = _sign(market_z)
    market_confirms = (
        market_direction == primary_direction if market_direction else None
    )

    narrative_z = (
        float(row["narrative_interaction_edge"])
        / float(scales["narrative_interaction_edge"])
        if "narrative_interaction_edge" in scales
        and row.get("narrative_interaction_edge") is not None
        else None
    )
    narrative_direction = _sign(narrative_z)
    narrative_state = (
        "agrees" if narrative_direction == primary_direction
        else "opposes" if narrative_direction
        else "missing"
    )

    available_confirmations = sum(
        value is not None for value in (structural_confirms, market_confirms)
    )
    confirmation_count = sum(
        value is True for value in (structural_confirms, market_confirms)
    )
    if structural_confirms is True and market_confirms is True:
        combination = "structural+market"
    elif structural_confirms is True:
        combination = "structural_only"
    elif market_confirms is True:
        combination = "market_only"
    else:
        combination = "none"

    return {
        "margin_z": margin_z,
        "primary_direction": primary_direction,
        "structural_z": structural_z,
        "structural_members": structural_members,
        "structural_confirms": structural_confirms,
        "market_z": market_z,
        "market_confirms": market_confirms,
        "narrative_z": narrative_z,
        "narrative_state": narrative_state,
        "available_confirmations": available_confirmations,
        "confirmation_count": confirmation_count,
        "confirmation_combination": combination,
    }


def _summary(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    if not items:
        return {"n": 0}
    aligned = [
        float(row["market_margin_residual"]) * int(state["primary_direction"])
        for row, state in items
    ]
    return {
        "n": len(items),
        "directional_hit_rate": round(sum(v > 0 for v in aligned) / len(aligned), 4),
        "mean_aligned_residual": round(sum(aligned) / len(aligned), 3),
        "median_aligned_residual": round(float(median(aligned)), 3),
        "worst_aligned_miss": round(min(aligned), 3),
        "best_aligned_result": round(max(aligned), 3),
        "mean_abs_margin_z": round(
            sum(abs(float(state["margin_z"])) for _, state in items) / len(items), 3
        ),
        "mean_structural_z_aligned": round(
            sum(
                (float(state["structural_z"]) * int(state["primary_direction"]))
                for _, state in items if state["structural_z"] is not None
            )
            / max(1, sum(state["structural_z"] is not None for _, state in items)),
            3,
        ),
        "mean_market_z_aligned": round(
            sum(
                (float(state["market_z"]) * int(state["primary_direction"]))
                for _, state in items if state["market_z"] is not None
            )
            / max(1, sum(state["market_z"] is not None for _, state in items)),
            3,
        ),
    }


def _subset(
    rows: list[dict[str, Any]],
    scales: dict[str, float],
    *,
    low: float,
    high: float,
    confirmation_count: int | None = None,
    combination: str | None = None,
    narrative_state: str | None = None,
    require_both_confirmation_families: bool = True,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    out = []
    for row in rows:
        state = _state(row, scales)
        if state is None:
            continue
        mag = abs(float(state["margin_z"]))
        if not (float(low) <= mag < float(high)):
            continue
        if require_both_confirmation_families and state["available_confirmations"] < 2:
            continue
        if confirmation_count is not None and state["confirmation_count"] != confirmation_count:
            continue
        if combination is not None and state["confirmation_combination"] != combination:
            continue
        if narrative_state is not None and state["narrative_state"] != narrative_state:
            continue
        out.append((row, state))
    return out


def _season_report(
    train: list[dict[str, Any]],
    test: list[dict[str, Any]],
    season: int,
) -> dict[str, Any]:
    scales = _lens_scales(train)
    coverage = {
        key: {
            "rows": sum(1 for r in train if r.get(key) is not None),
            "active": key in scales,
            "std": round(scales[key], 4) if key in scales else None,
        }
        for key in (
            "margin_power_edge",
            "football_lab_edge",
            "elo_edge",
            "efficiency_power_edge",
            "line_elo_edge",
            "narrative_interaction_edge",
        )
    }

    magnitude_matrix = []
    for low, high, label in MARGIN_BUCKETS:
        by_count = {}
        for count in (0, 1, 2):
            by_count[str(count)] = _summary(
                _subset(
                    test, scales, low=low, high=high,
                    confirmation_count=count,
                )
            )
        magnitude_matrix.append({
            "margin_power_bucket": label,
            "confirmations": by_count,
        })

    combination_matrix = []
    for low, high, label in MARGIN_BUCKETS:
        combos = {}
        for combo in ("none", "structural_only", "market_only", "structural+market"):
            combos[combo] = _summary(
                _subset(
                    test, scales, low=low, high=high,
                    combination=combo,
                )
            )
        combination_matrix.append({
            "margin_power_bucket": label,
            "combinations": combos,
        })

    threshold_ladder = []
    for threshold in MARGIN_THRESHOLDS:
        rows_by_count = {}
        for count in (0, 1, 2):
            rows_by_count[str(count)] = _summary(
                _subset(
                    test, scales,
                    low=threshold, high=float("inf"),
                    confirmation_count=count,
                )
            )
        threshold_ladder.append({
            "minimum_abs_margin_power_z": threshold,
            "confirmations": rows_by_count,
        })

    narrative_overlay = []
    for threshold in MARGIN_THRESHOLDS:
        for combo in ("structural+market", "structural_only", "market_only"):
            base = _subset(
                test, scales,
                low=threshold, high=float("inf"),
                combination=combo,
            )
            if not base:
                continue
            narrative_overlay.append({
                "minimum_abs_margin_power_z": threshold,
                "confirmation_combination": combo,
                "all_context": _summary(base),
                "narrative_agrees": _summary(
                    _subset(
                        test, scales,
                        low=threshold, high=float("inf"),
                        combination=combo,
                        narrative_state="agrees",
                    )
                ),
                "narrative_opposes": _summary(
                    _subset(
                        test, scales,
                        low=threshold, high=float("inf"),
                        combination=combo,
                        narrative_state="opposes",
                    )
                ),
                "narrative_missing": _summary(
                    _subset(
                        test, scales,
                        low=threshold, high=float("inf"),
                        combination=combo,
                        narrative_state="missing",
                    )
                ),
            })

    # Direct test of the structural cluster independent of Margin Power.
    structural_membership = {}
    for key in STRUCTURAL_KEYS:
        structural_membership[key] = {
            "training_rows": sum(1 for r in train if r.get(key) is not None),
            "active": key in scales,
        }

    return {
        "season": int(season),
        "train_rows": len(train),
        "test_rows": len(test),
        "coverage": coverage,
        "structural_cluster": {
            "members": list(STRUCTURAL_KEYS),
            "minimum_available_members": MIN_STRUCTURAL_COMPONENTS,
            "member_status": structural_membership,
        },
        "magnitude_x_confirmation_count": magnitude_matrix,
        "magnitude_x_confirmation_combination": combination_matrix,
        "cumulative_margin_thresholds": threshold_ladder,
        "narrative_context_overlay": narrative_overlay,
    }


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    rows = ipl.build_lens_rows(repository, test_season=int(test_season))
    seasons = sorted({int(r["season"]) for r in rows})
    walk = []
    for season in [s for s in seasons if s > min(seasons) and s <= int(test_season)]:
        train = [r for r in rows if int(r["season"]) < season]
        test = [r for r in rows if int(r["season"]) == season]
        if train and test:
            walk.append(_season_report(train, test, season))
    return {
        "version": "conditional-convergence-v1",
        "test_season": int(test_season),
        "primary_signal": "margin_power_edge",
        "confirmation_families": {
            "structural": {
                "members": list(STRUCTURAL_KEYS),
                "method": "mean of prior-history standardized available members; requires at least two",
            },
            "market": {
                "member": "line_elo_edge",
                "method": "sign confirmation versus Margin Power",
            },
        },
        "context": {
            "narrative": (
                "Narrative interaction is not counted as a power confirmation; "
                "it is reported as agrees/opposes/missing."
            )
        },
        "margin_power_magnitude_buckets": [label for _, _, label in MARGIN_BUCKETS],
        "walk_forward_years": walk,
        "notes": [
            "Every target season uses only prior seasons for normalization.",
            "Confirmation count is 0-2: structural cluster and market state.",
            "Rows require both confirmation families to be available for count/combo comparisons.",
            "Narrative is a context split, not an equal vote.",
            "No threshold is optimized on the target season.",
        ],
    }
