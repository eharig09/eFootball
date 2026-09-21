"""Frozen positive/fade routing hypotheses with a 2026 holdout.

The routing states in this module are frozen from the 2020-2025 discovery
analysis. They must not be changed after inspecting holdout results. The
original Margin Power-selected side is retained for positive routes and
reversed for fade routes.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from sports_aggregator.cfb import convergence_four_signal as four
from sports_aggregator.cfb import convergence_validation as cv
from sports_aggregator.cfb.repository import CFBRepository

DISCOVERY_END_SEASON = 2025
DEFAULT_HOLDOUT_SEASON = 2026


def _is_old_2_of_3_elo_agrees(row: dict[str, Any]) -> bool:
    return (
        int(row["old_agreement_count"]) == 2
        and bool(row["hc_qb_elo_confirms"])
    )


def _is_old_3_of_3_elo_disagrees(row: dict[str, Any]) -> bool:
    return (
        int(row["old_agreement_count"]) == 3
        and not bool(row["hc_qb_elo_confirms"])
    )


ROUTES: tuple[dict[str, Any], ...] = (
    {
        "name": "positive_4_of_4_spread_lt_14",
        "action": "follow",
        "description": "Follow Margin Power when all four signals agree and spread is below 14.",
        "predicate": lambda r: int(r["agreement_count"]) == 4 and r.get("spread_bucket") != "14+",
    },
    {
        "name": "positive_old_2_of_3_elo_agrees_spread_3_to_6_5",
        "action": "follow",
        "description": "Follow promoted old 2/3 + HC/QB Elo agreement in the 3-6.5 spread bucket.",
        "predicate": lambda r: _is_old_2_of_3_elo_agrees(r) and r.get("spread_bucket") == "3-6.5",
    },
    {
        "name": "positive_old_3_of_3_elo_disagrees_spread_lt_3",
        "action": "follow",
        "description": "Follow old 3/3 despite HC/QB Elo disagreement when spread is below 3.",
        "predicate": lambda r: _is_old_3_of_3_elo_disagrees(r) and r.get("spread_bucket") == "<3",
    },
    {
        "name": "fade_old_2_of_3_elo_agrees_spread_lt_3",
        "action": "fade",
        "description": "Fade promoted old 2/3 + HC/QB Elo agreement when spread is below 3.",
        "predicate": lambda r: _is_old_2_of_3_elo_agrees(r) and r.get("spread_bucket") == "<3",
    },
    {
        "name": "fade_old_3_of_3_elo_disagrees_spread_14_plus",
        "action": "fade",
        "description": "Fade old 3/3 + HC/QB Elo disagreement when spread is 14+.",
        "predicate": lambda r: _is_old_3_of_3_elo_disagrees(r) and r.get("spread_bucket") == "14+",
    },
)


def _orient_row(row: dict[str, Any], *, action: str, route_name: str) -> dict[str, Any]:
    direction = -1.0 if action == "fade" else 1.0
    residual = float(row["aligned_residual"]) * direction
    cooked = dict(row)
    cooked["route_name"] = route_name
    cooked["route_action"] = action
    cooked["routed_aligned_residual"] = residual
    cooked["routed_hit"] = residual > 0
    cooked["routed_selected_side"] = (
        row["selected_side"] if action == "follow"
        else ("away" if row["selected_side"] == "home" else "home")
    )
    cooked["routed_selected_team"] = (
        row["selected_team"] if action == "follow"
        else (row["away_team"] if row["selected_side"] == "home" else row["home_team"])
    )
    return cooked


def _route_rows(rows: list[dict[str, Any]], route: dict[str, Any]) -> list[dict[str, Any]]:
    predicate: Callable[[dict[str, Any]], bool] = route["predicate"]
    return [
        _orient_row(r, action=str(route["action"]), route_name=str(route["name"]))
        for r in rows
        if predicate(r)
    ]


def _summary(rows: list[dict[str, Any]], *, seed: int) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    projected = [
        {
            "aligned_residual": float(r["routed_aligned_residual"]),
            "hit": bool(r["routed_hit"]),
        }
        for r in rows
    ]
    return cv._summary(projected, seed=seed)


def _baseline_summary(rows: list[dict[str, Any]], *, seed: int) -> dict[str, Any]:
    baseline = [r for r in rows if int(r["old_agreement_count"]) == 3]
    return four._summary(baseline, seed=seed)


def _simple_policy_summary(rows: list[dict[str, Any]], *, seed: int) -> dict[str, Any]:
    selected = [
        r for r in rows
        if int(r["agreement_count"]) >= 3 and r.get("spread_bucket") != "14+"
    ]
    return four._summary(selected, seed=seed)


def _route_report(rows: list[dict[str, Any]], *, seed_base: int) -> list[dict[str, Any]]:
    out = []
    for index, route in enumerate(ROUTES):
        routed = _route_rows(rows, route)
        out.append({
            "route": route["name"],
            "action": route["action"],
            "description": route["description"],
            **_summary(routed, seed=seed_base + index),
        })
    return out


def _combined_portfolio(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    seen: set[int] = set()
    for route in ROUTES:
        for row in _route_rows(rows, route):
            game_id = int(row["game_id"])
            if game_id in seen:
                raise ValueError(f"frozen routing rules overlap on game_id={game_id}")
            seen.add(game_id)
            combined.append(row)
    return combined


def _by_season(rows: list[dict[str, Any]], *, seed_base: int) -> dict[str, list[dict[str, Any]]]:
    seasons = sorted({int(r["season"]) for r in rows})
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for route_index, route in enumerate(ROUTES):
        routed = _route_rows(rows, route)
        for season in seasons:
            out[str(route["name"])].append({
                "season": season,
                **_summary(
                    [r for r in routed if int(r["season"]) == season],
                    seed=seed_base + route_index * 100 + season,
                ),
            })
    return dict(out)


def report(
    repository: CFBRepository,
    *,
    holdout_season: int = DEFAULT_HOLDOUT_SEASON,
    elo_start_season: int = 2015,
) -> dict[str, Any]:
    holdout_season = int(holdout_season)
    if holdout_season <= DISCOVERY_END_SEASON:
        raise ValueError(
            f"holdout_season must be after frozen discovery end {DISCOVERY_END_SEASON}"
        )

    all_rows, coverage = four._joined_rows(
        repository,
        test_season=holdout_season,
        elo_start_season=int(elo_start_season),
    )
    discovery = [r for r in all_rows if int(r["season"]) <= DISCOVERY_END_SEASON]
    holdout = [r for r in all_rows if int(r["season"]) == holdout_season]

    discovery_portfolio = _combined_portfolio(discovery)
    holdout_portfolio = _combined_portfolio(holdout)

    return {
        "version": "cfb-convergence-routing-holdout-v1",
        "frozen_before_holdout": True,
        "discovery_seasons": f"through_{DISCOVERY_END_SEASON}",
        "holdout_season": holdout_season,
        "coverage": {
            **coverage,
            "discovery_complete_rows": len(discovery),
            "holdout_complete_rows": len(holdout),
        },
        "frozen_routes": [
            {
                "route": r["name"],
                "action": r["action"],
                "description": r["description"],
            }
            for r in ROUTES
        ],
        "discovery_reference": {
            "routes": _route_report(discovery, seed_base=981000),
            "combined_portfolio": _summary(discovery_portfolio, seed=981100),
            "old_full_convergence_baseline": _baseline_summary(discovery, seed=981101),
            "simple_ge_3_of_4_exclude_14_plus": _simple_policy_summary(discovery, seed=981102),
            "routes_by_season": _by_season(discovery, seed_base=981500),
        },
        "holdout": {
            "routes": _route_report(holdout, seed_base=982000),
            "combined_portfolio": _summary(holdout_portfolio, seed=982100),
            "old_full_convergence_baseline": _baseline_summary(holdout, seed=982101),
            "simple_ge_3_of_4_exclude_14_plus": _simple_policy_summary(holdout, seed=982102),
        },
        "holdout_routed_game_rows": holdout_portfolio,
        "notes": [
            "The five route definitions are frozen from 2020-2025 discovery results.",
            "Fade routes reverse the Margin Power-selected side and multiply aligned residual by -1.",
            "A zero residual remains a non-win after routing, matching the existing ATS hit convention.",
            "Do not alter route thresholds after viewing the holdout result; any revision should create a new hypothesis and a later holdout.",
        ],
    }
