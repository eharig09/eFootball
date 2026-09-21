"""Audit legacy global HC/QB normalization versus corrected walk-forward normalization.

This report is intentionally capped at 2025. It quantifies whether fixing the
normalization changes four-signal votes or membership in the already-researched
five convergence routes before any 2026 outcome is inspected.
"""
from __future__ import annotations

from typing import Any

from sports_aggregator.cfb import convergence_four_signal as four
from sports_aggregator.cfb import convergence_robustness as cr
from sports_aggregator.cfb import convergence_routing_holdout as routing
from sports_aggregator.cfb.rating_predictive_power import _load_dataset, _scored_rows
from sports_aggregator.cfb.repository import CFBRepository

MAX_SEASON = 2025


def _legacy_joined_rows(
    repository: CFBRepository,
    *,
    test_season: int,
    elo_start_season: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    classified = cr._classified_with_context(repository, test_season=int(test_season))
    margin_gate = [
        row for row in classified
        if float(row["abs_margin_power_z"]) >= four.FROZEN_MARGIN_THRESHOLD
    ]
    elo_source = _load_dataset(
        repository,
        start_season=int(elo_start_season),
        end_season=int(test_season),
    )
    scores = {
        int(row["game_id"]): float(row["combined_z_avg"])
        for row in _scored_rows(elo_source)
        if row.get("combined_z_avg") is not None
    }

    complete = []
    missing = 0
    neutral = 0
    for row in margin_gate:
        z = scores.get(int(row["game_id"]))
        if z is None:
            missing += 1
            continue
        if abs(z) <= 1e-12:
            neutral += 1
            continue
        cooked = dict(row)
        direction = 1 if row["selected_side"] == "home" else -1
        cooked["hc_qb_elo_z"] = z
        cooked["hc_qb_elo_aligned"] = z * direction
        cooked["hc_qb_elo_confirms"] = cooked["hc_qb_elo_aligned"] > 0
        cooked["old_agreement_count"] = four._old_agreement_count(cooked)
        cooked["agreement_count"] = four._agreement_count(cooked)
        cooked["old_agreement_label"] = f'{cooked["old_agreement_count"]}/3'
        cooked["agreement_label"] = f'{cooked["agreement_count"]}/4'
        complete.append(cooked)

    return complete, {
        "classified": len(classified),
        "margin_gate": len(margin_gate),
        "complete_rows": len(complete),
        "missing_hc_qb": missing,
        "neutral_hc_qb": neutral,
        "normalization": "global_over_all_loaded_seasons_legacy",
    }


def _route_membership(rows: list[dict[str, Any]]) -> dict[str, dict[int, dict[str, Any]]]:
    output: dict[str, dict[int, dict[str, Any]]] = {}
    for route in routing.ROUTES:
        name = str(route["name"])
        output[name] = {
            int(row["game_id"]): row
            for row in routing._route_rows(rows, route)
        }
    return output


def _route_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "wins": 0, "hit_rate": None, "mean_aligned_residual": None}
    vals = [float(row["routed_aligned_residual"]) for row in rows]
    return {
        "n": len(vals),
        "wins": sum(value > 0 for value in vals),
        "hit_rate": round(sum(value > 0 for value in vals) / len(vals), 4),
        "mean_aligned_residual": round(sum(vals) / len(vals), 3),
    }


