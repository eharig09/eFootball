"""Narrative interaction stability, intensity gradients, and multi-lens agreement.

Research-only. 2025 remains the final holdout.  Composite thresholds are chosen
on 2024 after normalization/calibration is learned from 2022-23, then frozen.
"""
from __future__ import annotations

from collections import defaultdict
import math
from statistics import median
from typing import Any

from sports_aggregator.cfb import narrative_shapes as v1
from sports_aggregator.cfb import narrative_shapes_v2 as v2
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.repository import CFBRepository

MIN_YEAR_ROWS = 12
AGREEMENT_GRID = (0.50, 0.60, 0.67, 0.75, 0.80, 1.00)
MAGNITUDE_GRID = (0.25, 0.50, 0.75, 1.00, 1.25, 1.50)
MIN_SIGNAL_GRID = (3, 4, 5)

SIGNAL_KEYS = (
    "football_lab_edge",
    "elo_edge",
    "fpi_edge",
    "core_edge",
    "line_elo_edge",
    "narrative_interaction_edge",
)


def _mean(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _std(values) -> float:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 1.0
    avg = sum(vals) / len(vals)
    return math.sqrt(sum((v - avg) ** 2 for v in vals) / len(vals)) or 1.0


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "positive_rate": None}
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 3),
        "median": round(float(median(values)), 3),
        "positive_rate": round(sum(v > 0 for v in values) / len(values), 4),
    }


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    index = (len(sorted_values) - 1) * q
    low = int(math.floor(index)); high = int(math.ceil(index))
    if low == high:
        return sorted_values[low]
    weight = index - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def _tag_intensity(tag: str, row: dict[str, Any],
                   state: dict[str, float | None]) -> float | None:
    prev_market = row.get("previous_market_surprise")
    prev_elo = row.get("previous_elo_surprise")
    prev_expected = row.get("previous_market_expected_margin")
    gap = state.get("centered_line_gap")
    change = state.get("perception_change_v2")
    if tag == "statement_win":
        return max(0.0, float(prev_elo)) if prev_elo is not None else None
    if tag == "upset_win":
        if prev_expected is None:
            return None
        return max(0.0, -float(prev_expected)) + max(0.0, float(prev_market or 0.0))
    if tag == "bad_loss":
        return max(0.0, -float(prev_market)) if prev_market is not None else None
    if tag == "upset_loss":
        if prev_expected is None:
            return None
        return max(0.0, float(prev_expected)) + max(0.0, -float(prev_market or 0.0))
    if tag == "letdown_candidate":
        vals = [v for v in (prev_market, prev_elo) if v is not None]
        return max([0.0] + [float(v) for v in vals])
    if tag == "bounceback_candidate":
        return max(0.0, -float(prev_market)) if prev_market is not None else None
    if tag == "won_big_then_underdog":
        return max(0.0, float(prev_elo)) if prev_elo is not None else None
    if tag == "market_darling":
        return max(0.0, float(gap)) if gap is not None else None
    if tag == "market_skepticism":
        return max(0.0, -float(gap)) if gap is not None else None
    if tag == "market_chase":
        return max(0.0, float(change)) if change is not None else None
    if tag == "market_lag":
        return max(0.0, -float(change)) if change is not None else None
    if tag == "lookahead_candidate":
        value = row.get("lookahead_score")
        return max(0.0, float(value)) if value is not None else None
    if tag == "sandwich_candidate":
        value = row.get("sandwich_score")
        return max(0.0, float(value)) if value is not None else None
    if tag == "disputed_team":
        lenses = [row.get("elo_expected_margin"), row.get("fpi_margin"),
                  row.get("core_margin"), row.get("market_expected_margin")]
        vals = [float(v) for v in lenses if v is not None]
        return max(vals) - min(vals) if len(vals) >= 2 else None
    return None


