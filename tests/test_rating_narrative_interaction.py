from sports_aggregator.cfb.rating_narrative_interaction import (
    _agreement_summary,
    _direction,
    _summary,
)


def test_direction_handles_positive_negative_and_neutral():
    assert _direction(2.0) == 1
    assert _direction(-0.1) == -1
    assert _direction(0.0) == 0
    assert _direction(None) == 0


def test_agreement_summary_separates_confirmation_and_disagreement():
    rows = [
        {"hc_direction": 1, "narrative_direction": 1, "market_residual": 7.0},
        {"hc_direction": 1, "narrative_direction": -1, "market_residual": -3.0},
        {"hc_direction": -1, "narrative_direction": -1, "market_residual": -5.0},
    ]
    result = _agreement_summary(rows, "hc_direction")
    assert result["all_with_narrative"]["n"] == 3
    assert result["narrative_confirms"]["n"] == 2
    assert result["narrative_confirms"]["wins"] == 2
    assert result["narrative_disagrees"]["n"] == 1
    assert result["narrative_disagrees"]["wins"] == 0


def test_summary_aligns_market_residual_to_signal_side():
    rows = [
        {"signal": 1, "market_residual": 4.0},
        {"signal": -1, "market_residual": -6.0},
        {"signal": -1, "market_residual": 2.0},
    ]
    result = _summary(rows, "signal")
    assert result["n"] == 3
    assert result["wins"] == 2
    assert result["mean_aligned_residual"] == 2.667
