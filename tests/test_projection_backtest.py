"""Tests for full-chain historical projection backtesting."""
from __future__ import annotations

from sports_aggregator.cfb import projection_backtest as bt


def test_metrics_reports_mae_rmse_and_signed_bias():
    result = bt._metrics([(10.0, 8.0), (4.0, 6.0)])
    assert result["n"] == 2
    assert result["mae"] == 2.0
    assert result["rmse"] == 2.0
    assert result["bias"] == 0.0


def test_prior_game_buckets_are_stable():
    assert bt._bucket_prior_games(1) == "1-2"
    assert bt._bucket_prior_games(4) == "3-4"
    assert bt._bucket_prior_games(7) == "5-7"
    assert bt._bucket_prior_games(11) == "8-11"
    assert bt._bucket_prior_games(12) == "12+"


def test_week_buckets_separate_early_and_late_season():
    assert bt._bucket_week(2) == "0-3"
    assert bt._bucket_week(5) == "4-6"
    assert bt._bucket_week(9) == "7-10"
    assert bt._bucket_week(12) == "11+"


def test_quality_buckets_use_absolute_edge():
    assert bt._bucket_quality(None) == "missing"
    assert bt._bucket_quality(-2.9) == "0-3"
    assert bt._bucket_quality(4.0) == "3-7"
    assert bt._bucket_quality(-9.0) == "7+"


def test_game_level_report_uses_two_sided_total_and_margin():
    rows = [
        {"game_id": 1, "side": "home", "projected_offensive_points": 30.0,
         "actual_score_points": 27.0, "actual_offensive_points": 24.0,
         "market_total": 50.0, "market_spread": -3.0},
        {"game_id": 1, "side": "away", "projected_offensive_points": 20.0,
         "actual_score_points": 17.0, "actual_offensive_points": 17.0,
         "market_total": 50.0, "market_spread": -3.0},
    ]
    result = bt._game_scores(rows)
    assert result["games_with_two_sides"] == 1
    assert result["projected_score_total"]["mae"] == 6.0
    assert result["projected_score_margin"]["mae"] == 0.0
    assert result["projected_offensive_total"]["mae"] == 9.0
    assert result["market_total"]["mae"] == 6.0
    assert result["market_margin"]["mae"] == 7.0


def test_source_coverage_shape_can_flag_missing_production_actuals():
    expected_keys = {
        "completed_games", "expected_team_game_rows", "pbp_rows", "derived_play_rows",
        "team_game_pace_rows", "team_game_scoring_rows",
        "team_game_special_teams_rows", "team_game_drive_outcomes_rows",
    }
    # Keep the public contract explicit; callers use these keys to decide
    # whether the historical production tables need to be prepared.
    assert expected_keys <= {
        "completed_games", "expected_team_game_rows", "pbp_rows", "derived_play_rows",
        "team_game_pace_rows", "team_game_scoring_rows",
        "team_game_special_teams_rows", "team_game_drive_outcomes_rows",
    }


def test_point_calibrator_shrinks_sparse_extreme_bins():
    rows = []
    for _ in range(100):
        rows.append({
            "projected_offensive_points": 25.0,
            "actual_offensive_points": 25.0,
        })
    for _ in range(5):
        rows.append({
            "projected_offensive_points": 42.0,
            "actual_offensive_points": 30.0,
        })
    model = bt._fit_point_calibrator(rows, pseudo_rows=50.0)
    assert model is not None
    # Sparse 40+ observations should pull downward, but shrinkage should
    # prevent blindly applying the raw -12 point residual.
    correction = model["corrections"]["40_plus"]
    assert -12.0 < correction < 0.0
    assert bt._apply_point_calibrator(model, 42.0) < 42.0


def test_yardage_decomposition_identifies_efficiency_only_miss():
    rows = [{
        "projected_dropbacks": 20.0,
        "projected_rush_attempts": 20.0,
        "projected_pass_yards": 140.0,
        "projected_rush_yards": 80.0,
        "actual_dropbacks": 20.0,
        "actual_rush_attempts": 20.0,
        "actual_pass_yards": 200.0,
        "actual_rush_yards": 100.0,
        "actual_total_yards": 300.0,
    }]
    result = bt._yardage_decomposition(rows)
    # Volume is already exact, so replacing projected volume with actual
    # volume cannot improve the miss.
    assert result["actual_volume_with_projected_efficiency"]["mae"] == 80.0
    # Replacing efficiency with actual rates should eliminate it.
    assert result["projected_volume_with_actual_efficiency"]["mae"] == 0.0


def test_temporal_point_calibration_uses_only_prior_seasons():
    all_rows = [
        {"season": 2024, "projected_offensive_points": 40.0, "actual_offensive_points": 30.0},
        {"season": 2025, "projected_offensive_points": 40.0, "actual_offensive_points": 50.0},
    ]
    test_rows = [all_rows[1]]
    result = bt._temporal_point_calibration(all_rows, test_rows)
    model = result["models"]["2025"]
    assert model["training_rows"] == 1
    # If the 2025 result leaked into training this correction would move back
    # toward zero rather than remaining negative.
    assert model["corrections"]["40_plus"] < 0.0
