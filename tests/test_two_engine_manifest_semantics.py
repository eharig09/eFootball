from sports_aggregator.cfb.two_engine_live import _spread_bucket


def test_spread_bucket_still_matches_frozen_routes():
    assert _spread_bucket(2.5) == "<3"
    assert _spread_bucket(-3.0) == "3-6.5"
    assert _spread_bucket(6.5) == "3-6.5"
    assert _spread_bucket(13.5) == "7-13.5"
    assert _spread_bucket(-14.0) == "14+"


def test_pending_is_not_a_resolved_manifest_state():
    resolved = {"engine_a_only", "engine_b_only", "agreement", "conflict", "none"}
    assert "pending" not in resolved
