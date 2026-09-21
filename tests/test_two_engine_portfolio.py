import pytest

from sports_aggregator.cfb.two_engine_portfolio import (
    _partition,
    _summary,
)


def test_partition_separates_a_only_b_only_agreement_and_conflict():
    a = {
        1: {"game_id": 1, "season": 2024, "selected_side": "home", "engine_a_residual": 3.0},
        2: {"game_id": 2, "season": 2024, "selected_side": "away", "engine_a_residual": 4.0},
        3: {"game_id": 3, "season": 2024, "selected_side": "home", "engine_a_residual": -2.0},
    }
    b = {
        2: {"game_id": 2, "season": 2024, "selected_side": "away", "engine_b_residual": 4.0, "engine_b_policies": ["p"]},
        3: {"game_id": 3, "season": 2024, "selected_side": "away", "engine_b_residual": 2.0, "engine_b_policies": ["p"]},
        4: {"game_id": 4, "season": 2024, "selected_side": "home", "engine_b_residual": 5.0, "engine_b_policies": ["p"]},
    }
    parts = _partition(a, b)
    assert [r["game_id"] for r in parts["engine_a_only"]] == [1]
    assert [r["game_id"] for r in parts["engine_b_only"]] == [4]
    assert [r["game_id"] for r in parts["agreement"]] == [2]
    assert [r["game_id"] for r in parts["conflict"]] == [3]


def test_summary_counts_positive_residual_as_win():
    rows = [
        {"portfolio_residual": 4.0},
        {"portfolio_residual": -1.0},
        {"portfolio_residual": 2.0},
    ]
    result = _summary(rows)
    assert result["n"] == 3
    assert result["wins"] == 2
    assert result["hit_rate"] == 0.6667
    assert result["mean_aligned_residual"] == 1.667
