"""Tests for the NFL live-forecast serving path degrading gracefully instead
of blacking out entirely when a team has less than MIN_PRIOR_GAMES history."""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sports_aggregator.nfl.drive_projection import MIN_PRIOR_GAMES, TeamHistory
from sports_aggregator.nfl.live_projection import _market_anchor, _team_row, report
from sports_aggregator.nfl.repository import NFLRepository

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


def test_market_anchor_needs_no_model():
    game = {"spread_line": -3.5, "total_line": 44.0}
    market = _market_anchor(game)
    assert market == {
        "margin": -3.5, "total": 44.0, "home_points": 20.25, "away_points": 23.75,
    }
    assert _market_anchor({"spread_line": None, "total_line": 44.0}) is None


class _ThinLeagueRepositoryTests(unittest.TestCase):
    """Reproduces the real production symptom: a games table seeded with only
    the current season (no 2010-2025 archive), so the football-only core
    models can't reach their 100-row training minimum this early in a season.
    Market lines should still surface even though Football Lab can't yet."""

    TEAMS = ("AAA", "BBB", "CCC", "DDD")

    def setUp(self):
        handle, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)

        def _cleanup():
            try:
                os.remove(path)
            except OSError:
                # sqlite3 on Windows can hold the file open across the
                # connections report() opens internally; not a functional
                # concern for what this test is checking.
                pass

        self.addCleanup(_cleanup)
        self.repository = NFLRepository(Path(path))
        self.repository.initialize()

    def _insert_week(self, connection, season: int, week: int, completed: bool,
                     spread: float | None = -1.5, total: float | None = 45.0):
        pairs = [(self.TEAMS[0], self.TEAMS[1]), (self.TEAMS[2], self.TEAMS[3])]
        for index, (away, home) in enumerate(pairs):
            game_id = f"{season}_{week:02d}_{away}_{home}"
            connection.execute(
                """INSERT INTO games(game_id,season,season_type,week,game_date,
                    away_team,home_team,away_score,home_score,completed,overtime,
                    division_game,spread_line,total_line,away_rest,home_rest,
                    updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,0,0,?,?,7,7,?)""",
                (game_id, season, "REG", week, f"{season}-09-{7+week:02d}",
                 away, home, 20 if completed else None, 23 if completed else None,
                 1 if completed else 0, spread, total, "2026-01-01T00:00:00Z"),
            )
            if not completed:
                continue
            for team, opponent in ((away, home), (home, away)):
                connection.execute(
                    """INSERT INTO game_team_efficiency(season,week,game_id,team,
                        opponent_team,plays,total_epa,successful_plays,pass_plays,
                        pass_epa,rush_plays,rush_epa,early_down_plays,
                        early_down_epa,explosive_plays)
                       VALUES(?,?,?,?,?,64,1.5,28,38,3.0,26,-1.0,30,0.5,6)""",
                    (season, week, game_id, team, opponent),
                )
                connection.execute(
                    """INSERT INTO game_team_situational(season,week,game_id,team,
                        opponent_team,plays,drives,third_down_plays,
                        third_down_conversions,red_zone_plays,red_zone_successes,
                        neutral_plays,neutral_passes,seconds_sum,clocked_plays)
                       VALUES(?,?,?,?,?,64,11,14,6,4,2,50,29,1400,55)""",
                    (season, week, game_id, team, opponent),
                )

    def test_thin_league_history_still_surfaces_the_market_line(self):
        with closing(self.repository._connect()) as connection:
            # Only 2 completed weeks exist anywhere in the database (mirrors
            # production's season-2026-only seed) -- 8 team-rows total, well
            # under the 100-row minimum every football-only model needs to fit.
            self._insert_week(connection, 2026, 1, completed=True)
            self._insert_week(connection, 2026, 2, completed=True)
            self._insert_week(connection, 2026, 3, completed=False, spread=-2.5, total=46.5)
            connection.commit()

        result = report(self.repository, season=2026, week=3)
        self.assertEqual(len(result["games"]), 2)
        for game in result["games"]:
            self.assertEqual(game["status"], "insufficient_history")
            self.assertIsNone(game["football_lab"])
            # This is the actual bug being fixed: the market line needs no
            # model at all and shouldn't be withheld just because Football
            # Lab's independent side can't train yet.
            self.assertIsNotNone(game["market_anchor"])
            self.assertEqual(game["market_anchor"]["margin"], -2.5)
            self.assertEqual(game["market_anchor"]["total"], 46.5)
