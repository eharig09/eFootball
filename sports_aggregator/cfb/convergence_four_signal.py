"""Backtest HC/QB Elo as a fourth convergence vote.

The frozen convergence model has three directional signals:
1. Margin Power (primary / side selector)
2. Structural cluster
3. Line Elo

This research layer adds the already-derived combined HC/QB Elo direction as a
fourth vote without changing the production definition. The primary Margin
Power gate remains frozen at >= 1.0 walk-forward sigma. We deliberately start
from convergence_robustness._classified_with_context rather than Full
Convergence so 3/4, 2/4, and 1/4 states can be measured instead of conditioning
on the existing 3/3 winner first.

Because Margin Power defines the tested side, it always contributes one vote.
Therefore the exact ladder is 4/4, 3/4, 2/4, 1/4; a 0/4 state is not defined in
this primary-signal-confirmation framework.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import convergence_robustness as cr
from sports_aggregator.cfb import convergence_validation as cv
from sports_aggregator.cfb.rating_predictive_power import _load_dataset, _scored_rows
from sports_aggregator.cfb.rating_walkforward import walk_forward_combined_scores
from sports_aggregator.cfb.repository import CFBRepository

FROZEN_MARGIN_THRESHOLD = 1.0
SPREAD_BUCKETS = ("<3", "3-6.5", "7-13.5", "14+")


def _summary(rows: list[dict[str, Any]], *, seed: int) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    projected = [
        {"aligned_residual": float(r["aligned_residual"]), "hit": bool(r["hit"])}
        for r in rows
    ]
    return cv._summary(projected, seed=seed)


def _agreement_count(row: dict[str, Any]) -> int:
    return (
        1
        + int(float(row["structural_z_aligned"]) > 0)
        + int(float(row["line_elo_z_aligned"]) > 0)
        + int(float(row["hc_qb_elo_aligned"]) > 0)
    )


def _old_agreement_count(row: dict[str, Any]) -> int:
    return (
        1
        + int(float(row["structural_z_aligned"]) > 0)
        + int(float(row["line_elo_z_aligned"]) > 0)
    )


def _joined_rows(
    repository: CFBRepository,
    *,
    test_season: int = 2025,
    elo_start_season: int = 2015,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    classified = cr._classified_with_context(repository, test_season=int(test_season))
    margin_gate = [
        r for r in classified
        if float(r["abs_margin_power_z"]) >= FROZEN_MARGIN_THRESHOLD
    ]

    elo_source = _load_dataset(
        repository,
        start_season=int(elo_start_season),
        end_season=int(test_season),
    )
    elo_scores, elo_normalization = walk_forward_combined_scores(elo_source)

    complete: list[dict[str, Any]] = []
    missing_elo = 0
    neutral_elo = 0
    for row in margin_gate:
        z = elo_scores.get(int(row["game_id"]))
        if z is None:
            missing_elo += 1
            continue
        if abs(z) <= 1e-12:
            neutral_elo += 1
            continue

        cooked = dict(row)
        direction = 1 if row["selected_side"] == "home" else -1
        cooked["hc_qb_elo_z"] = z
        cooked["hc_qb_elo_aligned"] = z * direction
        cooked["hc_qb_elo_confirms"] = cooked["hc_qb_elo_aligned"] > 0
        cooked["old_agreement_count"] = _old_agreement_count(cooked)
        cooked["agreement_count"] = _agreement_count(cooked)
        cooked["old_agreement_label"] = f'{cooked["old_agreement_count"]}/3'
        cooked["agreement_label"] = f'{cooked["agreement_count"]}/4'
        complete.append(cooked)

    coverage = {
        "classified_with_structural_and_line_elo_available": len(classified),
        "margin_power_ge_1_sigma": len(margin_gate),
        "complete_four_signal_rows": len(complete),
        "missing_hc_qb_elo": missing_elo,
        "neutral_hc_qb_elo": neutral_elo,
        "hc_qb_normalization": elo_normalization,
    }
    return complete, coverage


def exact_ladder_report(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[int(row["agreement_count"])].append(row)
    return [
        {"agreement": f"{count}/4", **_summary(groups.get(count, []), seed=970000 + count)}
        for count in (4, 3, 2, 1)
    ]


def cumulative_ladder_report(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for minimum in (4, 3, 2, 1):
        group = [r for r in rows if int(r["agreement_count"]) >= minimum]
        out.append({
            "rule": f">={minimum}/4",
            "minimum_agreements": minimum,
            "coverage_pct_of_complete_sample": (
                round(len(group) / len(rows), 4) if rows else None
            ),
            **_summary(group, seed=971000 + minimum),
        })
    return out


def old_vs_new_transition_report(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    index = 0
    for old_count in (3, 2, 1):
        for elo_confirms in (True, False):
            group = [
                r for r in rows
                if int(r["old_agreement_count"]) == old_count
                and bool(r["hc_qb_elo_confirms"]) is elo_confirms
            ]
            new_count = old_count + int(elo_confirms)
            out.append({
                "old_state": f"{old_count}/3",
                "hc_qb_elo": "agrees" if elo_confirms else "disagrees",
                "new_state": f"{new_count}/4",
                **_summary(group, seed=972000 + index),
            })
            index += 1
    return out


def old_three_signal_ladder(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[int(row["old_agreement_count"])].append(row)
    return [
        {"agreement": f"{count}/3", **_summary(groups.get(count, []), seed=973000 + count)}
        for count in (3, 2, 1)
    ]


def _three_of_four_origin(row: dict[str, Any]) -> str | None:
    """Identify the two distinct paths that produce an exact 3/4 state."""
    if int(row["agreement_count"]) != 3:
        return None
    old_count = int(row["old_agreement_count"])
    elo_confirms = bool(row["hc_qb_elo_confirms"])
    if old_count == 3 and not elo_confirms:
        return "old_3_of_3_elo_disagrees"
    if old_count == 2 and elo_confirms:
        return "old_2_of_3_elo_agrees"
    return "unexpected"


def three_of_four_origin_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Do not pool promoted and downgraded 3/4 states.

    An exact 3/4 can mean either:
    - old 3/3, but HC/QB Elo disagreed (a downgrade), or
    - old 2/3, but HC/QB Elo agreed (a promotion).
    """
    origins = ("old_3_of_3_elo_disagrees", "old_2_of_3_elo_agrees")
    out: dict[str, Any] = {}
    for index, origin in enumerate(origins):
        group = [r for r in rows if _three_of_four_origin(r) == origin]
        out[origin] = {
            "overall": _summary(group, seed=976000 + index),
            "by_spread_bucket": {
                bucket: _summary(
                    [r for r in group if r.get("spread_bucket") == bucket],
                    seed=976100 + index * 10 + bucket_index,
                )
                for bucket_index, bucket in enumerate(SPREAD_BUCKETS)
            },
        }
    return out


