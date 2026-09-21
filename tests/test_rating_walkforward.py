from sports_aggregator.cfb.rating_walkforward import walk_forward_combined_scores


def test_walk_forward_normalization_uses_only_prior_seasons():
    rows = [
        {"game_id": 1, "season": 2020, "hc_diff": -1.0, "qb_diff": -2.0},
        {"game_id": 2, "season": 2020, "hc_diff": 1.0, "qb_diff": 2.0},
        {"game_id": 3, "season": 2021, "hc_diff": 0.0, "qb_diff": 0.0},
        # Extreme 2021 target row must not alter 2021 scale.
        {"game_id": 4, "season": 2021, "hc_diff": 100.0, "qb_diff": 200.0},
    ]
    scores, diagnostics = walk_forward_combined_scores(
        rows, minimum_training_rows=2
    )
    assert 1 not in scores and 2 not in scores
    assert scores[3] == 0.0
    assert scores[4] > 50.0
    assert diagnostics["by_target_season"]["2021"]["training_rows"] == 2


def test_future_season_does_not_change_earlier_target_score():
    base = [
        {"game_id": 1, "season": 2020, "hc_diff": -1.0, "qb_diff": -2.0},
        {"game_id": 2, "season": 2020, "hc_diff": 1.0, "qb_diff": 2.0},
        {"game_id": 3, "season": 2021, "hc_diff": 0.5, "qb_diff": 1.0},
    ]
    expanded = base + [
        {"game_id": 4, "season": 2022, "hc_diff": -999.0, "qb_diff": 999.0}
    ]
    score_a, _ = walk_forward_combined_scores(base, minimum_training_rows=2)
    score_b, _ = walk_forward_combined_scores(expanded, minimum_training_rows=2)
    assert score_a[3] == score_b[3]
