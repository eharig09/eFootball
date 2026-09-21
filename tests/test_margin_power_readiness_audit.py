from sports_aggregator.cfb.margin_power_readiness_audit import _readiness_rows


def _game(game_id, week, home, away):
    return {
        "game_id": game_id,
        "season": 2026,
        "week": week,
        "start_date": f"2026-09-{game_id:02d}",
        "home_team": home,
        "away_team": away,
        "home_points": 28,
        "away_points": 21,
    }


def test_readiness_uses_prior_weeks_only():
    games = [
        _game(1, 1, "A", "B"),
        _game(2, 2, "A", "C"),
        _game(3, 2, "B", "D"),
        _game(4, 3, "A", "B"),
        _game(5, 3, "C", "D"),
        _game(6, 4, "A", "B"),
    ]
    rows = {row["game_id"]: row for row in _readiness_rows(games)}
    assert rows[6]["home_prior_games"] == 3
    assert rows[6]["away_prior_games"] == 2
    assert rows[6]["home_ready"] is True
    assert rows[6]["away_ready"] is False
    assert rows[6]["both_ready"] is False


def test_same_week_games_do_not_count_as_prior_games():
    games = [
        _game(1, 1, "A", "B"),
        _game(2, 1, "A", "C"),
        _game(3, 2, "A", "D"),
    ]
    rows = {row["game_id"]: row for row in _readiness_rows(games)}
    assert rows[1]["home_prior_games"] == 0
    assert rows[2]["home_prior_games"] == 0
    assert rows[3]["home_prior_games"] == 2
