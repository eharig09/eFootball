"""Tests for the HC/QB Elo predictive-power stat functions."""
from __future__ import annotations

import math

from sports_aggregator.cfb.rating_predictive_power import (
    _ols, _partial_correlation, _pearson, _win_rate_when_favored, _zscore_diff,
    agreement_report, magnitude_report,
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
