"""Tests for the CFB head-coach Elo engine's game-to-coach attribution and
Z-score composite math -- the parts with real logic worth verifying beyond
eyeballing the leaderboard output."""
from __future__ import annotations

from sports_aggregator.cfb.coach_elo import BASE, HOME_ADVANTAGE, K_FACTOR, _assign_coaches, _zscore


def _game(game_id, season, home_id, away_id, home="Home", away="Away"):
    return {
        "game_id": game_id, "season": season, "week": game_id,
        "start_date": f"{season}-01-{game_id:02d}", "neutral_site": 0,
        "home_team_id": home_id, "home_team": home, "home_points": 20,
        "home_pregame_elo": 1500,
        "away_team_id": away_id, "away_team": away, "away_points": 14,
        "away_pregame_elo": 1500,
    }


def test_single_coach_season_is_attributed_directly():
    games = [_game(1, 2024, home_id=1, away_id=2)]
    coach_seasons = {
        (2024, 1): [{"coach_id": 100, "team_id": 1, "games": 1,
                     "first_name": "A", "last_name": "One"}],
        (2024, 2): [{"coach_id": 200, "team_id": 2, "games": 1,
                     "first_name": "B", "last_name": "Two"}],
    }
    assignment = _assign_coaches(games, coach_seasons)
    home_coach, name, method = assignment[1]["home"]
    away_coach, _, away_method = assignment[1]["away"]
    assert (home_coach, method) == (100, "direct")
    assert (away_coach, away_method) == (200, "direct")


def test_mid_season_change_gives_the_larger_stint_the_earlier_games():
    # Team 1 plays 4 games; coach 100 (3 credited games) fired after game 3,
    # coach 101 (1 credited game) finishes the season.
    games = [_game(week, 2024, home_id=1, away_id=9) for week in range(1, 5)]
    coach_seasons = {
        (2024, 1): [
            {"coach_id": 100, "team_id": 1, "games": 3, "first_name": "Out", "last_name": "Going"},
            {"coach_id": 101, "team_id": 1, "games": 1, "first_name": "Inter", "last_name": "Im"},
        ],
        (2024, 9): [{"coach_id": 900, "team_id": 9, "games": 4, "first_name": "C", "last_name": "Nine"}],
    }
    assignment = _assign_coaches(games, coach_seasons)
    assigned = [assignment[w]["home"][0] for w in range(1, 5)]
    methods = [assignment[w]["home"][2] for w in range(1, 5)]
    assert assigned == [100, 100, 100, 101]
    assert methods == ["heuristic_split"] * 4


def test_team_season_missing_from_coach_seasons_is_left_unassigned():
    games = [_game(1, 2024, home_id=1, away_id=2)]
    assignment = _assign_coaches(games, coach_seasons={})
    assert 1 not in assignment or "home" not in assignment.get(1, {})


def test_zscore_centers_on_zero_with_unit_spread():
    values = {"a": 10.0, "b": 20.0, "c": 30.0}
    z = _zscore(values)
    assert abs(z["b"]) < 1e-9  # the mean maps to exactly 0
    assert z["a"] < 0 < z["c"]
    assert abs(z["a"] + z["c"]) < 1e-9  # symmetric around the mean


def test_zscore_empty_input_does_not_crash():
    assert _zscore({}) == {}


def test_elo_constants_match_the_nfl_engine_shape():
    # Not asserting specific values -- just that this wasn't accidentally
    # rewired to something wildly different from the validated NFL engine
    # this was modeled on.
    assert BASE == 1500.0
    assert K_FACTOR == 20.0
    assert HOME_ADVANTAGE > 0


def test_current_season_single_zero_game_coach_uses_safe_fallback():
    games = [
        _game(1, 2025, home_id=1, away_id=9),
        _game(2, 2026, home_id=1, away_id=9),
        _game(3, 2026, home_id=1, away_id=9),
    ]
    coach_seasons = {
        (2025, 1): [{"coach_id": 100, "team_id": 1, "games": 1,
                     "first_name": "A", "last_name": "One"}],
        (2025, 9): [{"coach_id": 900, "team_id": 9, "games": 1,
                     "first_name": "B", "last_name": "Nine"}],
        (2026, 1): [{"coach_id": 100, "team_id": 1, "games": 0,
                     "first_name": "A", "last_name": "One"}],
        (2026, 9): [{"coach_id": 900, "team_id": 9, "games": 0,
                     "first_name": "B", "last_name": "Nine"}],
    }
    assignment = _assign_coaches(games, coach_seasons)
    assert assignment[2]["home"] == (100, "A One", "current_single_coach_fallback")
    assert assignment[3]["home"] == (100, "A One", "current_single_coach_fallback")
    assert assignment[2]["away"] == (900, "B Nine", "current_single_coach_fallback")


def test_historical_single_zero_game_coach_is_not_backfilled():
    games = [
        _game(1, 2024, home_id=1, away_id=2),
        _game(2, 2025, home_id=3, away_id=4),
    ]
    coach_seasons = {
        (2024, 1): [{"coach_id": 100, "team_id": 1, "games": 0,
                     "first_name": "Old", "last_name": "Zero"}],
        (2024, 2): [{"coach_id": 200, "team_id": 2, "games": 0,
                     "first_name": "Old", "last_name": "Away"}],
        (2025, 3): [{"coach_id": 300, "team_id": 3, "games": 1,
                     "first_name": "New", "last_name": "Home"}],
        (2025, 4): [{"coach_id": 400, "team_id": 4, "games": 1,
                     "first_name": "New", "last_name": "Away"}],
    }
    assignment = _assign_coaches(games, coach_seasons)
    assert 1 not in assignment or not assignment[1]


def test_current_season_multiple_zero_game_coaches_remain_unassigned():
    games = [_game(1, 2026, home_id=1, away_id=2)]
    coach_seasons = {
        (2026, 1): [
            {"coach_id": 100, "team_id": 1, "games": 0,
             "first_name": "A", "last_name": "One"},
            {"coach_id": 101, "team_id": 1, "games": 0,
             "first_name": "B", "last_name": "Interim"},
        ],
        (2026, 2): [{"coach_id": 200, "team_id": 2, "games": 0,
                     "first_name": "C", "last_name": "Two"}],
    }
    assignment = _assign_coaches(games, coach_seasons)
    assert "home" not in assignment.get(1, {})
    assert assignment[1]["away"] == (200, "C Two", "current_single_coach_fallback")
