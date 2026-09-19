from sports_aggregator.cfb import narrative_shapes as ns


def test_line_elo_observation_moves_toward_market_strength_gap():
    home, away = ns._market_line_elo_observation(
        1500.0, 1500.0, 10.0,
        home_field_points=2.5,
        learning_rate=0.35,
    )
    assert home > 1500.0
    assert away < 1500.0
    assert round(home - away, 3) == 65.625


def test_elo_expected_margin_includes_home_field():
    assert ns._expected_margin_from_elo(
        1600.0, 1500.0, is_home=True, home_field_points=2.5) == 6.5
    assert ns._expected_margin_from_elo(
        1600.0, 1500.0, is_home=False, home_field_points=2.5) == 1.5


def test_tag_names_returns_only_active_flags():
    assert ns._tag_names({
        "statement_win": 1,
        "market_chase": 0,
        "lookahead_candidate": 1,
    }) == ["statement_win", "lookahead_candidate"]


def test_category_summary_uses_market_residual_and_cover():
    payload = ns._category_summary([
        {"market_margin_residual": 7.0, "covered": 1},
        {"market_margin_residual": -3.0, "covered": 0},
    ])
    assert payload["n"] == 2
    assert payload["mean_market_residual"] == 2.0
    assert payload["cover_rate"] == 0.5


from sports_aggregator.cfb import narrative_shapes_v2 as nsv2


def test_v2_difference_requires_both_sides():
    assert nsv2._difference(7.5, 2.0) == 5.5
    assert nsv2._difference(None, 2.0) is None


def test_v2_ridge_can_fit_simple_residual_signal():
    rows = []
    for value in (-2.0, -1.0, 0.0, 1.0, 2.0):
        row = {key: 0.0 for key in nsv2.CONTINUOUS_FEATURES}
        row["previous_market_surprise_diff"] = value
        row["market_margin_residual"] = 2.0 * value
        rows.append(row)
    model = nsv2._fit_ridge(rows, l2=0.5)
    assert model is not None
    probe = {key: 0.0 for key in nsv2.CONTINUOUS_FEATURES}
    probe["previous_market_surprise_diff"] = 1.5
    assert nsv2._predict(model, probe) > 0.0


def test_v2_model_metrics_reports_zero_error_for_perfect_predictions():
    rows = []
    for value in (-1.0, 0.0, 1.0):
        row = {key: 0.0 for key in nsv2.CONTINUOUS_FEATURES}
        row["previous_market_surprise_diff"] = value
        row["market_margin_residual"] = value
        rows.append(row)
    model = nsv2._fit_ridge(rows, l2=0.0)
    assert model is not None
    metrics = nsv2._model_metrics(model, rows)
    assert metrics["n"] == 3
    assert metrics["mae"] is not None
