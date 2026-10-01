from sports_aggregator.nfl.availability_ablation import POSITION_GROUP, _importance
from sports_aggregator.nfl.repository import NFLRepository


def test_importance_uses_only_prior_games_and_last_six():
    hist = {"a b": [(2020, w, 1.0) for w in range(1, 8)] + [(2020, 8, 0.0)]}
    assert _importance(hist, "a b", 2020, 8) == 1.0  # week 8 itself excluded
    assert _importance(hist, "a b", 2020, 9) == 5 / 6  # last six: five 1.0s and the 0.0
    assert _importance(hist, "nobody", 2020, 9) == 0.0
    assert _importance(hist, "a b", 2020, 1) == 0.0


def test_quarterbacks_are_not_in_any_group():
    assert "QB" not in POSITION_GROUP
    assert POSITION_GROUP["T"] == "ol" and POSITION_GROUP["CB"] == "back"


def test_replace_injury_history_filters_and_is_idempotent(tmp_path):
    repo = NFLRepository(tmp_path / "n.sqlite3")
    rows = [
        {"game_type": "REG", "team": "GB", "week": 1, "gsis_id": "00-1", "full_name": "A B",
         "position": "T", "report_status": "Out", "practice_status": "DNP"},
        {"game_type": "POST", "team": "GB", "week": 19, "gsis_id": "00-2", "full_name": "C D",
         "position": "WR", "report_status": "Out", "practice_status": None},
        {"game_type": "REG", "team": "GB", "week": 1, "gsis_id": "", "full_name": "E F"},
    ]
    assert repo.replace_injury_history(2020, rows) == 1
    assert repo.replace_injury_history(2020, rows) == 1
