"""internal_power_lenses._efficiency_power_margin_lookup() -- the discovery
side of Structural's third live member, reconciled to read the exact same
persisted value (cfb_projection_backtest.projected_residual_points_per_drive)
the live _efficiency_power_edge() computes, instead of a separate
unbounded-history formula that only correlated 0.76 with it. These tests
seed cfb_projection_backtest/cfb_team_game_pace directly rather than running
the full live projection pipeline, since the lookup itself only ever reads
those two tables in bulk.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb import two_engine_live as tel
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION, initialize as initialize_backtest
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


class EfficiencyPowerFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        initialize_backtest(self.repository)

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def game(self, game_id, *, home, away, season, week, start_date):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
                start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
                away_team_id,away_team,updated_at) VALUES(?,?,?,'regular',?,0,1,0,0,1,?,2,?,?)""",
                (game_id, season, week, start_date, home, away, NOW))
            c.commit()

    def pace(self, *, game_id, team, opponent, drives):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_team_game_pace(game_id,team,opponent,metric_version,
                raw_drives,meaningful_drives,ot_drives,three_and_out_drives,scrimmage_plays,
                plays_per_meaningful_drive,seconds_per_play,neutral_seconds_per_play,
                pass_rate,neutral_pass_rate,success_rate,explosive_rate,first_down_rate,
                three_and_out_rate,tempo_intervals,neutral_tempo_intervals,built_at)
                VALUES(?,?,?,'team-game-pace-v1',?,?,0,0,?,6.0,25.0,25.0,0.5,0.5,0.45,0.1,0.3,0.2,10,10,?)""",
                (game_id, team, opponent, drives, drives, round(drives * 6), NOW))
            c.commit()

    def backtest_row(self, *, game_id, team, opponent, side, season, week, start_date,
                     residual_ppd):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_projection_backtest(
                game_id,team,opponent,side,backtest_version,season,week,kickoff,
                prior_games,projected_residual_points_per_drive,built_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (game_id, team, opponent, side, BACKTEST_VERSION, season, week,
                 start_date, 5, residual_ppd, NOW))
            c.commit()


class EfficiencyPowerMarginLookupTests(EfficiencyPowerFixture):
    def test_matches_the_hand_computed_formula(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=2,
                  start_date="2026-09-13T00:00:00+00:00")
        # One earlier game this season sets the "actual drives so far" prior.
        self.game(2, home="Rutgers", away="Purdue", season=2026, week=1,
                  start_date="2026-09-06T00:00:00+00:00")
        self.pace(game_id=2, team="Rutgers", opponent="Purdue", drives=10.0)
        self.pace(game_id=2, team="Purdue", opponent="Rutgers", drives=12.0)
        # Game 1 itself also needs pace rows -- a real backtest-covered game
        # always has them (projection_backtest.build() requires team-pace
        # data to include a game at all); this is just what makes game 1's
        # own kickoff date a known key to look up league_drives_by_date at.
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=13.0)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=14.0)
        self.backtest_row(game_id=1, team="Michigan", opponent="Ohio State", side="home",
                          season=2026, week=2, start_date="2026-09-13T00:00:00+00:00",
                          residual_ppd=2.2)
        self.backtest_row(game_id=1, team="Ohio State", opponent="Michigan", side="away",
                          season=2026, week=2, start_date="2026-09-13T00:00:00+00:00",
                          residual_ppd=1.8)

        lookup = ipl._efficiency_power_margin_lookup(
            self.repository, from_season=2026, to_season=2026)

        # league_drives = mean of week-1's actual drives = (10+12)/2 = 11.0
        # home_margin = (2.2-1.8) * 11.0 + HFA_POINTS(2.5) = 4.4 + 2.5 = 6.9
        self.assertAlmostEqual(lookup[(1, "Michigan")], 6.9)
        self.assertAlmostEqual(lookup[(1, "Ohio State")], -6.9)

    def test_missing_residual_on_either_side_excludes_the_game(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2026-09-06T00:00:00+00:00")
        self.backtest_row(game_id=1, team="Michigan", opponent="Ohio State", side="home",
                          season=2026, week=1, start_date="2026-09-06T00:00:00+00:00",
                          residual_ppd=2.2)
        # Away side never got a row (e.g. missing prior data).
        lookup = ipl._efficiency_power_margin_lookup(
            self.repository, from_season=2026, to_season=2026)
        self.assertNotIn((1, "Michigan"), lookup)

    def test_no_prior_league_drives_excludes_the_game(self):
        # First game of the season -- nothing prior to average.
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2026-09-06T00:00:00+00:00")
        self.backtest_row(game_id=1, team="Michigan", opponent="Ohio State", side="home",
                          season=2026, week=1, start_date="2026-09-06T00:00:00+00:00",
                          residual_ppd=2.2)
        self.backtest_row(game_id=1, team="Ohio State", opponent="Michigan", side="away",
                          season=2026, week=1, start_date="2026-09-06T00:00:00+00:00",
                          residual_ppd=1.8)
        lookup = ipl._efficiency_power_margin_lookup(
            self.repository, from_season=2026, to_season=2026)
        self.assertEqual(lookup, {})


class LiveLeagueDrivesThisSeasonTests(EfficiencyPowerFixture):
    def test_averages_strictly_prior_games_only_this_season(self):
        self.game(1, home="Rutgers", away="Purdue", season=2026, week=1,
                  start_date="2026-09-06T00:00:00+00:00")
        self.pace(game_id=1, team="Rutgers", opponent="Purdue", drives=10.0)
        self.pace(game_id=1, team="Purdue", opponent="Rutgers", drives=12.0)
        # A later game must not affect an earlier "before_date" lookup.
        self.game(2, home="Michigan", away="Ohio State", season=2026, week=3,
                  start_date="2026-09-20T00:00:00+00:00")
        self.pace(game_id=2, team="Michigan", opponent="Ohio State", drives=20.0)
        self.pace(game_id=2, team="Ohio State", opponent="Michigan", drives=22.0)

        value = tel._live_league_drives_this_season(
            self.repository, season=2026, before_date="2026-09-13T00:00:00+00:00")
        self.assertAlmostEqual(value, 11.0)

    def test_returns_none_with_no_prior_games(self):
        value = tel._live_league_drives_this_season(
            self.repository, season=2026, before_date="2026-09-06T00:00:00+00:00")
        self.assertIsNone(value)

    def test_ignores_a_different_seasons_games(self):
        self.game(1, home="Rutgers", away="Purdue", season=2025, week=1,
                  start_date="2025-09-06T00:00:00+00:00")
        self.pace(game_id=1, team="Rutgers", opponent="Purdue", drives=10.0)
        value = tel._live_league_drives_this_season(
            self.repository, season=2026, before_date="2026-09-13T00:00:00+00:00")
        self.assertIsNone(value)


if __name__ == "__main__":
    unittest.main()
