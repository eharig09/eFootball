"""Tests for the HC/QB Elo predictive-power stat functions."""
from __future__ import annotations

import math

from sports_aggregator.cfb.rating_predictive_power import (
    SPREAD_CLOSENESS_BUCKETS, _error_summary, _ols, _partial_correlation, _pearson,
    _win_rate_when_favored, _zscore_diff, agreement_report, directional_edge_report,
    magnitude_report, vegas_comparison_report,
)


def test_pearson_perfect_positive_correlation():
    pairs = [(x, 2 * x + 1) for x in range(10)]
    assert math.isclose(_pearson(pairs), 1.0, abs_tol=1e-9)


def test_pearson_no_correlation_is_near_zero():
    pairs = [(0, 5), (1, -5), (2, 5), (3, -5)]
    r = _pearson(pairs)
    assert r is not None and abs(r) < 0.5


def test_pearson_needs_at_least_two_points():
    assert _pearson([(1.0, 1.0)]) is None
    assert _pearson([]) is None


def test_win_rate_when_favored_only_counts_positive_diffs():
    rows = [
        {"hc_diff": 10, "home_won": True},
        {"hc_diff": 5, "home_won": False},
        {"hc_diff": -5, "home_won": True},  # away favored -- excluded
        {"hc_diff": None, "home_won": True},  # no signal -- excluded
    ]
    result = _win_rate_when_favored(rows, "hc_diff")
    assert result["n"] == 2
    assert result["home_favored_win_rate"] == 0.5


def test_ols_univariate_recovers_a_known_linear_relationship():
    rows = [{"x": x, "y": 3.0 * x + 2.0} for x in range(20)]
    model = _ols(rows, ("x",), "y")
    assert model is not None
    intercept, slope = model["coefficients"]
    assert math.isclose(intercept, 2.0, abs_tol=1e-6)
    assert math.isclose(slope, 3.0, abs_tol=1e-6)
    assert math.isclose(model["r_squared"], 1.0, abs_tol=1e-6)


def test_zscore_diff_centers_on_zero():
    rows = [{"game_id": i, "hc_diff": v} for i, v in enumerate([10.0, 20.0, 30.0])]
    z = _zscore_diff(rows, "hc_diff")
    assert abs(z[1]) < 1e-9  # the middle value maps to the mean -> z=0


def test_agreement_report_buckets_by_sign_agreement():
    rows = [
        {"hc_diff": 10, "qb_diff": 10, "home_won": True, "actual_margin": 14},
        {"hc_diff": 10, "qb_diff": -5, "home_won": True, "actual_margin": 3},
        {"hc_diff": -10, "qb_diff": -10, "home_won": False, "actual_margin": -20},
    ]
    result = agreement_report(rows)
    assert result["2-0 (both favor home)"]["n"] == 1
    assert result["1-1 (split)"]["n"] == 1
    assert result["0-2 (both favor away)"]["n"] == 1
    assert result["2-0 (both favor home)"]["home_win_rate"] == 1.0
    assert result["0-2 (both favor away)"]["home_win_rate"] == 0.0


def test_partial_correlation_removes_a_pure_confound():
    # x and y are each driven only by z, with no direct x-y relationship --
    # the raw x-y correlation should be substantial (both track z), but the
    # partial correlation controlling for z should collapse toward zero.
    import random
    rng = random.Random(0)
    triplets = []
    for _ in range(200):
        z = rng.uniform(0, 10)
        x = z + rng.gauss(0, 0.1)
        y = 2 * z + rng.gauss(0, 0.1)
        triplets.append((x, y, z))
    result = _partial_correlation(triplets)
    assert result is not None
    assert result["raw_correlation_x_vs_y"] > 0.9  # confounded, looks strongly related
    assert abs(result["partial_correlation_x_vs_y_given_z"]) < 0.2  # but isn't, once z is controlled for


def test_partial_correlation_preserves_a_real_independent_relationship():
    # Here x really does predict y beyond what z explains -- the partial
    # correlation should stay strong, not get washed out.
    import random
    rng = random.Random(1)
    triplets = []
    for _ in range(200):
        z = rng.uniform(0, 10)
        x = rng.uniform(0, 10)
        y = x + 0.1 * z + rng.gauss(0, 0.1)
        triplets.append((x, y, z))
    result = _partial_correlation(triplets)
    assert result is not None
    assert result["partial_correlation_x_vs_y_given_z"] > 0.9


def test_partial_correlation_needs_enough_points():
    assert _partial_correlation([(1, 1, 1), (2, 2, 2)]) is None


