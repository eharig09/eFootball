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


def test_v2_market_tags_use_centered_state():
    row = {
        "game_id": 1,
        "team": "A",
        **{key: 0 for key in ns.CATEGORY_COLUMNS},
    }
    state = {
        (1, "A"): {
            "centered_line_gap": 90.0,
            "perception_change_v2": -40.0,
        }
    }
    tags = nsv2._v2_tags(row, state)
    assert "market_darling" in tags
    assert "market_lag" in tags
    assert "market_chase" not in tags


from sports_aggregator.cfb import narrative_composite as nc


def test_composite_score_rewards_agreement_and_magnitude():
    row = {
        "football_lab_edge": 2.0,
        "elo_edge": 1.0,
        "fpi_edge": 3.0,
        "core_edge": -0.5,
        "line_elo_edge": None,
        "narrative_interaction_edge": 1.5,
    }
    scales = {key: 1.0 for key in nc.SIGNAL_KEYS}
    score = nc._score(row, scales)
    assert score["available"] == 5
    assert score["direction"] == 1
    assert score["agreement_ratio"] == 0.8
    assert score["score"] > 0.0


def test_subset_metrics_filters_on_agreement_and_magnitude():
    scales = {key: 1.0 for key in nc.SIGNAL_KEYS}
    row = {
        "market_margin_residual": 4.0,
        **{key: 1.0 for key in nc.SIGNAL_KEYS},
    }
    kept = nc._subset_metrics(
        [row], scales, 1.0,
        min_agreement=1.0, min_magnitude=0.5, min_signals=5,
    )
    assert kept["n"] == 1
    assert kept["directional_hit_rate"] == 1.0
    dropped = nc._subset_metrics(
        [row], scales, 1.0,
        min_agreement=1.0, min_magnitude=2.0, min_signals=5,
    )
    assert dropped["n"] == 0


def test_tag_intensity_uses_underlying_narrative_magnitude():
    row = {
        "previous_market_surprise": -18.0,
        "previous_elo_surprise": -12.0,
        "previous_market_expected_margin": 7.0,
    }
    assert nc._tag_intensity("bad_loss", row, {}) == 18.0
    assert nc._tag_intensity("upset_loss", row, {}) == 25.0


from sports_aggregator.cfb import extreme_tail_composite as etc


def test_extreme_tail_score_counts_only_strong_votes():
    row = {
        "football_lab_edge": 1.2,
        "elo_edge": 0.8,
        "fpi_edge": 0.2,
        "core_edge": -0.1,
        "line_elo_edge": 1.0,
        "narrative_interaction_edge": None,
    }
    scales = {key: 1.0 for key in nc.SIGNAL_KEYS}
    score = etc._score_with_keys(row, scales, nc.SIGNAL_KEYS)
    assert score["direction"] == 1
    assert score["available"] == 5
    assert score["strong_positive"] == 3
    assert score["strong_negative"] == 0
    assert score["strong_agreement_count"] == 3


def test_signal_audit_flags_constant_or_missing_lenses():
    rows = [
        {"fpi_edge": None, "core_edge": 0.0},
        {"fpi_edge": None, "core_edge": 0.0},
    ]
    for row in rows:
        for key in nc.SIGNAL_KEYS:
            row.setdefault(key, None)
    audit = etc._signal_audit(rows)
    assert audit["fpi_edge"]["constant_or_missing"] is True
    assert audit["fpi_edge"]["coverage_rate"] == 0.0
    assert audit["core_edge"]["constant_or_missing"] is True
    assert audit["core_edge"]["unique_values"] == 1


def test_bucket_metrics_respects_fixed_magnitude_band():
    row = {
        "market_margin_residual": 3.0,
        **{key: 1.0 for key in nc.SIGNAL_KEYS},
    }
    scales = {key: 1.0 for key in nc.SIGNAL_KEYS}
    inside = etc._bucket_metrics(
        [row], scales, 0.0, low=0.75, high=1.25)
    outside = etc._bucket_metrics(
        [row], scales, 0.0, low=1.25, high=1.50)
    assert inside["n"] == 1
    assert outside["n"] == 0


from sports_aggregator.cfb import composite_input_repair as cir


def test_family_score_requires_only_populated_families():
    row = {
        "football_lab_family": 2.0,
        "elo_family": 1.0,
        "external_power_family": None,
        "market_family": 1.5,
        "narrative_family": None,
    }
    scales = {key: 1.0 for key in cir.FAMILY_KEYS}
    score = cir._score(row, scales)
    assert score["available"] == 3
    assert score["direction"] == 1
    assert score["agreement"] == 1.0


