from sports_aggregator.cfb.extreme_narrative_rating_modifiers import (
    _bucket,
    _percentile,
    _summary_values,
)


def test_summary_values_uses_aligned_values():
    result = _summary_values([4.0, -2.0, 6.0])
    assert result["n"] == 3
    assert result["wins"] == 2
    assert result["hit_rate"] == 0.6667
    assert result["mean_aligned_residual"] == 2.667


def test_percentile_interpolates():
    assert _percentile([0.0, 10.0], 0.5) == 5.0


def test_bucket_respects_training_cuts():
    cuts = [1.0, 2.0]
    assert _bucket(0.5, cuts) == "low"
    assert _bucket(1.5, cuts) == "mid"
    assert _bucket(2.5, cuts) == "high"