def test_error_summary_computes_mae_rmse_and_correlation():
    rows = [
        {"pred": 10.0, "actual_margin": 8.0},
        {"pred": -5.0, "actual_margin": -5.0},
    ]
    result = _error_summary(rows, "pred")
    assert result["n"] == 2
    assert result["mae"] == 1.0
    assert math.isclose(result["rmse"], math.sqrt(2.0), abs_tol=1e-3)


def test_error_summary_empty_is_zero_not_a_crash():
    assert _error_summary([], "pred") == {"n": 0}


def _synthetic_game(game_id, season, hc, qb, market, actual):
    return {
        "game_id": game_id, "season": season, "week": 1,
        "hc_diff": hc, "qb_diff": qb, "market_margin": market,
        "actual_margin": actual, "home_won": actual > 0,
    }


def test_vegas_comparison_skips_a_season_with_no_prior_training_data():
    rows = [_synthetic_game(i, 2020, float(i), float(i), float(i), float(i))
            for i in range(20)]
    result = vegas_comparison_report(rows, test_from_season=2020)
    # 2020 is the only season present -- there's no strictly-prior season to
    # train on, so it must not appear as a walk-forward test fold.
    assert result["walk_forward"] == []


def test_vegas_comparison_only_trains_on_strictly_prior_seasons():
    # hc/qb/market/actual are independent random draws, not scalar multiples
    # of each other or of a shared index -- avoids handing the two-feature
    # anchored fit (or the hc/qb z-score average) an accidentally collinear
    # or zero-variance synthetic dataset.
    import random
    rng = random.Random(2)

    def game(i, season):
        return _synthetic_game(
            i, season, hc=rng.uniform(-50, 50), qb=rng.uniform(-50, 50),
            market=rng.uniform(-10, 10), actual=rng.uniform(-30, 30))
    rows = [game(i, 2020) for i in range(20)] + [game(100 + i, 2021) for i in range(20)]
    result = vegas_comparison_report(rows, test_from_season=2021)
    assert len(result["walk_forward"]) == 1
    fold = result["walk_forward"][0]
    assert fold["season"] == 2021
    assert fold["train_games"] == 20  # only 2020's games, none of 2021's own
    assert fold["test_games"] == 20


def test_magnitude_report_buckets_are_monotonic_for_a_clean_relationship():
    # A dataset engineered so bigger signal really does mean bigger margin --
    # the bucket means should come out in non-decreasing order.
    rows = []
    for i in range(50):
        hc = float(i)
        qb = float(i)
        rows.append({"game_id": i, "hc_diff": hc, "qb_diff": qb,
                     "actual_margin": hc + qb, "home_won": (hc + qb) > 0})
    result = magnitude_report(rows, n_buckets=5)
    means = [b["mean_actual_margin"] for b in result["buckets_low_to_high_combined_signal"]]
    assert means == sorted(means)


def _directional_dataset(n_per_season=60, seed=7):
    import random
    rng = random.Random(seed)
    rows = []
    for season in (2020, 2021, 2022):
        for i in range(n_per_season):
            rows.append(_synthetic_game(
                f"{season}-{i}", season,
                hc=rng.uniform(-50, 50), qb=rng.uniform(-50, 50),
                market=rng.uniform(-14, 14), actual=rng.uniform(-40, 40)))
    return rows


def test_directional_edge_report_buckets_are_exhaustive_and_consistent():
    result = directional_edge_report(_directional_dataset(), test_from_season=2021)
    # Every scored game falls into exactly one closeness bucket.
    total_in_buckets = sum(b["n"] for b in result["by_market_closeness"].values())
    assert total_in_buckets == result["n_with_a_real_disagreement"]
    assert set(result["by_market_closeness"]) == {label for label, _ in SPREAD_CLOSENESS_BUCKETS}
    for bucket in result["by_market_closeness"].values():
        if bucket["n"] == 0:
            continue
        assert bucket["wins"] + bucket["losses"] + bucket["pushes"] == bucket["n"]


def test_upset_spots_reports_both_naive_and_bucket_matched_views():
    result = directional_edge_report(_directional_dataset(), test_from_season=2021)
    spots = result["upset_spots"]
    assert "naive_pooled_flip_call_upset_rate" in spots
    assert "bucket_matched_by_market_closeness" in spots
    matched = spots["bucket_matched_by_market_closeness"]
    assert set(matched) == {label for label, _ in SPREAD_CLOSENESS_BUCKETS}
    for bucket in matched.values():
        assert "baseline_upset_rate" in bucket
        assert "flip_call_upset_rate" in bucket
        # Every flip call in a bucket is also one of that bucket's games.
        assert bucket["n_flip_calls"] <= bucket["n_games_in_bucket"]
