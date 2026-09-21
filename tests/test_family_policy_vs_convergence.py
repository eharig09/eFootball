from sports_aggregator.cfb.family_policy_vs_convergence import (
    _overlap,
    _summary,
)


def test_summary_uses_policy_aligned_residual():
    rows = [
        {"policy_aligned_residual": 4.0},
        {"policy_aligned_residual": -1.0},
        {"policy_aligned_residual": 3.0},
    ]
    result = _summary(rows)
    assert result["n"] == 3
    assert result["wins"] == 2
    assert result["hit_rate"] == 0.6667
    assert result["mean_aligned_residual"] == 2.0


def test_overlap_counts_unique_same_and_opposite_side():
    rows = [
        {"game_id": 1, "policy_selected_side": "home", "policy_aligned_residual": 3.0},
        {"game_id": 2, "policy_selected_side": "away", "policy_aligned_residual": -2.0},
        {"game_id": 3, "policy_selected_side": "home", "policy_aligned_residual": 1.0},
    ]
    frozen = {
        1: {"route_name": "a", "route_selected_side": "home"},
        2: {"route_name": "b", "route_selected_side": "home"},
    }
    result = _overlap(rows, frozen)
    assert result["overlap_n"] == 2
    assert result["unique_added_n"] == 1
    assert result["same_selected_side_n"] == 1
    assert result["opposite_selected_side_n"] == 1
    assert result["overlap_by_frozen_route"] == {"a": 1, "b": 1}