def _route_comparison(
    legacy_rows: list[dict[str, Any]],
    corrected_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    legacy = _route_membership(legacy_rows)
    corrected = _route_membership(corrected_rows)
    output = []
    for route in routing.ROUTES:
        name = str(route["name"])
        old = legacy[name]
        new = corrected[name]
        retained_ids = set(old) & set(new)
        dropped_ids = set(old) - set(new)
        added_ids = set(new) - set(old)
        output.append({
            "route": name,
            "legacy": _route_summary(list(old.values())),
            "corrected": _route_summary(list(new.values())),
            "retained_n": len(retained_ids),
            "dropped_n": len(dropped_ids),
            "added_n": len(added_ids),
            "membership_jaccard": (
                round(len(retained_ids) / len(set(old) | set(new)), 4)
                if set(old) | set(new) else None
            ),
            "dropped_game_ids": sorted(dropped_ids),
            "added_game_ids": sorted(added_ids),
        })
    return output


def _vote_changes(
    legacy_rows: list[dict[str, Any]],
    corrected_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    legacy = {int(row["game_id"]): row for row in legacy_rows}
    corrected = {int(row["game_id"]): row for row in corrected_rows}
    common = set(legacy) & set(corrected)
    sign_changed = []
    agreement_changed = []
    for gid in sorted(common):
        old = legacy[gid]
        new = corrected[gid]
        if bool(old["hc_qb_elo_confirms"]) != bool(new["hc_qb_elo_confirms"]):
            sign_changed.append({
                "game_id": gid,
                "season": int(new["season"]),
                "legacy_hc_qb_elo_z": round(float(old["hc_qb_elo_z"]), 6),
                "corrected_hc_qb_elo_z": round(float(new["hc_qb_elo_z"]), 6),
                "selected_side": new["selected_side"],
                "legacy_confirms": bool(old["hc_qb_elo_confirms"]),
                "corrected_confirms": bool(new["hc_qb_elo_confirms"]),
            })
        if int(old["agreement_count"]) != int(new["agreement_count"]):
            agreement_changed.append({
                "game_id": gid,
                "season": int(new["season"]),
                "legacy_agreement": int(old["agreement_count"]),
                "corrected_agreement": int(new["agreement_count"]),
            })
    return {
        "common_complete_rows": len(common),
        "hc_qb_confirmation_sign_changes": len(sign_changed),
        "agreement_count_changes": len(agreement_changed),
        "sign_change_rate": round(len(sign_changed) / len(common), 4) if common else None,
        "sign_change_rows": sign_changed,
        "agreement_change_rows": agreement_changed,
    }


def _exact_ladder(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return four.exact_ladder_report(rows)


def report(
    repository: CFBRepository,
    *,
    end_season: int = 2025,
    elo_start_season: int = 2015,
) -> dict[str, Any]:
    if int(end_season) > MAX_SEASON:
        raise ValueError(
            "Normalization audit is capped at 2025; do not inspect 2026 outcomes here."
        )

    legacy_rows, legacy_coverage = _legacy_joined_rows(
        repository,
        test_season=int(end_season),
        elo_start_season=int(elo_start_season),
    )
    corrected_rows, corrected_coverage = four._joined_rows(
        repository,
        test_season=int(end_season),
        elo_start_season=int(elo_start_season),
    )

    routes = _route_comparison(legacy_rows, corrected_rows)
    legacy_route_ids = {
        int(row["game_id"])
        for route in routing.ROUTES
        for row in routing._route_rows(legacy_rows, route)
    }
    corrected_route_ids = {
        int(row["game_id"])
        for route in routing.ROUTES
        for row in routing._route_rows(corrected_rows, route)
    }

    return {
        "version": "cfb-hc-qb-normalization-audit-v1",
        "end_season": int(end_season),
        "elo_start_season": int(elo_start_season),
        "legacy_coverage": legacy_coverage,
        "corrected_coverage": corrected_coverage,
        "vote_changes": _vote_changes(legacy_rows, corrected_rows),
        "four_signal_exact_ladder": {
            "legacy_global_normalization": _exact_ladder(legacy_rows),
            "corrected_walk_forward_normalization": _exact_ladder(corrected_rows),
        },
        "frozen_route_membership_comparison": routes,
        "combined_frozen_route_membership": {
            "legacy_games": len(legacy_route_ids),
            "corrected_games": len(corrected_route_ids),
            "retained_games": len(legacy_route_ids & corrected_route_ids),
            "dropped_games": len(legacy_route_ids - corrected_route_ids),
            "added_games": len(corrected_route_ids - legacy_route_ids),
            "jaccard": (
                round(
                    len(legacy_route_ids & corrected_route_ids)
                    / len(legacy_route_ids | corrected_route_ids),
                    4,
                )
                if legacy_route_ids | corrected_route_ids else None
            ),
            "dropped_game_ids": sorted(legacy_route_ids - corrected_route_ids),
            "added_game_ids": sorted(corrected_route_ids - legacy_route_ids),
        },
        "notes": [
            "Legacy reproduces the prior global z-score normalization over all loaded seasons.",
            "Corrected normalization fits HC and QB mean/std using seasons strictly before each target season.",
            "Pregame HC/QB Elo snapshots themselves were already chronological; this audit changes only their combination scale.",
            "The report is capped at 2025 so no 2026 outcome is consumed.",
            "Review membership changes before deciding whether the historical route hypothesis needs a v2 freeze.",
        ],
    }
