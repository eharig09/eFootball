from sports_aggregator.cfb.narrative_family_rating_interactions import (
    _family_tags,
    _summary,
)


def test_family_tags_deduplicate_related_exact_tags():
    families, sources = _family_tags([
        "bad_loss",
        "bounceback_candidate",
        "upset_win",
    ])
    assert families == {
        "negative_result_rebound",
        "positive_result_momentum",
    }
    assert sources["negative_result_rebound"] == [
        "bad_loss",
        "bounceback_candidate",
    ]


def test_market_premium_and_discount_are_not_collapsed_together():
    families, _ = _family_tags([
        "market_darling",
        "market_chase",
        "market_skepticism",
        "market_lag",
    ])
    assert families == {"market_premium", "market_discount"}


def test_summary_reports_directional_outcomes():
    result = _summary([3.0, -1.0, 4.0])
    assert result["n"] == 3
    assert result["wins"] == 2
    assert result["hit_rate"] == 0.6667
    assert result["mean_aligned_residual"] == 2.0
