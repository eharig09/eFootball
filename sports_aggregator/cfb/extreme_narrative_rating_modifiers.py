"""Research extreme narrative blends with HC/QB Elo modifiers.

Two related questions are kept separate:
1) the previously observed HC+QB agreement / 3-6.5 / Narrative-disagreement
   state, including whether rating or narrative magnitude concentrates it;
2) historically extreme narrative tag-pair blends, selected on pre-2025 data,
   then stratified by HC, QB, and HC+QB support/opposition.

Nothing here changes production convergence or routing rules.
"""
from __future__ import annotations

from collections import defaultdict
import math
from statistics import median
from typing import Any

from sports_aggregator.cfb import narrative_composite as nc
from sports_aggregator.cfb import narrative_shapes_v2 as v2
from sports_aggregator.cfb.rating_narrative_interaction import _joined_rows
from sports_aggregator.cfb.rating_predictive_power import _load_dataset
from sports_aggregator.cfb.repository import CFBRepository

TEST_SEASON = 2025
MIN_BLEND_TRAIN_N = 40
MIN_YEAR_ROWS = 12
TOP_BLEND_COUNT = 15


def _summary_values(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "wins": 0, "hit_rate": None,
                "mean_aligned_residual": None, "median_aligned_residual": None}
    return {
        "n": len(values),
        "wins": sum(v > 0 for v in values),
        "hit_rate": round(sum(v > 0 for v in values) / len(values), 4),
        "mean_aligned_residual": round(sum(values) / len(values), 3),
        "median_aligned_residual": round(float(median(values)), 3),
    }


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = (len(sorted_values) - 1) * q
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(sorted_values[lo])
    w = idx - lo
    return float(sorted_values[lo]) * (1 - w) + float(sorted_values[hi]) * w


def _std(values) -> float:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 1.0
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) or 1.0


def _bucket(value: float, cuts: list[float]) -> str:
    if value < cuts[0]:
        return "low"
    if value < cuts[1]:
        return "mid"
    return "high"


def _focused_state(repository: CFBRepository, *, start_season: int, end_season: int,
                   test_season: int) -> dict[str, Any]:
    rows, _ = _joined_rows(
        repository, start_season=int(start_season), end_season=int(end_season)
    )
    chosen = [
        row for row in rows
        if row.get("spread_bucket") == "3-6.5"
        and int(row.get("both_direction") or 0) != 0
        and int(row.get("narrative_direction") or 0)
            == -int(row.get("both_direction") or 0)
        and row.get("market_residual") is not None
    ]
    train = [r for r in chosen if int(r["season"]) < int(test_season)]
    test = [r for r in chosen if int(r["season"]) == int(test_season)]

    hc_scale = _std(r.get("hc_diff") for r in train)
    qb_scale = _std(r.get("qb_diff") for r in train)

    def rating_magnitude(row):
        return (
            abs(float(row["hc_diff"])) / hc_scale
            + abs(float(row["qb_diff"])) / qb_scale
        ) / 2.0

    def narrative_magnitude(row):
        return abs(float(row.get("narrative_edge") or 0.0))

    rating_train = sorted(rating_magnitude(r) for r in train)
    narrative_train = sorted(narrative_magnitude(r) for r in train)
    rating_cuts = [_percentile(rating_train, 1 / 3), _percentile(rating_train, 2 / 3)]
    narrative_cuts = [
        _percentile(narrative_train, 1 / 3),
        _percentile(narrative_train, 2 / 3),
    ]

    enriched = []
    for row in chosen:
        aligned = float(row["market_residual"]) * int(row["both_direction"])
        enriched.append({
            **row,
            "aligned_residual": aligned,
            "rating_magnitude": rating_magnitude(row),
            "narrative_magnitude": narrative_magnitude(row),
            "rating_magnitude_bucket": _bucket(rating_magnitude(row), rating_cuts),
            "narrative_magnitude_bucket": _bucket(narrative_magnitude(row), narrative_cuts),
        })

    def summarize(group):
        return _summary_values([float(r["aligned_residual"]) for r in group])

    by_season = {
        str(season): summarize([r for r in enriched if int(r["season"]) == season])
        for season in sorted({int(r["season"]) for r in enriched})
    }
    rating_ladder = {
        label: {
            "pretest": summarize([
                r for r in enriched
                if int(r["season"]) < int(test_season)
                and r["rating_magnitude_bucket"] == label
            ]),
            "heldout_2025": summarize([
                r for r in enriched
                if int(r["season"]) == int(test_season)
                and r["rating_magnitude_bucket"] == label
            ]),
        }
        for label in ("low", "mid", "high")
    }
    narrative_ladder = {
        label: {
            "pretest": summarize([
                r for r in enriched
                if int(r["season"]) < int(test_season)
                and r["narrative_magnitude_bucket"] == label
            ]),
            "heldout_2025": summarize([
                r for r in enriched
                if int(r["season"]) == int(test_season)
                and r["narrative_magnitude_bucket"] == label
            ]),
        }
        for label in ("low", "mid", "high")
    }
    cross = []
    for rb in ("low", "mid", "high"):
        for nb in ("low", "mid", "high"):
            cross.append({
                "rating_magnitude": rb,
                "narrative_magnitude": nb,
                "pretest": summarize([
                    r for r in enriched
                    if int(r["season"]) < int(test_season)
                    and r["rating_magnitude_bucket"] == rb
                    and r["narrative_magnitude_bucket"] == nb
                ]),
                "heldout_2025": summarize([
                    r for r in enriched
                    if int(r["season"]) == int(test_season)
                    and r["rating_magnitude_bucket"] == rb
                    and r["narrative_magnitude_bucket"] == nb
                ]),
            })

    return {
        "definition": "HC and QB agree; market spread is 3-6.5; Narrative points to the opposite side.",
        "all_years": summarize(enriched),
        "pretest_through_2024": summarize([
            r for r in enriched if int(r["season"]) < int(test_season)
        ]),
        "heldout_2025": summarize([
            r for r in enriched if int(r["season"]) == int(test_season)
        ]),
        "by_season": by_season,
        "training_magnitude_scales": {
            "hc_diff_std": round(hc_scale, 4),
            "qb_diff_std": round(qb_scale, 4),
            "rating_tercile_cuts": [round(x, 4) for x in rating_cuts],
            "narrative_abs_edge_tercile_cuts": [round(x, 4) for x in narrative_cuts],
        },
        "rating_magnitude_ladder": rating_ladder,
        "narrative_magnitude_ladder": narrative_ladder,
        "rating_x_narrative_magnitude": cross,
    }


