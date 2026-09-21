from sports_aggregator.cfb.two_engine_live import _implication, _spread_bucket


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
