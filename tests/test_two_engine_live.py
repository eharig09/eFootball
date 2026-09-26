from sports_aggregator.cfb.two_engine_live import (
    _efficiency_power_edge, _engine_a_no_pick_reason, _implication, _spread_bucket,
    engine_b_rules_plain_language, route_plain_language,
)


def test_spread_bucket_boundaries():
    assert _spread_bucket(2.5) == "<3"
    assert _spread_bucket(-3.0) == "3-6.5"
    assert _spread_bucket(6.5) == "3-6.5"
    assert _spread_bucket(7.0) == "7-13.5"
    assert _spread_bucket(-14.0) == "14+"


def test_engine_a_no_pick_reason_explains_three_of_four_spread_gap():
    reason = _engine_a_no_pick_reason({
        "agreement_count": 3,
        "old_agreement_count": 2,
        "hc_qb_elo_confirms": True,
        "spread_bucket": "7-13.5",
    }, margin_threshold_met=True)

    assert "3/4 signals agree" in reason
    assert "only has frozen routes at spreads below 7" in reason
    assert "7-13.5 bucket" in reason


def test_conflict_implication_does_not_force_a_side():
    text = _implication("conflict", None)
    assert "opposite sides" in text
    assert "not a selection" in text


def test_route_plain_language_returns_none_for_no_route():
    assert route_plain_language(None) is None
    assert route_plain_language("not_a_real_route") is None


def test_engine_b_plain_language_returns_none_with_no_rules():
    assert engine_b_rules_plain_language(None) is None
    assert engine_b_rules_plain_language([]) is None


def test_engine_b_plain_language_single_rule_names_its_own_clause_only():
    text = engine_b_rules_plain_language(["rebound_vs_momentum_qb_opposes"])
    assert "hot streak" in text
    assert "letdown risk" not in text


def test_engine_b_plain_language_both_rules_combine_into_one_sentence():
    text = engine_b_rules_plain_language(
        ["rebound_vs_momentum_qb_opposes", "rebound_vs_post_success_qb_opposes"])
    assert "hot streak" in text
    assert "letdown risk" in text
    assert text.count("Follow the rebounding side") == 1


def test_efficiency_power_edge_returns_none_with_missing_inputs():
    assert _efficiency_power_edge(None, 12.0, -3.0) is None
    assert _efficiency_power_edge({"home": {}, "away": {}}, 12.0, -3.0) is None
    projection = {
        "home": {"residual_points_per_drive": 2.0},
        "away": {"residual_points_per_drive": 1.8},
    }
    assert _efficiency_power_edge(projection, None, -3.0) is None
    assert _efficiency_power_edge(projection, 12.0, None) is None


def test_efficiency_power_edge_matches_the_documented_formula():
    # home_margin = (home_residual - away_residual) * league_drives + HFA_POINTS(2.5)
    projection = {
        "home": {"residual_points_per_drive": 2.2},
        "away": {"residual_points_per_drive": 1.8},
    }
    edge = _efficiency_power_edge(projection, 12.0, market_home_margin=1.0)
    expected_home_margin = (2.2 - 1.8) * 12.0 + 2.5
    assert abs(edge - (expected_home_margin - 1.0)) < 1e-9
