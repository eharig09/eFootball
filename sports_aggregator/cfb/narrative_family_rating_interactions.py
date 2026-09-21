"""Collapse overlapping Narrative tags into semantic families and test HC/QB modifiers.

Family definitions are fixed in code before looking at the held-out 2025 results.
Multiple exact tags from the same family on one team collapse to one family label,
which prevents duplicate weighting of closely related states such as bad_loss and
bounceback_candidate.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Any

from sports_aggregator.cfb import narrative_shapes_v2 as v2
from sports_aggregator.cfb.rating_predictive_power import _load_dataset
from sports_aggregator.cfb.repository import CFBRepository

TEST_SEASON = 2025
MIN_TRAIN_ROWS = 60
MIN_YEAR_ROWS = 15

FAMILY_DEFINITIONS: dict[str, tuple[str, ...]] = {
    "negative_result_rebound": (
        "bad_loss",
        "upset_loss",
        "bounceback_candidate",
    ),
    "positive_result_momentum": (
        "statement_win",
        "upset_win",
        "won_big_then_underdog",
    ),
    "post_success_risk": (
        "letdown_candidate",
    ),
    "market_premium": (
        "market_darling",
        "market_chase",
    ),
    "market_discount": (
        "market_skepticism",
        "market_lag",
    ),
    "schedule_pressure": (
        "lookahead_candidate",
        "sandwich_candidate",
    ),
    "cross_lens_disagreement": (
        "disputed_team",
    ),
}

TAG_TO_FAMILY = {
    tag: family
    for family, tags in FAMILY_DEFINITIONS.items()
    for tag in tags
}


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "n": 0,
            "wins": 0,
            "hit_rate": None,
            "mean_aligned_residual": None,
            "median_aligned_residual": None,
        }
    return {
        "n": len(values),
        "wins": sum(v > 0 for v in values),
        "hit_rate": round(sum(v > 0 for v in values) / len(values), 4),
        "mean_aligned_residual": round(sum(values) / len(values), 3),
        "median_aligned_residual": round(float(median(values)), 3),
    }


def _family_tags(tags: list[str]) -> tuple[set[str], dict[str, list[str]]]:
    by_family: dict[str, list[str]] = defaultdict(list)
    for tag in tags:
        family = TAG_TO_FAMILY.get(tag)
        if family:
            by_family[family].append(tag)
    return set(by_family), dict(by_family)


def _family_observations(
    repository: CFBRepository,
    *,
    start_season: int,
    end_season: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = v2._load_rows(repository)
    selected_rate, _ = v2._choose_line_rate(rows, validation_season=TEST_SEASON - 1)
    line_state, _ = v2._line_elo_pass(rows, learning_rate=selected_rate)
    lookup = {(int(r["game_id"]), str(r["team"])): r for r in rows}

    output: list[dict[str, Any]] = []
    exact_tag_counts: dict[str, Counter] = defaultdict(Counter)
    game_family_membership: dict[int, dict[str, Any]] = {}

    for home in rows:
        season = int(home["season"])
        if (
            season < int(start_season)
            or season > int(end_season)
            or home["side"] != "home"
            or home.get("market_margin_residual") is None
        ):
            continue
        gid = int(home["game_id"])
        away = lookup.get((gid, str(home["opponent"])))
        if not away:
            continue

        home_tags = v2._v2_tags(home, line_state)
        away_tags = v2._v2_tags(away, line_state)
        home_families, home_sources = _family_tags(home_tags)
        away_families, away_sources = _family_tags(away_tags)

        for family, tags in home_sources.items():
            exact_tag_counts[family].update(tags)
        for family, tags in away_sources.items():
            exact_tag_counts[family].update(tags)

        game_family_membership[gid] = {
            "home_families": sorted(home_families),
            "away_families": sorted(away_families),
            "home_source_tags": home_sources,
            "away_source_tags": away_sources,
        }

        home_residual = float(home["market_margin_residual"])
        for home_family in home_families:
            for away_family in away_families:
                first, second = sorted((home_family, away_family))
                if home_family == first:
                    oriented = home_residual
                    orientation_sign = 1
                    source_a = home_sources[home_family]
                    source_b = away_sources[away_family]
                else:
                    oriented = -home_residual
                    orientation_sign = -1
                    source_a = away_sources[away_family]
                    source_b = home_sources[home_family]
                output.append({
                    "game_id": gid,
                    "season": season,
                    "family_a": first,
                    "family_b": second,
                    "residual": oriented,
                    "home_orientation_sign": orientation_sign,
                    "source_tags_a": sorted(source_a),
                    "source_tags_b": sorted(source_b),
                })

    diagnostics = {
        "line_elo_learning_rate": selected_rate,
        "family_definitions": {
            key: list(value) for key, value in FAMILY_DEFINITIONS.items()
        },
        "exact_tag_counts_by_family": {
            family: dict(counts.most_common())
            for family, counts in exact_tag_counts.items()
        },
        "games_with_family_membership": len(game_family_membership),
        "family_observations": len(output),
    }
    return output, diagnostics


def _ratings(repository: CFBRepository, start_season: int, end_season: int):
    return {
        int(row["game_id"]): row
        for row in _load_dataset(
            repository,
            start_season=int(start_season),
            end_season=int(end_season),
        )
    }


def _modifier_split(items: list[dict[str, Any]], rating_rows: dict[int, dict[str, Any]],
                    test_season: int) -> dict[str, Any]:
    enriched = []
    for item in items:
        rating = rating_rows.get(int(item["game_id"]))
        if not rating:
            continue
        orientation = int(item["home_orientation_sign"])
        hc_diff = float(rating["hc_diff"])
        qb_diff = rating.get("qb_diff")
        hc_dir = 1 if hc_diff > 0 else -1 if hc_diff < 0 else 0
        qb_dir = (
            1 if qb_diff is not None and float(qb_diff) > 0
            else -1 if qb_diff is not None and float(qb_diff) < 0
            else 0
        )
        both_dir = hc_dir if hc_dir and hc_dir == qb_dir else 0
        enriched.append({
            **item,
            "hc_relation": hc_dir * orientation if hc_dir else 0,
            "qb_relation": qb_dir * orientation if qb_dir else 0,
            "both_relation": both_dir * orientation if both_dir else 0,
        })

    def subset(relation_key: str, relation: int, *, heldout: bool):
        return [
            float(row["residual"])
            for row in enriched
            if int(row[relation_key]) == relation
            and (
                int(row["season"]) == int(test_season)
                if heldout
                else int(row["season"]) < int(test_season)
            )
        ]

    def signal(relation_key: str):
        return {
            "supports_family_a": {
                "pretest": _summary(subset(relation_key, 1, heldout=False)),
                "heldout_2025": _summary(subset(relation_key, 1, heldout=True)),
            },
            "opposes_family_a": {
                "pretest": _summary(subset(relation_key, -1, heldout=False)),
                "heldout_2025": _summary(subset(relation_key, -1, heldout=True)),
            },
        }

    return {
        "joined_rows": len(enriched),
        "hc": signal("hc_relation"),
        "qb": signal("qb_relation"),
        "both_hc_qb_agree": signal("both_relation"),
    }


def report(repository: CFBRepository, *, start_season: int = 2020,
           end_season: int = 2025, test_season: int = TEST_SEASON) -> dict[str, Any]:
    observations, diagnostics = _family_observations(
        repository,
        start_season=start_season,
        end_season=end_season,
    )
    rating_rows = _ratings(repository, start_season, end_season)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in observations:
        grouped[(item["family_a"], item["family_b"])].append(item)

    families = []
    for (family_a, family_b), items in grouped.items():
        train = [x for x in items if int(x["season"]) < int(test_season)]
        test = [x for x in items if int(x["season"]) == int(test_season)]
        if len(train) < MIN_TRAIN_ROWS:
            continue

        yearly = {}
        signs = []
        for season in sorted({int(x["season"]) for x in train}):
            vals = [
                float(x["residual"])
                for x in train
                if int(x["season"]) == season
            ]
            yearly[str(season)] = _summary(vals)
            if len(vals) >= MIN_YEAR_ROWS:
                mean = sum(vals) / len(vals)
                signs.append(1 if mean > 0 else -1 if mean < 0 else 0)

        train_vals = [float(x["residual"]) for x in train]
        test_vals = [float(x["residual"]) for x in test]
        train_mean = sum(train_vals) / len(train_vals)
        consistency = (
            max(signs.count(1), signs.count(-1)) / len(signs)
            if signs else None
        )
        families.append({
            "family_a": family_a,
            "family_b": family_b,
            "pretest": _summary(train_vals),
            "heldout_2025": _summary(test_vals),
            "year_by_year_pretest": yearly,
            "pretest_direction": (
                "family_a" if train_mean > 0 else "family_b" if train_mean < 0 else "neutral"
            ),
            "direction_consistency": (
                round(consistency, 4) if consistency is not None else None
            ),
            "heldout_direction_persisted": (
                bool(test_vals)
                and ((sum(test_vals) / len(test_vals) > 0) == (train_mean > 0))
            ),
            "rating_modifiers": _modifier_split(
                items, rating_rows, int(test_season)
            ),
        })

    # Ranking is based only on pre-2025 information.
    families.sort(key=lambda row: (
        -(row["direction_consistency"] or 0.0),
        -abs(float(row["pretest"]["mean_aligned_residual"] or 0.0)),
        -int(row["pretest"]["n"]),
    ))

    # Explicitly surface the family relationship suggested by the exact-tag run.
    target = next((
        row for row in families
        if {
            row["family_a"], row["family_b"]
        } == {"negative_result_rebound", "positive_result_momentum"}
    ), None)

    return {
        "version": "cfb-narrative-family-rating-interactions-v1",
        "start_season": int(start_season),
        "end_season": int(end_season),
        "test_season": int(test_season),
        "selection_rules": {
            "family_definitions_fixed_before_heldout_review": True,
            "minimum_pretest_rows": MIN_TRAIN_ROWS,
            "minimum_year_rows_for_direction_consistency": MIN_YEAR_ROWS,
            "family_pair_ranking": (
                "pre-2025 direction consistency, then absolute pre-2025 mean residual, then n"
            ),
        },
        "diagnostics": diagnostics,
        "negative_result_rebound_vs_positive_result_momentum": target,
        "family_pairs": families,
        "notes": [
            "Multiple exact tags from the same family on the same team are deduplicated.",
            "Family-pair residuals are oriented from family_a's team perspective.",
            "HC/QB support means the rating points to family_a's team; oppose means it points to family_b's team.",
            "Both HC+QB is present only when HC and QB agree with each other.",
            "2025 is reported separately and does not select family definitions, minimum sample rules, or ranking.",
            "This remains post-discovery research and does not modify the frozen 2026 convergence/routing model.",
        ],
    }
