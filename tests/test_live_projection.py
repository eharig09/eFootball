"""Tests for the NFL live-forecast serving path degrading gracefully instead
of blacking out entirely when a team has less than MIN_PRIOR_GAMES history."""
from __future__ import annotations

from sports_aggregator.nfl.drive_projection import MIN_PRIOR_GAMES, TeamHistory
from sports_aggregator.nfl.live_projection import _team_row

# Deliberately far from any realistic ratio the seasoned fixture below
# produces, so a test asserting "== league fallback" can't pass by accident.
LEAGUE = {
    "drives_for": 999.0, "drives_allowed": 999.0,
    "plays_per_drive": 999.0, "plays_per_drive_allowed": 999.0,
    "neutral_seconds_per_play": 999.0, "opponent_neutral_seconds_per_play": 999.0,
    "neutral_pass_rate": 0.999, "opponent_neutral_pass_rate": 0.999,
    "epa_per_play": 999.0, "epa_allowed_per_play": 999.0,
    "pass_epa_per_play": 999.0, "pass_epa_allowed_per_play": 999.0,
    "rush_epa_per_play": 999.0, "rush_epa_allowed_per_play": 999.0,
    "success_rate": 0.999, "success_allowed_rate": 0.999,
    "explosive_rate": 0.999, "explosive_allowed_rate": 0.999,
}


def _seasoned_history() -> TeamHistory:
    h = TeamHistory()
    h.games = 40
    h.weighted_games = 20.0
    h.drives_for = 220.0
    h.drives_against = 210.0
    h.plays = 1300.0
    h.opponent_plays = 1280.0
    h.seconds_sum = 26000.0
    h.clocked_plays = 950.0
    h.neutral_plays = 1000.0
    h.neutral_passes = 580.0
    h.opponent_seconds_sum = 25500.0
    h.opponent_clocked_plays = 940.0
    h.opponent_neutral_plays = 990.0
    h.opponent_neutral_passes = 560.0
    h.total_epa = 15.0
    h.pass_plays = 700.0
    h.pass_epa = 20.0
    h.rush_plays = 500.0
    h.rush_epa = -8.0
    h.successful_plays = 550.0
    h.explosive_plays = 100.0
    h.opponent_total_epa = -10.0
    h.opponent_pass_plays = 690.0
    h.opponent_pass_epa = -5.0
    h.opponent_rush_plays = 480.0
    h.opponent_rush_epa = -12.0
    h.opponent_successful_plays = 520.0
    h.opponent_explosive_plays = 90.0
    return h


def _game(away="AWY", home="HOM"):
    return {
        "game_id": "2026_01_AWY_HOM", "season": 2026, "week": 1,
        "away_team": away, "home_team": home,
        "home_rest": 7, "away_rest": 7, "division_game": 0,
    }


def test_thin_team_still_gets_a_row_via_league_fallback_not_none():
    history = {"AWY": TeamHistory(), "HOM": _seasoned_history()}
    row = _team_row(_game(), "away", history, LEAGUE)
    assert row is not None
    assert row["thin_sample"] is True
    # The zero-game AWAY team has no real numbers of its own -- its "team_*"
    # fields should fall back to the league average rather than leaving the
    # whole game blacked out. "opponent_*" fields describe the HOME side,
    # which is seasoned here, so those stay real rather than falling back.
    assert row["team_drives"] == LEAGUE["drives_for"]
    assert row["team_epa_per_play"] == LEAGUE["epa_per_play"]
    assert row["opponent_drives_allowed"] != LEAGUE["drives_allowed"]


def test_seasoned_matchup_is_not_flagged_thin():
    history = {"AWY": _seasoned_history(), "HOM": _seasoned_history()}
    row = _team_row(_game(), "away", history, LEAGUE)
    assert row is not None
    assert row["thin_sample"] is False
    # Uses its own accumulated numbers, not the league fallback.
    assert row["team_drives"] != LEAGUE["drives_for"]


def test_one_thin_side_is_enough_to_flag_the_matchup():
    history = {"AWY": _seasoned_history(), "HOM": TeamHistory()}
    away_row = _team_row(_game(), "away", history, LEAGUE)
    home_row = _team_row(_game(), "home", history, LEAGUE)
    assert away_row["thin_sample"] is True
    assert home_row["thin_sample"] is True


def test_below_threshold_but_nonzero_games_keeps_real_noisy_values():
    thin = TeamHistory()
    thin.games = MIN_PRIOR_GAMES - 1
    thin.weighted_games = float(thin.games)
    thin.drives_for = 22.0
    thin.drives_against = 20.0
    thin.plays = 130.0
    thin.opponent_plays = 128.0
    thin.total_epa = 1.0
    thin.pass_plays = 70.0
    thin.pass_epa = 2.0
    thin.rush_plays = 50.0
    thin.rush_epa = -1.0
    thin.successful_plays = 55.0
    thin.explosive_plays = 10.0
    thin.opponent_total_epa = -1.0
    thin.opponent_pass_plays = 69.0
    thin.opponent_pass_epa = -0.5
    thin.opponent_rush_plays = 48.0
    thin.opponent_rush_epa = -1.2
    thin.opponent_successful_plays = 52.0
    thin.opponent_explosive_plays = 9.0
    history = {"AWY": thin, "HOM": _seasoned_history()}
    row = _team_row(_game(), "away", history, LEAGUE)
    assert row["thin_sample"] is True
    # Already has real (if noisy) drives-per-game -- shouldn't be overwritten
    # by the league average just because it's below the confidence threshold.
    assert row["team_drives"] != LEAGUE["drives_for"]