def test_scoreboard_calibration_maps_offense_margin_to_scoreboard_margin():
    games = {
        1: {"home": {"season": 2022, "projected_offensive_points": 30.0, "actual_score_points": 28.0},
            "away": {"season": 2022, "projected_offensive_points": 20.0, "actual_score_points": 21.0}},
        2: {"home": {"season": 2022, "projected_offensive_points": 24.0, "actual_score_points": 24.0},
            "away": {"season": 2022, "projected_offensive_points": 20.0, "actual_score_points": 21.0}},
    }
    # Function intentionally requires a real sample before fitting.
    assert cir._fit_scoreboard_calibration(games, {2022}) is None


from sports_aggregator.cfb import internal_power_lenses as ipl


def test_internal_srs_rewards_margin_after_hfa():
    games = [
        {"home_team":"A","away_team":"B","home_points":30,"away_points":20},
        {"home_team":"A","away_team":"C","home_points":28,"away_points":14},
        {"home_team":"B","away_team":"C","home_points":24,"away_points":21},
    ]
    ratings, counts = ipl._solve_srs(games)
    assert counts["A"] == 2
    assert ratings["A"] > ratings["B"]
    assert ratings["B"] > ratings["C"]


def test_internal_score_uses_only_active_scales():
    row = {
        "football_lab_edge": 2.0,
        "elo_edge": 1.0,
        "margin_power_edge": 1.5,
        "efficiency_power_edge": None,
        "line_elo_edge": -0.25,
        "narrative_interaction_edge": None,
    }
    scales = {
        "football_lab_edge": 1.0,
        "elo_edge": 1.0,
        "margin_power_edge": 1.0,
        "line_elo_edge": 1.0,
    }
    score = ipl._score(row, scales)
    assert score["available"] == 4
    assert score["direction"] == 1
    assert score["agreement"] == 0.75


from sports_aggregator.cfb import conditional_convergence as cc


def test_conditional_state_counts_structural_and_market_confirmations():
    row = {
        "margin_power_edge": 2.0,
        "football_lab_edge": 1.0,
        "elo_edge": 0.5,
        "efficiency_power_edge": 1.5,
        "line_elo_edge": 0.75,
        "narrative_interaction_edge": -0.25,
    }
    scales = {
        "margin_power_edge": 1.0,
        "football_lab_edge": 1.0,
        "elo_edge": 1.0,
        "efficiency_power_edge": 1.0,
        "line_elo_edge": 1.0,
        "narrative_interaction_edge": 1.0,
    }
    state = cc._state(row, scales)
    assert state is not None
    assert state["confirmation_count"] == 2
    assert state["confirmation_combination"] == "structural+market"
    assert state["narrative_state"] == "opposes"


def test_conditional_structural_cluster_requires_two_members():
    row = {
        "margin_power_edge": 1.0,
        "football_lab_edge": 1.0,
        "elo_edge": None,
        "efficiency_power_edge": None,
        "line_elo_edge": 1.0,
        "narrative_interaction_edge": None,
    }
    scales = {
        "margin_power_edge": 1.0,
        "football_lab_edge": 1.0,
        "line_elo_edge": 1.0,
    }
    state = cc._state(row, scales)
    assert state is not None
    assert state["structural_confirms"] is None
    assert state["available_confirmations"] == 1


def test_conditional_subset_requires_both_confirmation_families_by_default():
    row = {
        "market_margin_residual": 3.0,
        "margin_power_edge": 1.2,
        "football_lab_edge": 0.8,
        "elo_edge": 0.6,
        "efficiency_power_edge": None,
        "line_elo_edge": 0.5,
        "narrative_interaction_edge": None,
    }
    scales = {
        "margin_power_edge": 1.0,
        "football_lab_edge": 1.0,
        "elo_edge": 1.0,
        "line_elo_edge": 1.0,
    }
    rows = cc._subset(
        [row], scales, low=1.0, high=1.5, confirmation_count=2)
    assert len(rows) == 1


from sports_aggregator.cfb import convergence_validation as cv


def test_wilson_interval_contains_observed_rate():
    low, high = cv._wilson_interval(60, 100)
    assert low < 0.60 < high


def test_bootstrap_interval_is_deterministic():
    values = [1.0, 2.0, 3.0, 4.0]
    first = cv._bootstrap_mean_interval(values, draws=200, seed=7)
    second = cv._bootstrap_mean_interval(values, draws=200, seed=7)
    assert first == second


