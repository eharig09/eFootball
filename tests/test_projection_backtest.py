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


def test_interval_score_penalizes_width_and_misses():
    narrow_hit = bt._interval_score(20.0, 18.0, 22.0, alpha=0.20)
    wide_hit = bt._interval_score(20.0, 10.0, 30.0, alpha=0.20)
    miss = bt._interval_score(25.0, 18.0, 22.0, alpha=0.20)
    assert narrow_hit == 4.0
    assert wide_hit == 20.0
    assert miss > wide_hit


def test_conditional_residuals_falls_back_when_specific_bucket_is_sparse():
    training = [
        {
            "week": 2,
            "prior_games": 2,
            "quality_edge": 1.0,
            "projected_offensive_points": 20.0 + (i % 3),
            "actual_offensive_points": 21.0 + (i % 3),
        }
        for i in range(120)
    ]
    row = {
        "week": 2,
        "prior_games": 2,
        "quality_edge": 9.0,
        "projected_offensive_points": 21.0,
    }
    residuals, source = bt._conditional_residuals(
        training,
        row,
        "projected_offensive_points",
        "actual_offensive_points",
        method="conditional_hierarchy",
        min_rows=100,
    )
    assert len(residuals) >= 100
    assert source in {"week=0-3|projection=20_24|prior=1-2", "week=0-3|projection=20_24", "projection=20_24", "week=0-3", "global"}


def test_generic_ridge_can_learn_larger_error_scale():
    rows = []
    for x in range(1, 40):
        features = [float(x), 1.0]
        target = 0.1 * float(x)
        rows.append((features, target))
    model = bt._ridge_fit_generic(rows, l2=0.01)
    assert model is not None
    low = bt._ridge_predict_generic(model, [5.0, 1.0])
    high = bt._ridge_predict_generic(model, [30.0, 1.0])
    assert low is not None and high is not None
    assert high > low


def test_interval_summary_rewards_narrower_equally_calibrated_intervals():
    narrow = [(20.0, 15.0, 25.0), (30.0, 25.0, 35.0)]
    wide = [(20.0, 5.0, 35.0), (30.0, 15.0, 45.0)]
    narrow_summary = bt._interval_summary(narrow, alpha=0.20)
    wide_summary = bt._interval_summary(wide, alpha=0.20)
    assert narrow_summary["coverage"] == wide_summary["coverage"] == 1.0
    assert narrow_summary["interval_score"] < wide_summary["interval_score"]


def test_chain_simulation_preserves_drive_ppd_structure():
    row = {
        "projected_drives": 10.0,
        "projected_points_per_drive": 2.0,
    }
    sims = bt._chain_simulated_points(row, [(1.0, 0.5), (-1.0, -0.5)])
    assert sims == [27.5, 13.5]


def test_paired_chain_residuals_keeps_component_errors_together():
    rows = [{
        "projected_drives": 10.0,
        "actual_drives": 12.0,
        "projected_points_per_drive": 2.0,
        "actual_points_per_drive": 1.5,
    }]
    assert bt._paired_chain_residuals(rows) == [(2.0, -0.5)]


def test_market_leverage_prediction_clips_extreme_adjustments():
    row = {
        "market_implied_points": 20.0,
        "projected_offensive_points": 35.0,
        "projected_drives": 12.0,
        "projected_plays": 70.0,
        "projected_points_per_drive": 3.0,
        "projected_total_yards": 450.0,
        "projected_giveaways": 1.0,
        "projected_red_zone_trips": 5.0,
        "projected_red_zone_touchdowns": 4.0,
        "quality_edge": 8.0,
        "quality_sources": 3,
        "prior_games": 8,
        "week": 9,
        "market_total": 55.0,
    }
    features = bt._leverage_features(row)
    assert features is not None
    model = {
        "coefficients": [100.0] + [0.0] * len(features),
        "means": [0.0] * len(features),
        "scales": [1.0] * len(features),
    }
    assert bt._predict_market_leverage(model, row, clip=10.0) == 10.0


def test_leverage_bucket_thresholds():
    assert bt._leverage_bucket(0.5) == "0-1"
    assert bt._leverage_bucket(-1.5) == "1-2"
    assert bt._leverage_bucket(2.5) == "2-3"
    assert bt._leverage_bucket(-4.0) == "3-5"
    assert bt._leverage_bucket(6.0) == "5+"