POLICY_NAMES = (
    "old_full_convergence_baseline",
    "four_of_four_only",
    "at_least_three_of_four",
    "at_least_three_of_four_exclude_14_plus",
    "four_of_four_plus_promoted_two_of_three_exclude_14_plus",
)


def _policy_rows(rows: list[dict[str, Any]], policy: str) -> list[dict[str, Any]]:
    if policy == "old_full_convergence_baseline":
        return [r for r in rows if int(r["old_agreement_count"]) == 3]
    if policy == "four_of_four_only":
        return [r for r in rows if int(r["agreement_count"]) == 4]
    if policy == "at_least_three_of_four":
        return [r for r in rows if int(r["agreement_count"]) >= 3]
    if policy == "at_least_three_of_four_exclude_14_plus":
        return [
            r for r in rows
            if int(r["agreement_count"]) >= 3 and r.get("spread_bucket") != "14+"
        ]
    if policy == "four_of_four_plus_promoted_two_of_three_exclude_14_plus":
        return [
            r for r in rows
            if r.get("spread_bucket") != "14+"
            and (
                int(r["agreement_count"]) == 4
                or (
                    int(r["old_agreement_count"]) == 2
                    and bool(r["hc_qb_elo_confirms"])
                )
            )
        ]
    raise ValueError(f"unknown policy: {policy}")


