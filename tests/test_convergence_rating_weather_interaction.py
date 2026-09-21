"""Tests for the convergence x HC/QB Elo x weather interaction report's
pure bucketing/summary logic."""
from __future__ import annotations

from sports_aggregator.cfb.convergence_rating_weather_interaction import (
    _hit_rate, _weather_bucket_label, combined_filter_report, elo_agreement_report,
)
from sports_aggregator.cfb.weather_market_impact import WIND_BUCKETS


def _row(game_id, spread_bucket, hit, aligned_residual, elo_aligned, wind=None):
    return {
        "game_id": game_id, "spread_bucket": spread_bucket, "hit": hit,
        "aligned_residual": aligned_residual, "elo_aligned": elo_aligned,
        "sustained_wind": wind,
    }


def test_hit_rate_empty_is_zero_not_a_crash():
    assert _hit_rate([]) == {"n": 0}


def test_hit_rate_basic_counts():
    rows = [
        _row(1, "3-6.5", True, 5.0, 1.0),
        _row(2, "3-6.5", False, -3.0, -1.0),
        _row(3, "3-6.5", True, 2.0, 0.5),
    ]
    result = _hit_rate(rows)
    assert result["n"] == 3
    assert result["hits"] == 2
    assert result["hit_rate"] == round(2 / 3, 4)


def test_weather_bucket_label_finds_matching_bucket():
    assert _weather_bucket_label({"sustained_wind": 18.0}, "sustained_wind", WIND_BUCKETS) == "15-19.9"
    assert _weather_bucket_label({"sustained_wind": None}, "sustained_wind", WIND_BUCKETS) is None


def test_elo_agreement_report_splits_by_sign_of_elo_aligned():
    rows = [
        _row(1, "3-6.5", True, 5.0, 2.0),   # agrees, hit
        _row(2, "3-6.5", False, -3.0, 1.5),  # agrees, miss
        _row(3, "3-6.5", True, 2.0, -0.5),  # disagrees, hit
        _row(4, "<3", True, 1.0, 3.0),      # different bucket
    ]
    result = elo_agreement_report(rows)
    assert result["n_with_elo_signal"] == 4
    overall = result["overall"]
    assert overall["elo_agrees_with_pick"]["n"] == 3
    assert overall["elo_disagrees_with_pick"]["n"] == 1
    bucket = result["by_spread_bucket"]["3-6.5"]
    assert bucket["elo_agrees_with_pick"]["n"] == 2
    assert bucket["elo_agrees_with_pick"]["hits"] == 1
    assert bucket["elo_disagrees_with_pick"]["n"] == 1


def test_elo_agreement_report_excludes_rows_without_an_elo_signal():
    rows = [_row(1, "3-6.5", True, 5.0, None), _row(2, "3-6.5", True, 2.0, 1.0)]
    result = elo_agreement_report(rows)
    assert result["n_with_elo_signal"] == 1


def test_combined_filter_report_only_looks_at_3_6_5():
    rows = [
        _row(1, "3-6.5", True, 5.0, 1.0),
        _row(2, "14+", True, 5.0, 1.0),  # excluded -- wrong bucket
        _row(3, "3-6.5", False, -2.0, -1.0),
    ]
    result = combined_filter_report(rows)
    assert result["spread_3_6_5_baseline"]["n"] == 2
    assert result["spread_3_6_5_filtered_to_elo_agreement"]["n"] == 1
    assert result["spread_3_6_5_filtered_to_elo_disagreement"]["n"] == 1
