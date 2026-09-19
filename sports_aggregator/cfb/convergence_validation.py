"""Frozen validation report for conditional convergence.

This module does not search for new thresholds or feature definitions.

Frozen hypothesis:
- Primary: Margin Power.
- Confirmation 1: structural cluster (Football Lab + Elo + Efficiency Power).
- Confirmation 2: Line Elo market state.
- Narrative: context only.
- Margin thresholds: |z| >= 0.50, 1.00, 1.50.

Each target season is classified using scales learned from prior seasons only.
Pooled results combine already-classified target-season observations; they do
not recompute a global scale.

Uncertainty:
- Wilson 95% interval for directional hit rate.
- Deterministic bootstrap 95% interval for mean aligned market residual.
"""
from __future__ import annotations

import math
import random
from statistics import median
from typing import Any

from sports_aggregator.cfb import conditional_convergence as cc
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.repository import CFBRepository

THRESHOLDS = (0.50, 1.00, 1.50)
CONFIRMATION_COUNTS = (0, 1, 2)
BOOTSTRAP_DRAWS = 1000
CI_LEVEL = 0.95


def _wilson_interval(successes: int, n: int, z: float = 1.959963984540054
                     ) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    radius = z * math.sqrt((p * (1.0 - p) / n) + z * z / (4.0 * n * n)) / denom
    return max(0.0, center - radius), min(1.0, center + radius)


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * q
    lo, hi = int(math.floor(position)), int(math.ceil(position))
    if lo == hi:
        return sorted_values[lo]
    weight = position - lo
    return sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight


def _bootstrap_mean_interval(values: list[float], *,
                             draws: int = BOOTSTRAP_DRAWS,
                             seed: int = 20260919
                             ) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(int(seed))
    n = len(values)
    means = []
    for _ in range(int(draws)):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    alpha = (1.0 - CI_LEVEL) / 2.0
    return _percentile(means, alpha), _percentile(means, 1.0 - alpha)


def _classified_rows(rows: list[dict[str, Any]],
                     scales: dict[str, float],
                     season: int) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        state = cc._state(row, scales)
        if state is None or state["available_confirmations"] < 2:
            continue
        aligned = (
            float(row["market_margin_residual"])
            * int(state["primary_direction"])
        )
        output.append({
            "season": int(season),
            "game_id": int(row["game_id"]),
            "aligned_residual": aligned,
            "hit": aligned > 0,
            "margin_abs_z": abs(float(state["margin_z"])),
            "confirmation_count": int(state["confirmation_count"]),
            "confirmation_combination": str(state["confirmation_combination"]),
            "narrative_state": str(state["narrative_state"]),
            "structural_z_aligned": (
                float(state["structural_z"]) * int(state["primary_direction"])
                if state["structural_z"] is not None else None
            ),
            "market_z_aligned": (
                float(state["market_z"]) * int(state["primary_direction"])
                if state["market_z"] is not None else None
            ),
        })
    return output


def _summary(rows: list[dict[str, Any]], *, seed: int) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "hit_rate": None,
            "hit_rate_ci95": [None, None],
            "mean_aligned_residual": None,
            "mean_aligned_residual_bootstrap_ci95": [None, None],
            "median_aligned_residual": None,
        }
    aligned = [float(r["aligned_residual"]) for r in rows]
    successes = sum(bool(r["hit"]) for r in rows)
    hit_low, hit_high = _wilson_interval(successes, len(rows))
    mean_low, mean_high = _bootstrap_mean_interval(aligned, seed=seed)
    return {
        "n": len(rows),
        "wins": successes,
        "hit_rate": round(successes / len(rows), 4),
        "hit_rate_ci95": [
            round(hit_low, 4) if hit_low is not None else None,
            round(hit_high, 4) if hit_high is not None else None,
        ],
        "mean_aligned_residual": round(sum(aligned) / len(aligned), 3),
        "mean_aligned_residual_bootstrap_ci95": [
            round(mean_low, 3) if mean_low is not None else None,
            round(mean_high, 3) if mean_high is not None else None,
        ],
        "median_aligned_residual": round(float(median(aligned)), 3),
        "worst_aligned_miss": round(min(aligned), 3),
        "best_aligned_result": round(max(aligned), 3),
    }