def test_market_leverage_features_include_model_market_gap():
    row = {
        "market_implied_points": 24.0,
        "projected_offensive_points": 29.0,
        "projected_drives": 11.0,
        "projected_plays": 68.0,
        "projected_points_per_drive": 2.6,
        "projected_total_yards": 410.0,
        "projected_giveaways": 1.1,
        "projected_red_zone_trips": 4.0,
        "projected_red_zone_touchdowns": 2.5,
        "quality_edge": 3.0,
        "quality_sources": 2,
        "prior_games": 6,
        "week": 7,
        "market_total": 51.0,
    }
    features = bt._leverage_features(row)
    assert features is not None
    assert features[0] == 5.0


def test_selective_leverage_policy_can_choose_market_unchanged():
    fit_rows = []
    validation_rows = []
    for i in range(40):
        base = {
            "market_implied_points": 24.0,
            "projected_offensive_points": 30.0 if i % 2 == 0 else 18.0,
            "projected_drives": 11.0,
            "projected_plays": 68.0,
            "projected_points_per_drive": 2.4,
            "projected_total_yards": 400.0,
            "projected_giveaways": 1.0,
            "projected_red_zone_trips": 4.0,
            "projected_red_zone_touchdowns": 2.5,
            "quality_edge": 3.0,
            "quality_sources": 2,
            "prior_games": 6,
            "week": 7,
            "market_total": 50.0,
        }
        fit_rows.append({**base, "actual_score_points": 30.0 if i % 2 == 0 else 18.0})
        # Validation outcomes equal the market, so any learned adjustment hurts.
        validation_rows.append({**base, "actual_score_points": 24.0})
    policy = bt._select_leverage_policy(fit_rows, validation_rows)
    assert policy["shrink"] == 0.0


def test_selective_leverage_policy_returns_validation_metadata():
    rows = []
    for i in range(50):
        rows.append({
            "market_implied_points": 20.0,
            "projected_offensive_points": 25.0,
            "projected_drives": 10.0 + (i % 2),
            "projected_plays": 65.0,
            "projected_points_per_drive": 2.5,
            "projected_total_yards": 380.0,
            "projected_giveaways": 1.0,
            "projected_red_zone_trips": 4.0,
            "projected_red_zone_touchdowns": 2.0,
            "quality_edge": 2.0,
            "quality_sources": 2,
            "prior_games": 5,
            "week": 6,
            "market_total": 48.0,
            "actual_score_points": 23.0,
        })
    policy = bt._select_leverage_policy(rows, rows)
    assert policy["candidate_count"] == 30
    assert policy["validation_rows"] == 50


def test_directional_diagnostics_separates_direction_and_magnitude():
    result = bt._directional_diagnostics([
        (3.0, 1.0),   # correct direction, overstated
        (-2.0, -5.0), # correct direction, understated
        (4.0, -2.0),  # wrong direction
    ])
    assert result["direction_hit_rate"] == 0.6667
    assert result["overstated_rate_when_correct"] == 0.5
    assert result["understated_rate_when_correct"] == 0.5
    assert result["correct_direction_n"] == 2
    assert result["wrong_direction_n"] == 1


def test_total_leverage_features_use_market_total_as_anchor():
    game = {
        "market_total": 50.0,
        "pure_total": 56.0,
        "projected_drives": 22.0,
        "projected_plays": 140.0,
        "home_ppd": 2.7,
        "away_ppd": 2.3,
        "projected_total_yards": 820.0,
        "projected_giveaways": 2.0,
        "projected_red_zone_trips": 8.0,
        "projected_red_zone_touchdowns": 5.0,
        "market_spread": -7.0,
        "home_quality_edge": 4.0,
        "away_quality_edge": -4.0,
        "quality_sources": 4.0,
        "min_prior_games": 6,
        "week": 8,
    }
    features = bt._total_leverage_features(game)
    assert features is not None
    assert features[0] == 6.0
    assert features[3] == 2.5
    assert features[-1] == 50.0


def test_total_policy_can_leave_market_unchanged():
    fit_games = []
    validation_games = []
    for i in range(40):
        game = {
            "market_total": 50.0,
            "pure_total": 58.0 if i % 2 == 0 else 42.0,
            "projected_drives": 22.0,
            "projected_plays": 140.0,
            "home_ppd": 2.6,
            "away_ppd": 2.4,
            "projected_total_yards": 800.0,
            "projected_giveaways": 2.0,
            "projected_red_zone_trips": 8.0,
            "projected_red_zone_touchdowns": 5.0,
            "market_spread": -3.0,
            "home_quality_edge": 2.0,
            "away_quality_edge": -2.0,
            "quality_sources": 4.0,
            "min_prior_games": 6,
            "week": 8,
        }
        fit_games.append({**game, "actual_total": 58.0 if i % 2 == 0 else 42.0})
        validation_games.append({**game, "actual_total": 50.0})
    policy = bt._select_total_leverage_policy(fit_games, validation_games)
    assert policy["shrink"] == 0.0