def _interaction_observations(rows: list[dict[str, Any]],
                              line_state: dict[tuple[int, str], dict[str, float | None]]
                              ) -> list[dict[str, Any]]:
    lookup = {(int(r["game_id"]), str(r["team"])): r for r in rows}
    out = []
    for home in rows:
        if home["side"] != "home" or home.get("market_margin_residual") is None:
            continue
        gid = int(home["game_id"])
        away = lookup.get((gid, str(home["opponent"])))
        if not away:
            continue
        home_tags = v2._v2_tags(home, line_state)
        away_tags = v2._v2_tags(away, line_state)
        home_resid = float(home["market_margin_residual"])
        for htag in home_tags:
            for atag in away_tags:
                first, second = sorted((htag, atag))
                if htag == first:
                    oriented = home_resid
                    arow, brow = home, away
                else:
                    oriented = -home_resid
                    arow, brow = away, home
                ai = _tag_intensity(first, arow, line_state.get((gid, str(arow["team"])), {}))
                bi = _tag_intensity(second, brow, line_state.get((gid, str(brow["team"])), {}))
                out.append({
                    "game_id": gid,
                    "season": int(home["season"]),
                    "narrative_a": first,
                    "narrative_b": second,
                    "residual": oriented,
                    "home_orientation_sign": 1.0 if htag == first else -1.0,
                    "intensity_a": ai,
                    "intensity_b": bi,
                })
    return out


def stability_report(repository: CFBRepository, *, test_season: int = 2025,
                     min_year_rows: int = MIN_YEAR_ROWS) -> dict[str, Any]:
    rows = v2._load_rows(repository)
    selected_rate, _ = v2._choose_line_rate(rows, validation_season=int(test_season) - 1)
    line_state, _ = v2._line_elo_pass(rows, learning_rate=selected_rate)
    observations = _interaction_observations(rows, line_state)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in observations:
        grouped[(item["narrative_a"], item["narrative_b"])].append(item)

    interactions = []
    for (a, b), items in grouped.items():
        yearly = {}
        signs = []
        eligible_total = 0
        for season in sorted({int(x["season"]) for x in items}):
            vals = [float(x["residual"]) for x in items if int(x["season"]) == season]
            summary = _summary(vals)
            yearly[str(season)] = summary
            if len(vals) >= int(min_year_rows):
                eligible_total += len(vals)
                signs.append(1 if sum(vals) / len(vals) > 0 else -1)
        if len(signs) < 2:
            continue
        direction_consistency = max(signs.count(1), signs.count(-1)) / len(signs)
        all_vals = [float(x["residual"]) for x in items]
        train_vals = [float(x["residual"]) for x in items
                      if int(x["season"]) < int(test_season)]
        test_vals = [float(x["residual"]) for x in items
                     if int(x["season"]) == int(test_season)]
        interactions.append({
            "narrative_a": a,
            "narrative_b": b,
            "eligible_years": len(signs),
            "direction_consistency": round(direction_consistency, 4),
            "all_years": _summary(all_vals),
            "pretest": _summary(train_vals),
            "heldout_test": _summary(test_vals),
            "year_by_year": yearly,
            "heldout_direction_persisted": (
                bool(test_vals) and bool(train_vals)
                and ((sum(test_vals) / len(test_vals) > 0)
                     == (sum(train_vals) / len(train_vals) > 0))
            ),
        })

    interactions.sort(key=lambda x: (
        -x["direction_consistency"],
        -x["eligible_years"],
        -min(x["pretest"]["n"], x["heldout_test"]["n"]),
        -abs(float(x["heldout_test"]["mean"] or 0.0)),
    ))

    # Intensity gradient: normalize each tag's intensity using pre-2025 history,
    # combine the two z magnitudes, and evaluate fixed pretest quartile cutoffs.
    intensity_rows = []
    for interaction in interactions[:40]:
        a, b = interaction["narrative_a"], interaction["narrative_b"]
        items = grouped[(a, b)]
        train = [x for x in items if int(x["season"]) < int(test_season)
                 and x["intensity_a"] is not None and x["intensity_b"] is not None]
        test = [x for x in items if int(x["season"]) == int(test_season)
                and x["intensity_a"] is not None and x["intensity_b"] is not None]
        if len(train) < 40 or len(test) < 12:
            continue
        mean_a = _mean(x["intensity_a"] for x in train) or 0.0
        mean_b = _mean(x["intensity_b"] for x in train) or 0.0
        std_a = _std(x["intensity_a"] for x in train)
        std_b = _std(x["intensity_b"] for x in train)
        def score(x):
            za = (float(x["intensity_a"]) - mean_a) / std_a
            zb = (float(x["intensity_b"]) - mean_b) / std_b
            return (za + zb) / 2.0
        train_scores = sorted(score(x) for x in train)
        cuts = [_percentile(train_scores, q) for q in (0.25, 0.50, 0.75)]
        def bins(dataset):
            result = []
            bounds = [float("-inf"), *cuts, float("inf")]
            for idx in range(4):
                vals = [float(x["residual"]) for x in dataset
                        if bounds[idx] <= score(x) < bounds[idx + 1]]
                result.append({
                    "quartile": idx + 1,
                    "n": len(vals),
                    "mean_residual": round(sum(vals) / len(vals), 3) if vals else None,
                    "positive_rate": round(sum(v > 0 for v in vals) / len(vals), 4)
                    if vals else None,
                })
            return result
        train_bins, test_bins = bins(train), bins(test)
        train_means = [x["mean_residual"] for x in train_bins if x["mean_residual"] is not None]
        test_means = [x["mean_residual"] for x in test_bins if x["mean_residual"] is not None]
        intensity_rows.append({
            "narrative_a": a,
            "narrative_b": b,
            "train_quartiles": train_bins,
            "test_quartiles": test_bins,
            "train_extreme_minus_mild": (
                round(train_means[-1] - train_means[0], 3) if len(train_means) == 4 else None
            ),
            "test_extreme_minus_mild": (
                round(test_means[-1] - test_means[0], 3) if len(test_means) == 4 else None
            ),
        })

    return {
        "version": "narrative-interaction-stability-v1",
        "test_season": int(test_season),
        "line_elo_learning_rate": selected_rate,
        "min_year_rows": int(min_year_rows),
        "interactions": interactions[:75],
        "intensity_gradients": intensity_rows,
        "notes": [
            "Year-by-year rows are independent games with mirrored interactions canonicalized.",
            "Intensity quartile cutoffs are learned only from seasons before the held-out test.",
            "Interaction intensity is the average standardized magnitude of both narrative states.",
        ],
    }


