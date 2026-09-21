"""Two-engine CFB portfolio research.

Engine A is the existing frozen five-route convergence portfolio.
Engine B is frozen here from two previously researched Narrative-family states:
- negative-result/rebound vs positive-result/momentum, QB opposes family A
- negative-result/rebound vs post-success-risk, QB opposes family A

Engine B is deduplicated by game. A/B same-side overlap is counted once.
A/B opposite-side conflicts are reported separately and excluded from the
non-conflicting combined portfolio.

Important: 2025 informed the selection of Engine B, so it is retrospective
validation here, not a clean holdout. This module does not evaluate 2026.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from sports_aggregator.cfb import convergence_four_signal as four
from sports_aggregator.cfb import convergence_routing_holdout as routing
from sports_aggregator.cfb import family_policy_vs_convergence as fp
from sports_aggregator.cfb import narrative_family_rating_interactions as fam
from sports_aggregator.cfb.repository import CFBRepository

TEST_SEASON = 2025
ENGINE_B_POLICY_NAMES = (
    "rebound_vs_momentum_qb_opposes",
    "rebound_vs_post_success_qb_opposes",
)


def _summary(rows: list[dict[str, Any]], residual_key: str = "portfolio_residual") -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "wins": 0,
            "hit_rate": None,
            "mean_aligned_residual": None,
            "median_aligned_residual": None,
        }
    values = [float(row[residual_key]) for row in rows]
    return {
        "n": len(values),
        "wins": sum(value > 0 for value in values),
        "hit_rate": round(sum(value > 0 for value in values) / len(values), 4),
        "mean_aligned_residual": round(sum(values) / len(values), 3),
        "median_aligned_residual": round(float(median(values)), 3),
    }


def _engine_a_rows(repository: CFBRepository, *, end_season: int) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    rows, coverage = four._joined_rows(
        repository,
        test_season=int(end_season),
        elo_start_season=2015,
    )
    out: dict[int, dict[str, Any]] = {}
    for route in routing.ROUTES:
        for row in routing._route_rows(rows, route):
            gid = int(row["game_id"])
            if gid in out:
                raise ValueError(f"Frozen Engine A routes overlap on game_id={gid}")
            out[gid] = {
                "game_id": gid,
                "season": int(row["season"]),
                "selected_side": str(row["routed_selected_side"]),
                "selected_team": row.get("routed_selected_team"),
                "engine_a_route": str(row["route_name"]),
                "engine_a_action": str(row["route_action"]),
                "engine_a_residual": float(row["routed_aligned_residual"]),
            }
    return out, coverage


def _engine_b_rows(
    repository: CFBRepository,
    *,
    start_season: int,
    end_season: int,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    observations, family_diagnostics = fam._family_observations(
        repository,
        start_season=int(start_season),
        end_season=int(end_season),
    )
    rating_dirs = fp._rating_directions(repository, start_season, end_season)
    policies = {
        str(policy["name"]): policy
        for policy in fp.CANDIDATE_POLICIES
        if str(policy["name"]) in ENGINE_B_POLICY_NAMES
    }
    missing = set(ENGINE_B_POLICY_NAMES) - set(policies)
    if missing:
        raise ValueError(f"Missing Engine B policy definitions: {sorted(missing)}")

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for name in ENGINE_B_POLICY_NAMES:
        for row in fp._policy_rows(observations, rating_dirs, policies[name]):
            grouped[int(row["game_id"])].append(row)

    out: dict[int, dict[str, Any]] = {}
    internal_overlap = 0
    for gid, rows in grouped.items():
        sides = {str(row["policy_selected_side"]) for row in rows}
        if len(sides) != 1:
            raise ValueError(
                f"Engine B policies select conflicting sides for game_id={gid}: {sorted(sides)}"
            )
        if len(rows) > 1:
            internal_overlap += 1
        residuals = [float(row["policy_aligned_residual"]) for row in rows]
        # Same selected side should imply the same market-aligned residual.
        # Retain the first and diagnose any unexpected disagreement.
        if max(residuals) - min(residuals) > 1e-9:
            raise ValueError(
                f"Engine B residual mismatch on same-side game_id={gid}: {residuals}"
            )
        first = rows[0]
        out[gid] = {
            "game_id": gid,
            "season": int(first["season"]),
            "selected_side": str(first["policy_selected_side"]),
            "engine_b_policies": sorted({str(row["policy_name"]) for row in rows}),
            "engine_b_residual": residuals[0],
        }

    return out, {
        "family_diagnostics": family_diagnostics,
        "engine_b_distinct_games": len(out),
        "engine_b_internal_overlap_games": internal_overlap,
    }


def _partition(
    engine_a: dict[int, dict[str, Any]],
    engine_b: dict[int, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    a_ids = set(engine_a)
    b_ids = set(engine_b)
    a_only = []
    b_only = []
    agree = []
    conflict = []

    for gid in sorted(a_ids - b_ids):
        row = dict(engine_a[gid])
        row["portfolio_residual"] = float(row["engine_a_residual"])
        a_only.append(row)

    for gid in sorted(b_ids - a_ids):
        row = dict(engine_b[gid])
        row["portfolio_residual"] = float(row["engine_b_residual"])
        b_only.append(row)

    for gid in sorted(a_ids & b_ids):
        a = engine_a[gid]
        b = engine_b[gid]
        common = {
            "game_id": gid,
            "season": int(a["season"]),
            "engine_a_route": a["engine_a_route"],
            "engine_a_action": a["engine_a_action"],
            "engine_a_selected_side": a["selected_side"],
            "engine_a_residual": float(a["engine_a_residual"]),
            "engine_b_policies": b["engine_b_policies"],
            "engine_b_selected_side": b["selected_side"],
            "engine_b_residual": float(b["engine_b_residual"]),
        }
        if str(a["selected_side"]) == str(b["selected_side"]):
            common["selected_side"] = str(a["selected_side"])
            common["portfolio_residual"] = float(b["engine_b_residual"])
            agree.append(common)
        else:
            conflict.append(common)

    return {
        "engine_a_only": a_only,
        "engine_b_only": b_only,
        "agreement": agree,
        "conflict": conflict,
    }


def _period(rows: list[dict[str, Any]], *, test_season: int, heldout: bool) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if (
            int(row["season"]) == int(test_season)
            if heldout
            else int(row["season"]) < int(test_season)
        )
    ]


def _conflict_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "engine_a_side": {"n": 0},
            "engine_b_side": {"n": 0},
        }
    a_rows = [{"x": float(row["engine_a_residual"])} for row in rows]
    b_rows = [{"x": float(row["engine_b_residual"])} for row in rows]
    return {
        "n": len(rows),
        "engine_a_side": _summary(a_rows, residual_key="x"),
        "engine_b_side": _summary(b_rows, residual_key="x"),
    }


def _portfolio_rows(parts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return (
        list(parts["engine_a_only"])
        + list(parts["engine_b_only"])
        + list(parts["agreement"])
    )


def _category_report(parts: dict[str, list[dict[str, Any]]], *, test_season: int) -> dict[str, Any]:
    portfolio = _portfolio_rows(parts)
    report: dict[str, Any] = {}
    for key in ("engine_a_only", "engine_b_only", "agreement"):
        rows = parts[key]
        report[key] = {
            "all_2020_2025": _summary(rows),
            "pre_2025": _summary(_period(rows, test_season=test_season, heldout=False)),
            "retrospective_2025": _summary(
                _period(rows, test_season=test_season, heldout=True)
            ),
        }
    conflicts = parts["conflict"]
    report["conflict"] = {
        "all_2020_2025": _conflict_summary(conflicts),
        "pre_2025": _conflict_summary(
            _period(conflicts, test_season=test_season, heldout=False)
        ),
        "retrospective_2025": _conflict_summary(
            _period(conflicts, test_season=test_season, heldout=True)
        ),
    }
    report["non_conflicting_combined_portfolio"] = {
        "all_2020_2025": _summary(portfolio),
        "pre_2025": _summary(
            _period(portfolio, test_season=test_season, heldout=False)
        ),
        "retrospective_2025": _summary(
            _period(portfolio, test_season=test_season, heldout=True)
        ),
    }
    return report


def _by_season(parts: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    portfolio = _portfolio_rows(parts)
    seasons = sorted({
        int(row["season"])
        for rows in parts.values()
        for row in rows
    })
    out: dict[str, Any] = {}
    for season in seasons:
        out[str(season)] = {
            "engine_a_only": _summary([
                r for r in parts["engine_a_only"] if int(r["season"]) == season
            ]),
            "engine_b_only": _summary([
                r for r in parts["engine_b_only"] if int(r["season"]) == season
            ]),
            "agreement": _summary([
                r for r in parts["agreement"] if int(r["season"]) == season
            ]),
            "conflict": _conflict_summary([
                r for r in parts["conflict"] if int(r["season"]) == season
            ]),
            "non_conflicting_combined_portfolio": _summary([
                r for r in portfolio if int(r["season"]) == season
            ]),
        }
    return out


def report(
    repository: CFBRepository,
    *,
    start_season: int = 2020,
    end_season: int = 2025,
    test_season: int = TEST_SEASON,
) -> dict[str, Any]:
    if int(end_season) > 2025:
        raise ValueError(
            "This retrospective study is intentionally capped at 2025. "
            "Do not evaluate the newly frozen Engine B on 2026 in this module."
        )

    engine_a, convergence_coverage = _engine_a_rows(
        repository, end_season=end_season
    )
    engine_b, engine_b_diagnostics = _engine_b_rows(
        repository,
        start_season=start_season,
        end_season=end_season,
    )
    parts = _partition(engine_a, engine_b)
    portfolio = _portfolio_rows(parts)

    return {
        "version": "cfb-two-engine-portfolio-v1",
        "start_season": int(start_season),
        "end_season": int(end_season),
        "retrospective_validation_season": int(test_season),
        "engine_definitions": {
            "engine_a": {
                "name": "frozen_convergence_routes",
                "routes": [
                    {
                        "name": route["name"],
                        "action": route["action"],
                        "description": route["description"],
                    }
                    for route in routing.ROUTES
                ],
                "status": "previously frozen",
            },
            "engine_b": {
                "name": "narrative_family_qb_overreaction",
                "policies": list(ENGINE_B_POLICY_NAMES),
                "status": "frozen now for future evaluation",
                "deduplication": "one selection per game; same-side policy overlaps collapse",
            },
        },
        "coverage": {
            "engine_a_games": len(engine_a),
            "engine_b_distinct_games": len(engine_b),
            "engine_b_internal_overlap_games": engine_b_diagnostics[
                "engine_b_internal_overlap_games"
            ],
            "engine_a_only_games": len(parts["engine_a_only"]),
            "engine_b_only_games": len(parts["engine_b_only"]),
            "agreement_games": len(parts["agreement"]),
            "conflict_games": len(parts["conflict"]),
            "non_conflicting_combined_games": len(portfolio),
            "combined_increment_vs_engine_a": len(portfolio) - len(engine_a),
            "combined_coverage_multiplier_vs_engine_a": (
                round(len(portfolio) / len(engine_a), 4) if engine_a else None
            ),
        },
        "performance": _category_report(parts, test_season=int(test_season)),
        "by_season": _by_season(parts),
        "conflict_game_rows": parts["conflict"],
        "agreement_game_rows": parts["agreement"],
        "diagnostics": {
            "convergence_coverage": convergence_coverage,
            **engine_b_diagnostics,
        },
        "notes": [
            "Engine A is imported unchanged from the five frozen convergence routes.",
            "Engine B contains only the two broader QB-opposition Narrative-family policies.",
            "Engine B is deduplicated by game before comparison with Engine A.",
            "Same-side A/B overlaps count once in the combined portfolio.",
            "Opposite-side A/B conflicts are excluded from the non-conflicting combined portfolio and reported separately.",
            "2025 is retrospective validation, not a clean holdout, because its results informed Engine B selection.",
            "This module is capped at 2025. A future 2026 evaluation must use a separately clean holdout path.",
            "The existing four-signal Engine A HC/QB normalization should be made walk-forward before interpreting a 2026 holdout.",
        ],
    }
