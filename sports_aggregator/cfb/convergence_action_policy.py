"""Frozen action-policy and confirmation-strength study.

This report does NOT redefine Full Convergence. It starts from the frozen
>=1.0 sigma + structural + Line Elo state and evaluates hypothetical actions:

- keep the original side,
- pass,
- fade (take the opposite side),

for predeclared warning conditions:
- spread magnitude >= 14,
- Narrative missing,
- weak confirmation magnitude.

It also buckets structural and Line Elo confirmation strength using fixed
thresholds (0.25, 0.50 z) without optimizing them on outcomes.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from typing import Any, Callable

from sports_aggregator.cfb import convergence_robustness as cr
from sports_aggregator.cfb import convergence_validation as cv
from sports_aggregator.cfb.repository import CFBRepository

FROZEN_THRESHOLD = 1.0
CONFIRMATION_STRENGTH_CUTS = (0.25, 0.50)


def _strength_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    v = abs(float(value))
    if v < 0.25:
        return "weak_<0.25"
    if v < 0.50:
        return "moderate_0.25_0.50"
    return "strong_>=0.50"


def _joint_strength(row: dict[str, Any]) -> str:
    s = row.get("structural_z_aligned")
    m = row.get("line_elo_z_aligned")
    if s is None or m is None:
        return "missing"
    weakest = min(float(s), float(m))
    if weakest < 0.25:
        return "weak_any_<0.25"
    if weakest < 0.50:
        return "moderate_both_>=0.25"
    return "strong_both_>=0.50"


def _full_rows(repository: CFBRepository, *, test_season: int) -> list[dict[str, Any]]:
    rows = cr._classified_with_context(repository, test_season=int(test_season))
    out = []
    for row in rows:
        if (
            float(row["abs_margin_power_z"]) >= FROZEN_THRESHOLD
            and int(row["confirmation_count"]) == 2
        ):
            cooked = dict(row)
            cooked["structural_strength"] = _strength_bucket(
                cooked.get("structural_z_aligned"))
            cooked["line_elo_strength"] = _strength_bucket(
                cooked.get("line_elo_z_aligned"))
            cooked["joint_confirmation_strength"] = _joint_strength(cooked)
            out.append(cooked)
    return out


def _acted_rows(rows: list[dict[str, Any]], *,
                action: str) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if action == "pass":
            continue
        cooked = dict(row)
        if action == "fade":
            cooked["action_aligned_residual"] = -float(row["aligned_residual"])
        else:
            cooked["action_aligned_residual"] = float(row["aligned_residual"])
        cooked["action_hit"] = cooked["action_aligned_residual"] > 0
        result.append(cooked)
    return result


def _action_summary(rows: list[dict[str, Any]], *,
                    action: str,
                    seed: int) -> dict[str, Any]:
    acted = _acted_rows(rows, action=action)
    if action == "pass":
        return {
            "eligible_n": len(rows),
            "acted_n": 0,
            "action": "pass",
        }
    projected = [
        {
            "aligned_residual": float(r["action_aligned_residual"]),
            "hit": bool(r["action_hit"]),
        }
        for r in acted
    ]
    summary = cv._summary(projected, seed=seed)
    summary["eligible_n"] = len(rows)
    summary["acted_n"] = len(acted)
    summary["action"] = action
    return summary


def _split(rows: list[dict[str, Any]],
           predicate: Callable[[dict[str, Any]], bool]
           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    yes, no = [], []
    for row in rows:
        (yes if predicate(row) else no).append(row)
    return yes, no


def _policy_summary(rows: list[dict[str, Any]], *,
                    name: str,
                    chooser: Callable[[dict[str, Any]], str],
                    seed: int) -> dict[str, Any]:
    acted = []
    counts = defaultdict(int)
    for row in rows:
        action = chooser(row)
        counts[action] += 1
        if action == "pass":
            continue
        value = float(row["aligned_residual"])
        action_value = -value if action == "fade" else value
        acted.append({
            "aligned_residual": action_value,
            "hit": action_value > 0,
        })
    summary = cv._summary(acted, seed=seed) if acted else {"n": 0}
    return {
        "policy": name,
        "source_games": len(rows),
        "keep_n": counts["keep"],
        "fade_n": counts["fade"],
        "pass_n": counts["pass"],
        "acted_n": len(acted),
        **summary,
    }


def _by_year(rows: list[dict[str, Any]],
             fn: Callable[[list[dict[str, Any]], int], dict[str, Any]]
             ) -> list[dict[str, Any]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[int(row["season"])].append(row)
    return [
        {"season": season, **fn(grouped[season], season)}
        for season in sorted(grouped)
    ]


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    rows = _full_rows(repository, test_season=int(test_season))

    spread_14, spread_lt14 = _split(
        rows, lambda r: r.get("spread_bucket") == "14+")
    narrative_missing, narrative_present = _split(
        rows, lambda r: r.get("narrative_state") == "missing")
    weak_confirmation, material_confirmation = _split(
        rows, lambda r: r.get("joint_confirmation_strength") == "weak_any_<0.25")

    condition_tests = {
        "spread_14_plus": {
            "n": len(spread_14),
            "keep": _action_summary(spread_14, action="keep", seed=910001),
            "fade": _action_summary(spread_14, action="fade", seed=910002),
            "by_year_keep": _by_year(
                spread_14,
                lambda subset, season: _action_summary(
                    subset, action="keep", seed=season * 100 + 1),
            ),
            "by_year_fade": _by_year(
                spread_14,
                lambda subset, season: _action_summary(
                    subset, action="fade", seed=season * 100 + 2),
            ),
        },
        "narrative_missing": {
            "n": len(narrative_missing),
            "keep": _action_summary(
                narrative_missing, action="keep", seed=920001),
            "fade": _action_summary(
                narrative_missing, action="fade", seed=920002),
            "by_year_keep": _by_year(
                narrative_missing,
                lambda subset, season: _action_summary(
                    subset, action="keep", seed=season * 100 + 3),
            ),
            "by_year_fade": _by_year(
                narrative_missing,
                lambda subset, season: _action_summary(
                    subset, action="fade", seed=season * 100 + 4),
            ),
        },
        "weak_confirmation": {
            "n": len(weak_confirmation),
            "keep": _action_summary(
                weak_confirmation, action="keep", seed=930001),
            "fade": _action_summary(
                weak_confirmation, action="fade", seed=930002),
            "by_year_keep": _by_year(
                weak_confirmation,
                lambda subset, season: _action_summary(
                    subset, action="keep", seed=season * 100 + 5),
            ),
            "by_year_fade": _by_year(
                weak_confirmation,
                lambda subset, season: _action_summary(
                    subset, action="fade", seed=season * 100 + 6),
            ),
        },
    }

    policies = [
        _policy_summary(
            rows,
            name="baseline_keep_all_full_convergence",
            chooser=lambda r: "keep",
            seed=940001,
        ),
        _policy_summary(
            rows,
            name="pass_narrative_missing",
            chooser=lambda r: (
                "pass" if r.get("narrative_state") == "missing" else "keep"),
            seed=940002,
        ),
        _policy_summary(
            rows,
            name="pass_spread_14_plus",
            chooser=lambda r: (
                "pass" if r.get("spread_bucket") == "14+" else "keep"),
            seed=940003,
        ),
        _policy_summary(
            rows,
            name="fade_spread_14_plus",
            chooser=lambda r: (
                "fade" if r.get("spread_bucket") == "14+" else "keep"),
            seed=940004,
        ),
        _policy_summary(
            rows,
            name="pass_missing_and_fade_14_plus",
            chooser=lambda r: (
                "pass" if r.get("narrative_state") == "missing"
                else "fade" if r.get("spread_bucket") == "14+"
                else "keep"
            ),
            seed=940005,
        ),
        _policy_summary(
            rows,
            name="pass_weak_confirmation",
            chooser=lambda r: (
                "pass"
                if r.get("joint_confirmation_strength") == "weak_any_<0.25"
                else "keep"
            ),
            seed=940006,
        ),
        _policy_summary(
            rows,
            name="pass_missing_and_weak_fade_14_plus",
            chooser=lambda r: (
                "pass"
                if (
                    r.get("narrative_state") == "missing"
                    or r.get("joint_confirmation_strength") == "weak_any_<0.25"
                )
                else "fade" if r.get("spread_bucket") == "14+"
                else "keep"
            ),
            seed=940007,
        ),
    ]

    policy_by_year = []
    for policy in policies:
        name = policy["policy"]
        if name == "baseline_keep_all_full_convergence":
            chooser = lambda r: "keep"
        elif name == "pass_narrative_missing":
            chooser = lambda r: (
                "pass" if r.get("narrative_state") == "missing" else "keep")
        elif name == "pass_spread_14_plus":
            chooser = lambda r: (
                "pass" if r.get("spread_bucket") == "14+" else "keep")
        elif name == "fade_spread_14_plus":
            chooser = lambda r: (
                "fade" if r.get("spread_bucket") == "14+" else "keep")
        elif name == "pass_missing_and_fade_14_plus":
            chooser = lambda r: (
                "pass" if r.get("narrative_state") == "missing"
                else "fade" if r.get("spread_bucket") == "14+" else "keep")
        elif name == "pass_weak_confirmation":
            chooser = lambda r: (
                "pass"
                if r.get("joint_confirmation_strength") == "weak_any_<0.25"
                else "keep")
        else:
            chooser = lambda r: (
                "pass"
                if (
                    r.get("narrative_state") == "missing"
                    or r.get("joint_confirmation_strength") == "weak_any_<0.25"
                )
                else "fade" if r.get("spread_bucket") == "14+"
                else "keep"
            )
        policy_by_year.append({
            "policy": name,
            "years": _by_year(
                rows,
                lambda subset, season, chooser=chooser, name=name:
                    _policy_summary(
                        subset, name=name, chooser=chooser,
                        seed=season * 1000 + len(name),
                    ),
            ),
        })

    strength_groups = defaultdict(list)
    matrix_groups = defaultdict(list)
    for row in rows:
        strength_groups[row["joint_confirmation_strength"]].append(row)
        matrix_groups[
            (row["structural_strength"], row["line_elo_strength"])
        ].append(row)

    strength_validation = []
    for index, key in enumerate(sorted(strength_groups)):
        subset = strength_groups[key]
        strength_validation.append({
            "joint_confirmation_strength": key,
            **_action_summary(subset, action="keep", seed=950000 + index),
            "by_year": _by_year(
                subset,
                lambda year_rows, season: _action_summary(
                    year_rows, action="keep", seed=season * 100 + 20 + index),
            ),
        })

    strength_matrix = []
    for index, ((structural, line), subset) in enumerate(
            sorted(matrix_groups.items())):
        strength_matrix.append({
            "structural_strength": structural,
            "line_elo_strength": line,
            **_action_summary(subset, action="keep", seed=960000 + index),
        })

    return {
        "version": "convergence-action-policy-v1",
        "test_through_season": int(test_season),
        "frozen_definition": {
            "minimum_abs_margin_power_z": FROZEN_THRESHOLD,
            "required_confirmations": 2,
            "note": "No Full Convergence definition was changed.",
        },
        "confirmation_strength_cuts": list(CONFIRMATION_STRENGTH_CUTS),
        "full_convergence_n": len(rows),
        "condition_tests": condition_tests,
        "confirmation_strength_validation": strength_validation,
        "confirmation_strength_matrix": strength_matrix,
        "hypothetical_policies": policies,
        "hypothetical_policies_by_year": policy_by_year,
        "game_rows": rows,
        "notes": [
            "Fade means evaluating the exact opposite side on the same closing spread residual.",
            "Pass means no action and is not counted as a win or loss.",
            "All policy results are retrospective hypothesis tests, not production recommendations.",
            "Strength thresholds 0.25 and 0.50 z are fixed descriptive bands, not fitted cutoffs.",
            "Year-by-year output is required before interpreting a pooled fade result as stable.",
        ],
    }


def export_report(payload: dict[str, Any], output_dir: str) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "convergence_action_policy.json")
    policy_path = os.path.join(output_dir, "convergence_action_policy_summary.csv")
    games_path = os.path.join(output_dir, "convergence_action_policy_games.csv")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    rows = []
    for policy in payload["hypothetical_policies"]:
        rows.append({"scope": "pooled", "season": "", **policy})
    for group in payload["hypothetical_policies_by_year"]:
        for year in group["years"]:
            rows.append({
                "scope": "year",
                "season": year["season"],
                **{k: v for k, v in year.items() if k != "season"},
            })
    fields = [
        "scope", "season", "policy", "source_games", "keep_n", "fade_n",
        "pass_n", "acted_n", "n", "wins", "hit_rate", "hit_rate_ci95",
        "mean_aligned_residual", "mean_aligned_residual_bootstrap_ci95",
        "median_aligned_residual", "worst_aligned_miss", "best_aligned_result",
    ]
    with open(policy_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            cooked = dict(row)
            for field in (
                "hit_rate_ci95",
                "mean_aligned_residual_bootstrap_ci95",
            ):
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

    return {
        "json": json_path,
        "policy_csv": policy_path,
        "games_csv": games_path,
    }


def compact_console_summary(payload: dict[str, Any],
                            paths: dict[str, str]) -> dict[str, Any]:
    spread = payload["condition_tests"]["spread_14_plus"]
    missing = payload["condition_tests"]["narrative_missing"]
    return {
        "version": payload["version"],
        "full_convergence_n": payload["full_convergence_n"],
        "spread_14_plus": {
            "n": spread["n"],
            "keep_hit_rate": spread["keep"].get("hit_rate"),
            "fade_hit_rate": spread["fade"].get("hit_rate"),
            "fade_mean_residual": spread["fade"].get("mean_aligned_residual"),
        },
        "narrative_missing": {
            "n": missing["n"],
            "keep_hit_rate": missing["keep"].get("hit_rate"),
            "fade_hit_rate": missing["fade"].get("hit_rate"),
        },
        "files": paths,
    }
