from sports_aggregator.cfb.research_coverage_audit import _diagnosis


def test_diagnosis_identifies_first_zero_and_last_positive():
    stages = [
        {"stage": "games", "status": "ok", "n": 100},
        {"stage": "lines", "status": "ok", "n": 95},
        {"stage": "derived", "status": "ok", "n": 0},
        {"stage": "later", "status": "ok", "n": 0},
    ]
    result = _diagnosis(stages)
    assert result["first_zero_stage"] == "derived"
    assert result["last_positive_stage_before_zero"] == "lines"


def test_diagnosis_preserves_errors():
    stages = [
        {"stage": "games", "status": "ok", "n": 100},
        {"stage": "missing_table", "status": "error", "n": None, "error": "no such table"},
        {"stage": "derived", "status": "ok", "n": 0},
    ]
    result = _diagnosis(stages)
    assert result["first_zero_stage"] == "derived"
    assert result["errors"] == [{"stage": "missing_table", "error": "no such table"}]