def _rating_map(repository: CFBRepository, start_season: int, end_season: int):
    rows = _load_dataset(
        repository, start_season=int(start_season), end_season=int(end_season)
    )
    return {int(r["game_id"]): r for r in rows}


def _blend_modifier_summary(items: list[dict[str, Any]], ratings: dict[int, dict[str, Any]],
                            test_season: int) -> dict[str, Any]:
    enriched = []
    for item in items:
        rating = ratings.get(int(item["game_id"]))
        if not rating:
            continue
        orientation = int(1 if float(item["home_orientation_sign"]) > 0 else -1)
        hc_dir = 1 if float(rating["hc_diff"]) > 0 else -1 if float(rating["hc_diff"]) < 0 else 0
        qb = rating.get("qb_diff")
        qb_dir = 1 if qb is not None and float(qb) > 0 else -1 if qb is not None and float(qb) < 0 else 0
        both_dir = hc_dir if hc_dir and hc_dir == qb_dir else 0
        enriched.append({
            **item,
            "hc_relation": hc_dir * orientation if hc_dir else 0,
            "qb_relation": qb_dir * orientation if qb_dir else 0,
            "both_relation": both_dir * orientation if both_dir else 0,
        })

    def summarize(rows):
        return _summary_values([float(r["residual"]) for r in rows])

    def split(relation_key):
        return {
            "supports_blend_side": {
                "pretest": summarize([
                    r for r in enriched
                    if int(r["season"]) < int(test_season)
                    and int(r[relation_key]) == 1
                ]),
                "heldout_2025": summarize([
                    r for r in enriched
                    if int(r["season"]) == int(test_season)
                    and int(r[relation_key]) == 1
                ]),
            },
            "opposes_blend_side": {
                "pretest": summarize([
                    r for r in enriched
                    if int(r["season"]) < int(test_season)
                    and int(r[relation_key]) == -1
                ]),
                "heldout_2025": summarize([
                    r for r in enriched
                    if int(r["season"]) == int(test_season)
                    and int(r[relation_key]) == -1
                ]),
            },
        }

    return {
        "joined_rows": len(enriched),
        "hc_modifier": split("hc_relation"),
        "qb_modifier": split("qb_relation"),
        "both_hc_qb_agree_modifier": split("both_relation"),
    }