def policy_comparison_report(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for index, policy in enumerate(POLICY_NAMES):
        group = _policy_rows(rows, policy)
        out.append({
            "policy": policy,
            "coverage_pct_of_complete_sample": (
                round(len(group) / len(rows), 4) if rows else None
            ),
            **_summary(group, seed=977000 + index),
        })
    return out


def policy_by_season_report(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    seasons = sorted({int(r["season"]) for r in rows})
    out: dict[str, list[dict[str, Any]]] = {}
    for policy_index, policy in enumerate(POLICY_NAMES):
        policy_rows = _policy_rows(rows, policy)
        out[policy] = [
            {
                "season": season,
                **_summary(
                    [r for r in policy_rows if int(r["season"]) == season],
                    seed=978000 + policy_index * 100 + season,
                ),
            }
            for season in seasons
        ]
    return out


def _grouped_exact(
    rows: list[dict[str, Any]],
    *,
    key: str,
    values: list[Any],
    seed_base: int,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for value_index, value in enumerate(values):
        subset = [r for r in rows if r.get(key) == value]
        groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in subset:
            groups[int(row["agreement_count"])].append(row)
        out[str(value)] = [
            {
                "agreement": f"{count}/4",
                **_summary(groups.get(count, []), seed=seed_base + value_index * 10 + count),
            }
            for count in (4, 3, 2, 1)
        ]
    return out


def report(
    repository: CFBRepository,
    *,
    test_season: int = 2025,
    elo_start_season: int = 2015,
) -> dict[str, Any]:
    rows, coverage = _joined_rows(
        repository,
        test_season=int(test_season),
        elo_start_season=int(elo_start_season),
    )
    seasons = sorted({int(r["season"]) for r in rows})

    return {
        "version": "cfb-convergence-four-signal-v2",
        "test_through_season": int(test_season),
        "elo_start_season": int(elo_start_season),
        "frozen_frame": {
            "primary_signal": "Margin Power",
            "minimum_abs_margin_power_z": FROZEN_MARGIN_THRESHOLD,
            "existing_confirmations": ["Structural cluster", "Line Elo"],
            "candidate_fourth_signal": "combined HC/QB Elo",
            "side_selection": "Margin Power direction",
            "zero_of_four_note": (
                "0/4 is undefined because Margin Power selects the tested side "
                "and therefore always contributes one agreement."
            ),
        },
        "coverage": coverage,
        "existing_three_signal_exact_ladder_on_same_complete_sample": old_three_signal_ladder(rows),
        "four_signal_exact_ladder": exact_ladder_report(rows),
        "four_signal_cumulative_widening": cumulative_ladder_report(rows),
        "elo_transition_from_existing_three_signal_states": old_vs_new_transition_report(rows),
        "three_of_four_origin_analysis": three_of_four_origin_report(rows),
        "predeclared_policy_comparison": policy_comparison_report(rows),
        "predeclared_policies_by_season": policy_by_season_report(rows),
        "four_signal_exact_by_season": _grouped_exact(
            rows, key="season", values=seasons, seed_base=974000
        ),
        "four_signal_exact_by_spread_bucket": _grouped_exact(
            rows, key="spread_bucket", values=list(SPREAD_BUCKETS), seed_base=975000
        ),
        "game_rows": rows,
        "notes": [
            "Research only: this does not modify the frozen Full Convergence production definition.",
            "All original convergence z-scores remain walk-forward and are reused from convergence_robustness.",
            "HC/QB Elo combines leak-safe pregame ratings with prior-seasons-only walk-forward normalization.",
            "Exact tiers answer quality by agreement count; cumulative tiers answer the volume-versus-hit-rate widening question.",
            "The transition table is the cleanest direct test of whether HC/QB Elo adds information beyond the old three-signal state.",
            "Exact 3/4 is split by origin because old 3/3 + Elo disagreement and old 2/3 + Elo agreement represent different information states.",
            "Policy comparisons are predeclared from the prior research read: old Full Convergence, 4/4, >=3/4, >=3/4 excluding 14+, and 4/4 plus promoted 2/3 games excluding 14+.",
            "The old Full Convergence baseline is evaluated on the same HC/QB-Elo-complete sample for apples-to-apples comparison.",
            "Read year-by-year stability and sample size alongside pooled hit rate before promoting any threshold.",
        ],
    }
