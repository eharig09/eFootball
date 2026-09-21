"""Tests for the weather-vs-market-movement bucketing and stat math."""
from __future__ import annotations

from sports_aggregator.cfb.weather_market_impact import (
    PRECIP_BUCKETS, TEMP_BUCKETS, WIND_BUCKETS, _bucket_stats, _dimension, _extremes,
)


def _row(game_id, wind=5.0, precip=0.0, temp=60.0, open_total=50.0, close_total=50.0, actual_total=50.0):
    return {
        "game_id": game_id, "season": 2024, "week": 1,
        "home_team": "Home", "away_team": "Away",
        "sustained_wind": wind, "precipitation_amount": precip, "temperature": temp,
        "open_total": open_total, "close_total": close_total, "actual_total": actual_total,
        "line_movement": close_total - open_total,
        "actual_vs_open": actual_total - open_total,
        "actual_vs_close": actual_total - close_total,
        "weather_source": "open-meteo-archive",
    }


def test_bucket_stats_on_empty_rows_is_zero_not_a_crash():
    assert _bucket_stats([]) == {"n": 0}


def test_bucket_stats_signs_match_documented_meaning():
    rows = [
        _row(1, open_total=50, close_total=46, actual_total=40),  # moved and finished under
        _row(2, open_total=50, close_total=54, actual_total=60),  # moved and finished over
    ]
    stats = _bucket_stats(rows)
    assert stats["n"] == 2
    assert stats["pct_line_moved_toward_under"] == 0.5
    assert stats["pct_line_moved_toward_over"] == 0.5
    assert stats["pct_finished_under_open"] == 0.5
    assert stats["pct_finished_over_open"] == 0.5


def test_wind_buckets_are_mutually_exclusive_and_exhaustive():
    rows = [_row(i, wind=w) for i, w in enumerate([5, 12, 17, 22, 30])]
    grouped = _dimension(rows, "sustained_wind", WIND_BUCKETS)
    total = sum(v["n"] for v in grouped.values())
    assert total == len(rows)
    assert grouped["<10"]["n"] == 1
    assert grouped["25+"]["n"] == 1


def test_rows_missing_the_dimension_value_are_excluded_not_zero_filled():
    rows = [_row(1, wind=5.0), {**_row(2), "sustained_wind": None}]
    grouped = _dimension(rows, "sustained_wind", WIND_BUCKETS)
    assert sum(v["n"] for v in grouped.values()) == 1


def test_precip_and_temp_buckets_cover_realistic_ranges():
    precip_rows = [_row(i, precip=p) for i, p in enumerate([0.0, 0.05, 0.15, 0.5])]
    grouped = _dimension(precip_rows, "precipitation_amount", PRECIP_BUCKETS)
    assert sum(v["n"] for v in grouped.values()) == 4

    temp_rows = [_row(i, temp=t) for i, t in enumerate([10, 25, 40, 70, 90, 100])]
    grouped = _dimension(temp_rows, "temperature", TEMP_BUCKETS)
    assert sum(v["n"] for v in grouped.values()) == 6
    assert grouped["extreme cold (<20F)"]["n"] == 1
    assert grouped["extreme heat (95F+)"]["n"] == 1


def test_extremes_ranks_and_truncates():
    rows = [_row(i, wind=w) for i, w in enumerate([5, 30, 15, 25, 10])]
    top = _extremes(rows, "sustained_wind", top=2, reverse=True)
    assert [r["sustained_wind"] for r in top] == [30, 25]

    coldest = _extremes(rows, "sustained_wind", top=2, reverse=False)
    assert [r["sustained_wind"] for r in coldest] == [5, 10]