def _cell(rows: list[dict[str, Any]], *, threshold: float,
          confirmation_count: int | None = None,
          combination: str | None = None,
          narrative_state: str | None = None) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if float(row["margin_abs_z"]) < float(threshold):
            continue
        if confirmation_count is not None and int(row["confirmation_count"]) != int(confirmation_count):
            continue
        if combination is not None and row["confirmation_combination"] != combination:
            continue
        if narrative_state is not None and row["narrative_state"] != narrative_state:
            continue
        result.append(row)
    return result


def _validation_table(rows: list[dict[str, Any]], *, seed_base: int) -> list[dict[str, Any]]:
    table = []
    for t_index, threshold in enumerate(THRESHOLDS):
        states = {}
        for count in CONFIRMATION_COUNTS:
            cell = _cell(rows, threshold=threshold, confirmation_count=count)
            states[str(count)] = _summary(
                cell, seed=seed_base + t_index * 100 + count
            )
        table.append({
            "minimum_abs_margin_power_z": threshold,
            "confirmations": states,
        })
    return table


def _combination_table(rows: list[dict[str, Any]], *, seed_base: int) -> list[dict[str, Any]]:
    combos = ("none", "structural_only", "market_only", "structural+market")
    table = []
    for t_index, threshold in enumerate(THRESHOLDS):
        values = {}
        for c_index, combo in enumerate(combos):
            values[combo] = _summary(
                _cell(rows, threshold=threshold, combination=combo),
                seed=seed_base + t_index * 100 + c_index,
            )
        table.append({
            "minimum_abs_margin_power_z": threshold,
            "combinations": values,
        })
    return table


def _narrative_table(rows: list[dict[str, Any]], *, seed_base: int) -> list[dict[str, Any]]:
    table = []
    for t_index, threshold in enumerate(THRESHOLDS):
        full = _cell(
            rows, threshold=threshold, combination="structural+market")
        table.append({
            "minimum_abs_margin_power_z": threshold,
            "all_context": _summary(full, seed=seed_base + t_index * 100),
            "narrative_agrees": _summary(
                _cell(
                    rows, threshold=threshold,
                    combination="structural+market",
                    narrative_state="agrees",
                ),
                seed=seed_base + t_index * 100 + 1,
            ),
            "narrative_opposes": _summary(
                _cell(
                    rows, threshold=threshold,
                    combination="structural+market",
                    narrative_state="opposes",
                ),
                seed=seed_base + t_index * 100 + 2,
            ),
            "narrative_missing": _summary(
                _cell(
                    rows, threshold=threshold,
                    combination="structural+market",
                    narrative_state="missing",
                ),
                seed=seed_base + t_index * 100 + 3,
            ),
        })
    return table