def _projection_rows(repository: CFBRepository) -> dict[tuple[int, str], dict[str, Any]]:
    with repository._reader() as connection:
        return {
            (int(r["game_id"]), str(r["team"])): dict(r)
            for r in connection.execute(
                """SELECT game_id,team,side,season,projected_offensive_points,
                          actual_score_points,market_spread
                   FROM cfb_projection_backtest
                   WHERE backtest_version=?""",
                (BACKTEST_VERSION,),
            )
        }


def _prior_interaction_means(observations: list[dict[str, Any]],
                             target_season: int) -> dict[tuple[str, str], float]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for x in observations:
        if int(x["season"]) < int(target_season):
            grouped[(x["narrative_a"], x["narrative_b"])].append(float(x["residual"]))
    return {key: sum(vals) / len(vals) for key, vals in grouped.items() if len(vals) >= 30}


def _composite_games(repository: CFBRepository, *, test_season: int,
                     line_rate: float) -> list[dict[str, Any]]:
    rows = v2._load_rows(repository)
    line_state, _ = v2._line_elo_pass(rows, learning_rate=line_rate)
    lookup = {(int(r["game_id"]), str(r["team"])): r for r in rows}
    projections = _projection_rows(repository)
    interaction_obs = _interaction_observations(rows, line_state)

    interactions_by_game: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for x in interaction_obs:
        interactions_by_game[int(x["game_id"])].append(x)

    out = []
    for home in rows:
        if home["side"] != "home" or home.get("market_margin_residual") is None:
            continue
        gid = int(home["game_id"]); season = int(home["season"])
        away = lookup.get((gid, str(home["opponent"])))
        if not away:
            continue
        market_margin = home.get("market_expected_margin")
        if market_margin is None:
            continue
        market_margin = float(market_margin)
        hp = projections.get((gid, str(home["team"])))
        ap = projections.get((gid, str(home["opponent"])))
        football_lab = None
        if hp and ap and hp.get("projected_offensive_points") is not None and ap.get("projected_offensive_points") is not None:
            football_lab = (
                float(hp["projected_offensive_points"])
                - float(ap["projected_offensive_points"])
                - market_margin
            )
        elo_expected = home.get("elo_expected_margin")
        fpi = home.get("fpi_margin")
        core = home.get("core_margin")
        pre_home = line_state.get((gid, str(home["team"])), {}).get("pre_line_elo")
        pre_away = line_state.get((gid, str(home["opponent"])), {}).get("pre_line_elo")
        line_edge = None
        if pre_home is not None and pre_away is not None:
            line_pred_margin = (
                (float(pre_home) - float(pre_away)) / v1.ELO_POINTS_PER_SCORE_POINT
                + v1.DEFAULT_HOME_FIELD_POINTS
            )
            line_edge = line_pred_margin - market_margin

        prior_means = _prior_interaction_means(interaction_obs, season)
        narrative_edges = []
        for x in interactions_by_game.get(gid, []):
            key = (x["narrative_a"], x["narrative_b"])
            prior = prior_means.get(key)
            if prior is None:
                continue
            # Historical interaction means are oriented from narrative_a.
            # Convert them back to the current home-team perspective.
            narrative_edges.append(float(prior) * float(x["home_orientation_sign"]))
        narrative_edge = _mean(narrative_edges)

        out.append({
            "game_id": gid,
            "season": season,
            "team": str(home["team"]),
            "opponent": str(home["opponent"]),
            "market_margin_residual": float(home["market_margin_residual"]),
            "football_lab_edge": football_lab,
            "elo_edge": (float(elo_expected) - market_margin) if elo_expected is not None else None,
            "fpi_edge": (float(fpi) - market_margin) if fpi is not None else None,
            "core_edge": (float(core) - market_margin) if core is not None else None,
            "line_elo_edge": line_edge,
            "narrative_interaction_edge": narrative_edge,
        })
    return out


