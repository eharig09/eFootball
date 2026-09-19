"""Milestone 11 drive-outcome, leakage, opponent-quality and form tests."""
from __future__ import annotations

from contextlib import closing
import os
import sqlite3
import tempfile
import unittest

from sports_aggregator.cfb import team_game_drive_outcomes as outcomes
from sports_aggregator.cfb import xpoints
from sports_aggregator.cfb import external
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


class XPointsFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle); os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        xpoints.initialize(self.repository)
        external.initialize(self.repository)

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path): os.unlink(path)

    def game(self, game_id, week, home_points, away_points, *,
             home_elo=1600, away_elo=1500):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
              start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
              home_points,home_pregame_elo,away_team_id,away_team,away_points,
              away_pregame_elo,updated_at)
              VALUES(?,2026,?,'regular',?,0,1,0,0,1,'Alpha',?,?,2,'Beta',?,?,?)""",
              (game_id, week, f"2026-09-{week:02d}", home_points, home_elo,
               away_points, away_elo, NOW))
            connection.execute("""INSERT INTO game_lines(game_id,season,provider,spread,
              over_under,formatted_spread,fetched_at) VALUES(?,2026,'test',-4,52,'Alpha -4',?)""",
              (game_id, NOW))
            connection.commit()

    def actual(self, game_id, team, opponent, *, touchdowns, field_goals,
               turnovers=1, punts=4, drives=10):
        points = touchdowns * 7 + field_goals * 3
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO cfb_team_game_drive_outcomes(
              game_id,team,opponent,metric_version,meaningful_drives,touchdowns,
              field_goals,turnovers,punts,turnovers_on_downs,end_half_drives,
              other_drives,offensive_points,touchdowns_per_drive,field_goals_per_drive,
              turnovers_per_drive,punts_per_drive,points_per_drive,built_at)
              VALUES(?,?,?,'team-game-drive-outcomes-v1',?,?,?,?,?,0,0,0,?,?,?,?,?,?,?)""",
              (game_id, team, opponent, drives, touchdowns, field_goals, turnovers, punts,
               points, touchdowns / drives, field_goals / drives, turnovers / drives,
               punts / drives, points / drives, NOW))
            connection.commit()

    def seed_game(self, game_id, week, home_score, away_score, home_td, away_td):
        self.game(game_id, week, home_score, away_score)
        self.actual(game_id, "Alpha", "Beta", touchdowns=home_td, field_goals=1)
        self.actual(game_id, "Beta", "Alpha", touchdowns=away_td, field_goals=1)


class OutcomeClassificationTests(unittest.TestCase):
    def test_defensive_touchdown_is_a_turnover_not_an_offensive_touchdown(self):
        rows = [{"scoring": 1, "rush_pass": "pass", "play_type": "Interception Return Touchdown",
                 "garbage_time": 0}]
        self.assertEqual(outcomes._outcome(rows), "turnovers")

    def test_outcomes_are_mutually_exclusive(self):
        touchdown = [{"scoring": 1, "rush_pass": "rush", "play_type": "Rushing Touchdown",
                      "garbage_time": 0}]
        field_goal = [{"scoring": 1, "rush_pass": None, "play_type": "Field Goal Good",
                       "garbage_time": 0}]
        self.assertEqual(outcomes._outcome(touchdown), "touchdowns")
        self.assertEqual(outcomes._outcome(field_goal), "field_goals")


class LeakSafeDatasetTests(XPointsFixture):
    def test_current_game_is_excluded_from_prior_form_and_efficiency(self):
        self.seed_game(1, 1, 31, 17, 4, 2)
        self.seed_game(2, 2, 20, 27, 2, 3)
        self.seed_game(3, 3, 70, 0, 9, 0)
        report = xpoints.build_dataset(self.repository, from_season=2026, to_season=2026)
        self.assertEqual(report["rows"], 6)
        with self.repository._reader() as connection:
            row = dict(connection.execute("""SELECT * FROM cfb_xpoints_dataset
              WHERE game_id=3 AND team='Alpha'""").fetchone())
        self.assertEqual(row["team_prior_games"], 2)
        self.assertLess(row["team_prior_points_per_drive"], 4.0)
        self.assertGreater(row["team_recent_margin"], -7.0)
        self.assertLess(row["team_recent_margin"], 14.0)

    def test_quality_blend_orients_every_source_to_team_margin(self):
        self.seed_game(1, 1, 31, 17, 4, 2)
        xpoints.build_dataset(self.repository, from_season=2026, to_season=2026)
        with self.repository._reader() as connection:
            alpha = dict(connection.execute("""SELECT * FROM cfb_xpoints_dataset
              WHERE game_id=1 AND team='Alpha'""").fetchone())
            beta = dict(connection.execute("""SELECT * FROM cfb_xpoints_dataset
              WHERE game_id=1 AND team='Beta'""").fetchone())
        # Elo says +4 points and Vegas says +4, with unavailable FPI/CORE omitted.
        self.assertAlmostEqual(alpha["opponent_quality_blend"], 4.0)
        self.assertAlmostEqual(beta["opponent_quality_blend"], -4.0)
        self.assertEqual(alpha["quality_source_count"], 2)

    def test_quality_blend_adds_strictly_pregame_fpi_and_core(self):
        self.seed_game(1, 2, 31, 17, 4, 2)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany("INSERT INTO core_ratings VALUES(2026,'regular',1,?,?,?,?,?,?,?,?)", (
                ("Alpha", "A", 8.0, 4.0, 4.0, 100, 100, "test"),
                ("Beta", "B", 2.0, 1.0, 1.0, 100, 100, "test"),
            ))
            connection.executemany("""INSERT INTO fpi_game_projections VALUES(
              2026,1,?,?,NULL,NULL,NULL,'test','test',?)""", (
                (1, 10.0, NOW), (2, -10.0, NOW)))
            connection.commit()
        xpoints.build_dataset(self.repository, from_season=2026, to_season=2026)
        with self.repository._reader() as connection:
            alpha = dict(connection.execute("""SELECT * FROM cfb_xpoints_dataset
              WHERE game_id=1 AND team='Alpha'""").fetchone())
        self.assertEqual(alpha["quality_source_count"], 4)
        self.assertAlmostEqual(alpha["core_margin"], 6.0)
        self.assertAlmostEqual(alpha["fpi_margin"], 10.0)
        self.assertAlmostEqual(alpha["opponent_quality_blend"], 6.0)

    def test_opponent_adjustment_uses_only_previously_established_norms(self):
        self.seed_game(1, 1, 31, 17, 4, 2)
        self.seed_game(2, 2, 20, 27, 2, 3)
        self.seed_game(3, 3, 24, 21, 3, 3)
        xpoints.build_dataset(self.repository, from_season=2026, to_season=2026)
        with self.repository._reader() as connection:
            second = dict(connection.execute("""SELECT * FROM cfb_xpoints_dataset
              WHERE game_id=2 AND team='Alpha'""").fetchone())
            third = dict(connection.execute("""SELECT * FROM cfb_xpoints_dataset
              WHERE game_id=3 AND team='Alpha'""").fetchone())
        self.assertIsNone(second["team_opponent_adjusted_residual"])
        self.assertIsNotNone(third["team_opponent_adjusted_residual"])


if __name__ == "__main__":
    unittest.main()