def _monotonicity(table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for entry in table:
        states = entry["confirmations"]
        hit_rates = [states[str(i)]["hit_rate"] for i in CONFIRMATION_COUNTS]
        means = [states[str(i)]["mean_aligned_residual"] for i in CONFIRMATION_COUNTS]
        enough = all(states[str(i)]["n"] > 0 for i in CONFIRMATION_COUNTS)
        hit_monotonic = (
            enough and all(hit_rates[i] <= hit_rates[i + 1]
                           for i in range(len(hit_rates) - 1))
        )
        mean_monotonic = (
            enough and all(means[i] <= means[i + 1]
                           for i in range(len(means) - 1))
        )
        result.append({
            "minimum_abs_margin_power_z": entry["minimum_abs_margin_power_z"],
            "all_states_present": enough,
            "hit_rate_monotonic_0_to_2": bool(hit_monotonic),
            "mean_residual_monotonic_0_to_2": bool(mean_monotonic),
            "hit_rates": hit_rates,
            "mean_aligned_residuals": means,
        })
    return result


def _difference_bootstrap(a: list[dict[str, Any]],
                          b: list[dict[str, Any]], *,
                          seed: int) -> dict[str, Any]:
    """Bootstrap difference: group A mean aligned residual minus group B."""
    if not a or not b:
        return {
            "mean_difference": None,
            "bootstrap_ci95": [None, None],
        }
    av = [float(r["aligned_residual"]) for r in a]
    bv = [float(r["aligned_residual"]) for r in b]
    observed = sum(av) / len(av) - sum(bv) / len(bv)
    rng = random.Random(seed)
    diffs = []
    for _ in range(BOOTSTRAP_DRAWS):
        am = sum(av[rng.randrange(len(av))] for _ in range(len(av))) / len(av)
        bm = sum(bv[rng.randrange(len(bv))] for _ in range(len(bv))) / len(bv)
        diffs.append(am - bm)
    diffs.sort()
    low = _percentile(diffs, 0.025)
    high = _percentile(diffs, 0.975)
    return {
        "mean_difference": round(observed, 3),
        "bootstrap_ci95": [
            round(low, 3) if low is not None else None,
            round(high, 3) if high is not None else None,
        ],
    }


def _full_vs_unconfirmed(rows: list[dict[str, Any]], *, seed_base: int
                         ) -> list[dict[str, Any]]:
    result = []
    for index, threshold in enumerate(THRESHOLDS):
        full = _cell(rows, threshold=threshold, confirmation_count=2)
        none = _cell(rows, threshold=threshold, confirmation_count=0)
        result.append({
            "minimum_abs_margin_power_z": threshold,
            "full_confirmation": _summary(full, seed=seed_base + index * 10),
            "unconfirmed": _summary(none, seed=seed_base + index * 10 + 1),
            "mean_residual_difference_full_minus_unconfirmed": _difference_bootstrap(
                full, none, seed=seed_base + index * 10 + 2
            ),
        })
    return result


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    raw_rows = ipl.build_lens_rows(repository, test_season=int(test_season))
    seasons = sorted({int(r["season"]) for r in raw_rows})

    classified_by_season: dict[int, list[dict[str, Any]]] = {}
    per_year = []

    for season in [s for s in seasons if s > min(seasons) and s <= int(test_season)]:
        train = [r for r in raw_rows if int(r["season"]) < season]
        test = [r for r in raw_rows if int(r["season"]) == season]
        scales = cc._lens_scales(train)
        classified = _classified_rows(test, scales, season)
        classified_by_season[season] = classified
        table = _validation_table(classified, seed_base=season * 1000)
        per_year.append({
            "season": season,
            "classified_games": len(classified),
            "fixed_threshold_validation": table,
            "exact_confirmation_combinations": _combination_table(
                classified, seed_base=season * 2000),
            "full_confirmation_narrative_context": _narrative_table(
                classified, seed_base=season * 3000),
            "monotonicity": _monotonicity(table),
            "full_vs_unconfirmed": _full_vs_unconfirmed(
                classified, seed_base=season * 4000),
        })

    pooled = [
        row
        for season in sorted(classified_by_season)
        for row in classified_by_season[season]
    ]
    pooled_table = _validation_table(pooled, seed_base=990000)
    return {
        "version": "conditional-convergence-validation-v1",
        "test_through_season": int(test_season),
        "frozen_hypothesis": {
            "primary": "Margin Power",
            "margin_thresholds": list(THRESHOLDS),
            "confirmation_1": "Structural cluster: Football Lab + Elo + Efficiency Power",
            "confirmation_2": "Line Elo market state",
            "narrative_role": "Context only; agrees/opposes/missing",
        },
        "uncertainty": {
            "hit_rate_interval": "Wilson 95%",
            "mean_aligned_residual_interval": (
                f"deterministic {BOOTSTRAP_DRAWS}-draw bootstrap 95%"
            ),
        },
        "per_year": per_year,
        "pooled_walk_forward": {
            "seasons": sorted(classified_by_season),
            "classified_games": len(pooled),
            "fixed_threshold_validation": pooled_table,
            "exact_confirmation_combinations": _combination_table(
                pooled, seed_base=991000),
            "full_confirmation_narrative_context": _narrative_table(
                pooled, seed_base=992000),
            "monotonicity": _monotonicity(pooled_table),
            "full_vs_unconfirmed": _full_vs_unconfirmed(
                pooled, seed_base=993000),
        },
        "notes": [
            "No thresholds, lens definitions, or confirmation rules are selected from these results.",
            "Each season is normalized from prior seasons only before pooled aggregation.",
            "Pooled results combine already-classified walk-forward observations.",
            "Confidence intervals quantify sampling uncertainty; they are not betting guarantees.",
        ],
    }