def _normalization(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {key: _std(r.get(key) for r in rows if r.get(key) is not None) for key in SIGNAL_KEYS}


def _score(row: dict[str, Any], scales: dict[str, float]) -> dict[str, Any]:
    zs = []
    raw = {}
    for key in SIGNAL_KEYS:
        value = row.get(key)
        if value is None:
            continue
        z = float(value) / float(scales.get(key) or 1.0)
        zs.append(z); raw[key] = z
    if not zs:
        return {"available": 0, "agreement_ratio": None, "score": None, "direction": 0, "zs": raw}
    score = sum(zs) / len(zs)
    direction = 1 if score > 0 else -1 if score < 0 else 0
    agree = sum(1 for z in zs if (z > 0) == (direction > 0)) if direction else 0
    return {
        "available": len(zs),
        "agreement_ratio": agree / len(zs) if direction else 0.0,
        "score": score,
        "direction": direction,
        "zs": raw,
    }


def _fit_slope(rows: list[dict[str, Any]], scales: dict[str, float]) -> float:
    pairs = []
    for row in rows:
        s = _score(row, scales)["score"]
        if s is not None:
            pairs.append((float(s), float(row["market_margin_residual"])))
    denom = sum(x * x for x, _ in pairs)
    return sum(x * y for x, y in pairs) / denom if denom else 0.0


def _subset_metrics(rows: list[dict[str, Any]], scales: dict[str, float], slope: float,
                    *, min_agreement: float, min_magnitude: float,
                    min_signals: int) -> dict[str, Any]:
    chosen = []
    all_adjusted_errors = []
    all_market_errors = []
    for row in rows:
        s = _score(row, scales)
        if (s["score"] is None or s["available"] < int(min_signals)
                or s["agreement_ratio"] < float(min_agreement)
                or abs(float(s["score"])) < float(min_magnitude)):
            continue
        actual = float(row["market_margin_residual"])
        predicted = float(slope) * float(s["score"])
        chosen.append((s, actual, predicted))
        all_adjusted_errors.append(predicted - actual)
        all_market_errors.append(-actual)
    if not chosen:
        return {"n": 0, "directional_hit_rate": None, "mean_aligned_residual": None,
                "market_mae": None, "composite_mae": None, "mae_delta": None}
    aligned = [actual * s["direction"] for s, actual, _ in chosen]
    return {
        "n": len(chosen),
        "directional_hit_rate": round(sum(v > 0 for v in aligned) / len(aligned), 4),
        "mean_aligned_residual": round(sum(aligned) / len(aligned), 3),
        "median_aligned_residual": round(float(median(aligned)), 3),
        "mean_abs_composite_score": round(
            sum(abs(float(s["score"])) for s, _, _ in chosen) / len(chosen), 3),
        "mean_agreement_ratio": round(
            sum(float(s["agreement_ratio"]) for s, _, _ in chosen) / len(chosen), 4),
        "market_mae": round(sum(abs(e) for e in all_market_errors) / len(chosen), 4),
        "composite_mae": round(sum(abs(e) for e in all_adjusted_errors) / len(chosen), 4),
        "mae_delta": round(
            sum(abs(e) for e in all_adjusted_errors) / len(chosen)
            - sum(abs(e) for e in all_market_errors) / len(chosen), 4),
    }


def composite_report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    narrative_rows = v2._load_rows(repository)
    validation_season = int(test_season) - 1
    line_rate, _ = v2._choose_line_rate(narrative_rows, validation_season=validation_season)
    games = _composite_games(repository, test_season=test_season, line_rate=line_rate)

    fit = [r for r in games if int(r["season"]) < validation_season]
    validation = [r for r in games if int(r["season"]) == validation_season]
    test = [r for r in games if int(r["season"]) == int(test_season)]
    scales = _normalization(fit)
    slope = _fit_slope(fit, scales)

    grid = []
    for min_signals in MIN_SIGNAL_GRID:
        for agreement in AGREEMENT_GRID:
            for magnitude in MAGNITUDE_GRID:
                metrics = _subset_metrics(
                    validation, scales, slope,
                    min_agreement=agreement,
                    min_magnitude=magnitude,
                    min_signals=min_signals,
                )
                grid.append({
                    "min_signals": min_signals,
                    "min_agreement": agreement,
                    "min_magnitude": magnitude,
                    "validation": metrics,
                })
    eligible = [g for g in grid if g["validation"]["n"] >= 50]
    # Select for actual prediction improvement first; tie-break on directional
    # quality and then sample size.  2025 is not consulted.
    selected = min(
        eligible,
        key=lambda g: (
            g["validation"]["mae_delta"],
            -float(g["validation"]["directional_hit_rate"] or 0.0),
            -g["validation"]["n"],
        ),
    )
    policy = {
        "min_signals": selected["min_signals"],
        "min_agreement": selected["min_agreement"],
        "min_magnitude": selected["min_magnitude"],
    }
    test_selected = _subset_metrics(test, scales, slope, **policy)

    # Descriptive held-out ladders answer "what happens when more agree?" without
    # selecting a new threshold from 2025.
    agreement_ladder = []
    for agreement in AGREEMENT_GRID:
        agreement_ladder.append({
            "min_agreement": agreement,
            "test": _subset_metrics(
                test, scales, slope, min_agreement=agreement,
                min_magnitude=0.0, min_signals=4),
        })
    magnitude_ladder = []
    for magnitude in MAGNITUDE_GRID:
        magnitude_ladder.append({
            "min_magnitude": magnitude,
            "test": _subset_metrics(
                test, scales, slope, min_agreement=0.5,
                min_magnitude=magnitude, min_signals=4),
        })

    # Refit only the calibration slope through 2024 after policy selection.
    # Keep 2022-23 normalization frozen so the selected magnitude threshold
    # retains exactly the same meaning in the held-out season.
    final_train = [r for r in games if int(r["season"]) < int(test_season)]
    final_slope = _fit_slope(final_train, scales)
    final_test = _subset_metrics(test, scales, final_slope, **policy)

    return {
        "version": "multi-lens-composite-v1",
        "test_season": int(test_season),
        "validation_season": validation_season,
        "signals": list(SIGNAL_KEYS),
        "signal_definition": (
            "Every signal is oriented as home-team edge versus the closing market. "
            "Positive means the lens prefers the home side relative to the line."
        ),
        "fit_rows": len(fit),
        "validation_rows": len(validation),
        "test_rows": len(test),
        "training_scales": {k: round(v, 4) for k, v in scales.items()},
        "training_calibration_slope": round(slope, 4),
        "selected_policy_on_validation": {
            **policy,
            "validation_metrics": selected["validation"],
        },
        "heldout_test_with_prevalidation_calibration": test_selected,
        "final_refit_through_validation": {
            "calibration_slope": round(final_slope, 4),
            "heldout_test": final_test,
        },
        "heldout_agreement_ladder": agreement_ladder,
        "heldout_magnitude_ladder": magnitude_ladder,
        "notes": [
            "2025 never selects the composite policy.",
            "Physical team-shape adjustments are not separately double-counted; production Football Lab is one lens.",
            "Narrative interaction edge uses only interaction history from seasons before each game.",
            "Composite magnitude is the mean standardized signed edge across available lenses.",
            "Agreement is the fraction of available lenses sharing the composite direction.",
        ],
    }
