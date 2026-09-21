from sports_aggregator.cfb.convergence_routing_holdout import (
    ROUTES,
    _combined_portfolio,
    _route_rows,
)


def _row(
    game_id,
    *,
    agreement_count,
    old_count,
    elo_confirms,
    bucket,
    residual,
    side="home",
):
    return {
        "game_id": game_id,
        "season": 2026,
        "agreement_count": agreement_count,
        "old_agreement_count": old_count,
        "hc_qb_elo_confirms": elo_confirms,
        "spread_bucket": bucket,
        "aligned_residual": residual,
        "hit": residual > 0,
        "selected_side": side,
        "selected_team": "Home" if side == "home" else "Away",
        "home_team": "Home",
        "away_team": "Away",
    }


def test_fade_route_reverses_result_and_selected_side():
    route = next(r for r in ROUTES if r["name"] == "fade_old_2_of_3_elo_agrees_spread_lt_3")
    rows = [_row(
        1,
        agreement_count=3,
        old_count=2,
        elo_confirms=True,
        bucket="<3",
        residual=-4.0,
    )]
    routed = _route_rows(rows, route)
    assert len(routed) == 1
    assert routed[0]["routed_aligned_residual"] == 4.0
    assert routed[0]["routed_hit"] is True
    assert routed[0]["routed_selected_side"] == "away"
    assert routed[0]["routed_selected_team"] == "Away"


def test_zero_residual_is_not_a_win_when_faded():
    route = next(r for r in ROUTES if r["name"] == "fade_old_3_of_3_elo_disagrees_spread_14_plus")
    rows = [_row(
        2,
        agreement_count=3,
        old_count=3,
        elo_confirms=False,
        bucket="14+",
        residual=0.0,
    )]
    routed = _route_rows(rows, route)
    assert routed[0]["routed_aligned_residual"] == 0.0
    assert routed[0]["routed_hit"] is False


def test_frozen_routes_are_non_overlapping():
    rows = [
        _row(1, agreement_count=4, old_count=3, elo_confirms=True, bucket="<3", residual=2),
        _row(2, agreement_count=3, old_count=2, elo_confirms=True, bucket="3-6.5", residual=2),
        _row(3, agreement_count=3, old_count=3, elo_confirms=False, bucket="<3", residual=2),
        _row(4, agreement_count=3, old_count=2, elo_confirms=True, bucket="<3", residual=-2),
        _row(5, agreement_count=3, old_count=3, elo_confirms=False, bucket="14+", residual=-2),
    ]
    portfolio = _combined_portfolio(rows)
    assert len(portfolio) == 5
    assert len({r["game_id"] for r in portfolio}) == 5
