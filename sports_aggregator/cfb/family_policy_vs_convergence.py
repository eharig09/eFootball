"""Compare predeclared Narrative-family policies with frozen convergence routes.

Research-only. Candidate rules are fixed from prior family-analysis findings and
are evaluated for performance, incremental coverage, and overlap with the
already-frozen convergence routing portfolio.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any, Callable

from sports_aggregator.cfb import convergence_four_signal as four
from sports_aggregator.cfb import convergence_routing_holdout as routing
from sports_aggregator.cfb import narrative_family_rating_interactions as fam
from sports_aggregator.cfb.rating_predictive_power import _load_dataset
from sports_aggregator.cfb.repository import CFBRepository

TEST_SEASON = 2025

# All definitions are declared before this module evaluates their outcomes.
CANDIDATE_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "name": "rebound_vs_momentum_qb_opposes",
        "family_pair": ("negative_result_rebound", "positive_result_momentum"),
        "rating_mode": "qb",
        "relation": -1,
        "action": "follow_family_a",
        "description": (
            "Follow negative-result/rebound side versus positive-result/momentum "
            "when QB Elo points to the opponent."
        ),
    },
    {
        "name": "rebound_vs_momentum_both_oppose",
        "family_pair": ("negative_result_rebound", "positive_result_momentum"),
        "rating_mode": "both",
        "relation": -1,
        "action": "follow_family_a",
        "description": (
            "Follow negative-result/rebound side when HC and QB agree on the "
            "positive-result/momentum opponent."
        ),
    },
    {
        "name": "rebound_vs_post_success_qb_opposes",
        "family_pair": ("negative_result_rebound", "post_success_risk"),
        "rating_mode": "qb",
        "relation": -1,
        "action": "follow_family_a",
        "description": (
            "Follow negative-result/rebound side versus post-success-risk side "
            "when QB Elo points to the opponent."
        ),
    },
    {
        "name": "rebound_vs_post_success_both_oppose",
        "family_pair": ("negative_result_rebound", "post_success_risk"),
        "rating_mode": "both",
        "relation": -1,
        "action": "follow_family_a",
        "description": (
            "Follow negative-result/rebound side when HC and QB agree on the "
            "post-success-risk opponent."
        ),
    },
    {
        "name": "market_premium_vs_momentum_qb_supports",
        "family_pair": ("market_premium", "positive_result_momentum"),
        "rating_mode": "qb",
        "relation": 1,
        "action": "follow_family_a",
        "description": (
            "Follow market-premium side versus positive-result/momentum when "
            "QB Elo supports the market-premium side."
        ),
    },
    {
        "name": "market_discount_vs_schedule_pressure_qb_supports",
        "family_pair": ("market_discount", "schedule_pressure"),
        "rating_mode": "qb",
        "relation": 1,
        "action": "follow_family_a",
        "description": (
            "Exploratory comparator: follow market-discount side versus schedule "
            "pressure when QB Elo supports the market-discount side."
        ),
    },
)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "wins": 0, "hit_rate": None,
                "mean_aligned_residual": None, "median_aligned_residual": None}
    vals = [float(r["policy_aligned_residual"]) for r in rows]
    return {
        "n": len(vals),
        "wins": sum(v > 0 for v in vals),
        "hit_rate": round(sum(v > 0 for v in vals) / len(vals), 4),
        "mean_aligned_residual": round(sum(vals) / len(vals), 3),
        "median_aligned_residual": round(float(median(vals)), 3),
    }


def _rating_directions(repository: CFBRepository, start_season: int, end_season: int):
    out = {}
    for row in _load_dataset(
        repository, start_season=int(start_season), end_season=int(end_season)
    ):
        hc = float(row["hc_diff"])
        qb = row.get("qb_diff")
        hc_dir = 1 if hc > 0 else -1 if hc < 0 else 0
        qb_dir = (
            1 if qb is not None and float(qb) > 0
            else -1 if qb is not None and float(qb) < 0
            else 0
        )
        both_dir = hc_dir if hc_dir and hc_dir == qb_dir else 0
        out[int(row["game_id"])] = {
            "hc": hc_dir,
            "qb": qb_dir,
            "both": both_dir,
        }
    return out


def _policy_rows(
    observations: list[dict[str, Any]],
    rating_dirs: dict[int, dict[str, int]],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    a, b = policy["family_pair"]
    mode = str(policy["rating_mode"])
    required_relation = int(policy["relation"])
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in observations:
        if (item["family_a"], item["family_b"]) != (a, b):
            continue
        gid = int(item["game_id"])
        directions = rating_dirs.get(gid)
        if not directions:
            continue
        rating_direction = int(directions.get(mode) or 0)
        relation = rating_direction * int(item["home_orientation_sign"])
        if relation != required_relation:
            continue
        if gid in seen:
            continue
        seen.add(gid)
        # residual is already oriented to family_a.
        cooked = dict(item)
        cooked["policy_name"] = policy["name"]
        cooked["policy_selected_side"] = (
            "home" if int(item["home_orientation_sign"]) == 1 else "away"
        )
        cooked["policy_aligned_residual"] = float(item["residual"])
        cooked["policy_hit"] = float(item["residual"]) > 0
        cooked["rating_mode"] = mode
        cooked["rating_relation_to_family_a"] = relation
        selected.append(cooked)
    return selected


def _frozen_route_map(repository: CFBRepository, end_season: int) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    rows, coverage = four._joined_rows(
        repository, test_season=int(end_season), elo_start_season=2015
    )
    frozen: dict[int, dict[str, Any]] = {}
    for route in routing.ROUTES:
        for row in routing._route_rows(rows, route):
            gid = int(row["game_id"])
            if gid in frozen:
                raise ValueError(f"Frozen route overlap on game_id={gid}")
            frozen[gid] = {
                "route_name": row["route_name"],
                "route_action": row["route_action"],
                "route_selected_side": row["routed_selected_side"],
                "route_aligned_residual": float(row["routed_aligned_residual"]),
            }
    return frozen, coverage


def _overlap(rows: list[dict[str, Any]], frozen: dict[int, dict[str, Any]]) -> dict[str, Any]:
    overlap_rows = [r for r in rows if int(r["game_id"]) in frozen]
    unique_rows = [r for r in rows if int(r["game_id"]) not in frozen]
    same_side = 0
    opposite_side = 0
    by_route: dict[str, int] = defaultdict(int)
    for row in overlap_rows:
        route = frozen[int(row["game_id"])]
        by_route[str(route["route_name"])] += 1
        if row["policy_selected_side"] == route["route_selected_side"]:
            same_side += 1
        else:
            opposite_side += 1
    return {
        "overlap_n": len(overlap_rows),
        "overlap_rate": round(len(overlap_rows) / len(rows), 4) if rows else None,
        "unique_added_n": len(unique_rows),
        "same_selected_side_n": same_side,
        "opposite_selected_side_n": opposite_side,
        "overlap_by_frozen_route": dict(sorted(by_route.items())),
        "unique_games_summary": _summary(unique_rows),
        "overlap_games_policy_side_summary": _summary(overlap_rows),
    }


def _by_season(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        str(season): _summary([r for r in rows if int(r["season"]) == season])
        for season in sorted({int(r["season"]) for r in rows})
    }


def report(
    repository: CFBRepository,
    *,
    start_season: int = 2020,
    end_season: int = 2025,
    test_season: int = TEST_SEASON,
) -> dict[str, Any]:
    observations, family_diagnostics = fam._family_observations(
        repository, start_season=int(start_season), end_season=int(end_season)
    )
    rating_dirs = _rating_directions(repository, start_season, end_season)
    frozen, convergence_coverage = _frozen_route_map(repository, end_season)

    policies = []
    all_candidate_games: dict[int, list[str]] = defaultdict(list)
    for policy in CANDIDATE_POLICIES:
        rows = _policy_rows(observations, rating_dirs, policy)
        pretest = [r for r in rows if int(r["season"]) < int(test_season)]
        holdout = [r for r in rows if int(r["season"]) == int(test_season)]
        for row in rows:
            all_candidate_games[int(row["game_id"])].append(str(policy["name"]))
        policies.append({
            "policy": policy["name"],
            "description": policy["description"],
            "family_pair": list(policy["family_pair"]),
            "rating_mode": policy["rating_mode"],
            "required_relation_to_family_a": policy["relation"],
            "pretest_through_2024": _summary(pretest),
            "heldout_2025": _summary(holdout),
            "all_2020_2025": _summary(rows),
            "by_season": _by_season(rows),
            "frozen_route_overlap_all_years": _overlap(rows, frozen),
            "frozen_route_overlap_pretest": _overlap(pretest, frozen),
            "frozen_route_overlap_heldout_2025": _overlap(holdout, frozen),
        })

    multi_policy_games = {
        str(gid): names
        for gid, names in all_candidate_games.items()
        if len(names) > 1
    }
    candidate_game_ids = set(all_candidate_games)
    frozen_ids = set(frozen)
    unique_candidate_ids = candidate_game_ids - frozen_ids

    return {
        "version": "cfb-family-policy-vs-convergence-v1",
        "start_season": int(start_season),
        "end_season": int(end_season),
        "test_season": int(test_season),
        "candidate_policies": [
            {
                "name": p["name"],
                "description": p["description"],
                "family_pair": list(p["family_pair"]),
                "rating_mode": p["rating_mode"],
                "relation": p["relation"],
            }
            for p in CANDIDATE_POLICIES
        ],
        "policy_results": policies,
        "portfolio_coverage": {
            "distinct_candidate_games": len(candidate_game_ids),
            "distinct_frozen_route_games": len(frozen_ids),
            "candidate_games_overlapping_frozen_routes": len(candidate_game_ids & frozen_ids),
            "candidate_games_unique_vs_frozen_routes": len(unique_candidate_ids),
            "multi_candidate_policy_games": len(multi_policy_games),
            "multi_candidate_policy_game_ids": multi_policy_games,
        },
        "family_diagnostics": family_diagnostics,
        "convergence_coverage": convergence_coverage,
        "notes": [
            "Research-only. Existing five frozen convergence routes are imported unchanged.",
            "Candidate family policies were declared from prior analyses before this comparison.",
            "Family policies select family_a's side; the rating relation determines eligibility, not side selection.",
            "Unique-added counts measure games absent from the frozen convergence routing portfolio.",
            "Overlap same/opposite side counts reveal whether a family rule confirms or conflicts with an existing routed selection.",
            "2025 remains separated from pre-2025 rows; do not use these results to alter the frozen 2026 routing rules.",
            "The market-discount/schedule-pressure policy is retained as an exploratory comparator because its pretest support was weaker than its 2025 result.",
        ],
    }