def _extreme_blends(repository: CFBRepository, *, start_season: int, end_season: int,
                    test_season: int) -> dict[str, Any]:
    rows = v2._load_rows(repository)
    selected_rate, _ = v2._choose_line_rate(
        rows, validation_season=int(test_season) - 1
    )
    line_state, _ = v2._line_elo_pass(rows, learning_rate=selected_rate)
    observations = [
        x for x in nc._interaction_observations(rows, line_state)
        if int(start_season) <= int(x["season"]) <= int(end_season)
    ]
    ratings = _rating_map(repository, start_season, end_season)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in observations:
        grouped[(item["narrative_a"], item["narrative_b"])].append(item)

    candidates = []
    for key, items in grouped.items():
        train = [x for x in items if int(x["season"]) < int(test_season)]
        if len(train) < MIN_BLEND_TRAIN_N:
            continue
        yearly_signs = []
        for season in sorted({int(x["season"]) for x in train}):
            vals = [float(x["residual"]) for x in train if int(x["season"]) == season]
            if len(vals) >= MIN_YEAR_ROWS:
                yearly_signs.append(1 if sum(vals) / len(vals) > 0 else -1)
        if len(yearly_signs) < 2:
            continue
        mean_train = sum(float(x["residual"]) for x in train) / len(train)
        consistency = max(yearly_signs.count(1), yearly_signs.count(-1)) / len(yearly_signs)
        candidates.append({
            "key": key,
            "items": items,
            "train_n": len(train),
            "train_mean": mean_train,
            "direction_consistency": consistency,
        })

    # "Extreme" is defined before the 2025 holdout: largest absolute pretest
    # market residual among blends with repeated-year support.
    candidates.sort(key=lambda x: (
        -abs(float(x["train_mean"])),
        -float(x["direction_consistency"]),
        -int(x["train_n"]),
    ))
    selected = candidates[:TOP_BLEND_COUNT]

    output = []
    for candidate in selected:
        a, b = candidate["key"]
        items = candidate["items"]
        train = [x for x in items if int(x["season"]) < int(test_season)]
        test = [x for x in items if int(x["season"]) == int(test_season)]

        # Extreme-intensity cutoff is also trained pre-2025. Intensity is
        # standardized within this blend using pretest distributions only.
        usable_train = [
            x for x in train
            if x.get("intensity_a") is not None and x.get("intensity_b") is not None
        ]
        usable_test = [
            x for x in test
            if x.get("intensity_a") is not None and x.get("intensity_b") is not None
        ]
        intensity = None
        if len(usable_train) >= MIN_BLEND_TRAIN_N:
            ma = sum(float(x["intensity_a"]) for x in usable_train) / len(usable_train)
            mb = sum(float(x["intensity_b"]) for x in usable_train) / len(usable_train)
            sa = _std(x["intensity_a"] for x in usable_train)
            sb = _std(x["intensity_b"] for x in usable_train)
            def score(x):
                return (
                    (float(x["intensity_a"]) - ma) / sa
                    + (float(x["intensity_b"]) - mb) / sb
                ) / 2.0
            cutoff = _percentile(sorted(score(x) for x in usable_train), 0.75)
            extreme_train = [x for x in usable_train if score(x) >= cutoff]
            extreme_test = [x for x in usable_test if score(x) >= cutoff]
            intensity = {
                "pretest_q75_cutoff": round(cutoff, 4),
                "extreme_pretest": _summary_values([
                    float(x["residual"]) for x in extreme_train
                ]),
                "extreme_heldout_2025": _summary_values([
                    float(x["residual"]) for x in extreme_test
                ]),
                "extreme_modifiers": _blend_modifier_summary(
                    extreme_train + extreme_test, ratings, test_season
                ),
            }

        output.append({
            "narrative_a": a,
            "narrative_b": b,
            "selection_basis_pre_2025": {
                "n": candidate["train_n"],
                "mean_residual_from_narrative_a_side": round(candidate["train_mean"], 3),
                "abs_mean_residual": round(abs(candidate["train_mean"]), 3),
                "direction_consistency": round(candidate["direction_consistency"], 4),
            },
            "pretest": _summary_values([float(x["residual"]) for x in train]),
            "heldout_2025": _summary_values([float(x["residual"]) for x in test]),
            "rating_modifiers": _blend_modifier_summary(items, ratings, test_season),
            "extreme_intensity_q4": intensity,
        })

    return {
        "selection_rule": {
            "test_season": int(test_season),
            "minimum_pretest_rows": MIN_BLEND_TRAIN_N,
            "minimum_year_rows_for_consistency": MIN_YEAR_ROWS,
            "top_blends": TOP_BLEND_COUNT,
            "ranking": "absolute pre-2025 mean residual, then direction consistency, then n",
        },
        "blends": output,
    }


def report(repository: CFBRepository, *, start_season: int = 2020,
           end_season: int = 2025, test_season: int = TEST_SEASON) -> dict[str, Any]:
    return {
        "version": "cfb-extreme-narrative-rating-modifiers-v1",
        "start_season": int(start_season),
        "end_season": int(end_season),
        "test_season": int(test_season),
        "focused_3_to_6_5_hc_qb_agree_narrative_disagrees": _focused_state(
            repository,
            start_season=start_season,
            end_season=end_season,
            test_season=test_season,
        ),
        "extreme_narrative_blends_with_rating_modifiers": _extreme_blends(
            repository,
            start_season=start_season,
            end_season=end_season,
            test_season=test_season,
        ),
        "notes": [
            "Research-only; no production convergence or routing definition is changed.",
            "Extreme narrative blends are selected using pre-2025 data only; 2025 is then shown separately.",
            "Blend residuals are oriented from narrative_a's side, matching the existing interaction research.",
            "HC/QB support means that rating points to narrative_a's side; oppose means it points to the other team.",
            "BOTH modifier exists only when HC and QB agree with each other.",
            "Extreme-intensity Q4 cutoffs are learned from each blend's pre-2025 intensity distribution only.",
            "Focused-state rating magnitude uses pre-2025 HC/QB scales and pre-2025 tercile cutoffs.",
            "These are post-discovery analyses and should not alter the frozen 2026 holdout without a separately frozen hypothesis.",
        ],
    }