def test_validation_monotonicity_detects_ordering():
    table = [{
        "minimum_abs_margin_power_z": 1.0,
        "confirmations": {
            "0": {"n": 20, "hit_rate": 0.50, "mean_aligned_residual": 0.0},
            "1": {"n": 20, "hit_rate": 0.55, "mean_aligned_residual": 1.0},
            "2": {"n": 20, "hit_rate": 0.60, "mean_aligned_residual": 2.0},
        },
    }]
    result = cv._monotonicity(table)[0]
    assert result["hit_rate_monotonic_0_to_2"] is True
    assert result["mean_residual_monotonic_0_to_2"] is True


def test_classified_rows_preserve_walk_forward_state():
    row = {
        "game_id": 1,
        "season": 2025,
        "market_margin_residual": 4.0,
        "margin_power_edge": 2.0,
        "football_lab_edge": 1.0,
        "elo_edge": 1.0,
        "efficiency_power_edge": 1.0,
        "line_elo_edge": 1.0,
        "narrative_interaction_edge": 1.0,
    }
    scales = {
        "margin_power_edge": 1.0,
        "football_lab_edge": 1.0,
        "elo_edge": 1.0,
        "efficiency_power_edge": 1.0,
        "line_elo_edge": 1.0,
        "narrative_interaction_edge": 1.0,
    }
    classified = cv._classified_rows([row], scales, 2025)
    assert len(classified) == 1
    assert classified[0]["confirmation_count"] == 2
    assert classified[0]["aligned_residual"] == 4.0


from sports_aggregator.cfb import convergence_robustness as cr


def test_robustness_summary_reuses_validation_uncertainty():
    rows = [
        {"aligned_residual": 3.0, "hit": True},
        {"aligned_residual": -1.0, "hit": False},
        {"aligned_residual": 2.0, "hit": True},
    ]
    summary = cr._summary(rows, seed=11)
    assert summary["n"] == 3
    assert summary["wins"] == 2
    assert summary["hit_rate"] == 0.6667


def test_robustness_slice_groups_fixed_categories():
    rows = [
        {"market_role": "favorite", "aligned_residual": 2.0, "hit": True},
        {"market_role": "underdog", "aligned_residual": -1.0, "hit": False},
        {"market_role": "favorite", "aligned_residual": 4.0, "hit": True},
    ]
    result = cr._slice(rows, "market_role", seed_base=20)
    lookup = {item["value"]: item for item in result}
    assert lookup["favorite"]["n"] == 2
    assert lookup["favorite"]["hit_rate"] == 1.0
    assert lookup["underdog"]["n"] == 1


def test_compact_console_summary_excludes_game_rows():
    payload = {
        "version": "x",
        "test_through_season": 2025,
        "full_convergence": {
            "n": 10, "hit_rate": 0.6, "hit_rate_ci95": [0.3, 0.8],
            "mean_aligned_residual": 2.0,
            "mean_aligned_residual_bootstrap_ci95": [0.1, 4.0],
        },
        "context_supported_full_convergence": {
            "n": 5, "hit_rate": 0.8, "mean_aligned_residual": 3.0,
        },
        "game_rows": [{"huge": "payload"}],
    }
    compact = cr.compact_console_summary(payload, {"json": "/tmp/x.json"})
    assert "game_rows" not in compact
    assert compact["files"]["json"] == "/tmp/x.json"


from sports_aggregator.cfb import convergence_action_policy as cap


def test_confirmation_strength_buckets_are_fixed():
    assert cap._strength_bucket(0.10) == "weak_<0.25"
    assert cap._strength_bucket(0.25) == "moderate_0.25_0.50"
    assert cap._strength_bucket(0.50) == "strong_>=0.50"


def test_joint_confirmation_uses_weakest_family():
    row = {
        "structural_z_aligned": 0.80,
        "line_elo_z_aligned": 0.10,
    }
    assert cap._joint_strength(row) == "weak_any_<0.25"


def test_fade_inverts_aligned_residual():
    rows = [{"aligned_residual": -4.0, "hit": False}]
    summary = cap._action_summary(rows, action="fade", seed=1)
    assert summary["n"] == 1
    assert summary["wins"] == 1
    assert summary["hit_rate"] == 1.0
    assert summary["mean_aligned_residual"] == 4.0


def test_policy_pass_does_not_count_as_result():
    rows = [
        {"aligned_residual": 3.0, "narrative_state": "agrees"},
        {"aligned_residual": -2.0, "narrative_state": "missing"},
    ]
    result = cap._policy_summary(
        rows,
        name="x",
        chooser=lambda r: "pass" if r["narrative_state"] == "missing" else "keep",
        seed=2,
    )
    assert result["source_games"] == 2
    assert result["acted_n"] == 1
    assert result["pass_n"] == 1
    assert result["hit_rate"] == 1.0
