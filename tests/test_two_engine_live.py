from sports_aggregator.cfb.two_engine_live import (
    _implication, _spread_bucket, engine_b_rules_plain_language, route_plain_language,
)


def test_spread_bucket_boundaries():
    assert _spread_bucket(2.5) == "<3"
    assert _spread_bucket(-3.0) == "3-6.5"
    assert _spread_bucket(6.5) == "3-6.5"
    assert _spread_bucket(7.0) == "7-13.5"
    assert _spread_bucket(-14.0) == "14+"


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
