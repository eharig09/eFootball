"""Milestone 10 field-position reconstruction and leakage tests."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import team_game_special_teams as special
from sports_aggregator.cfb import xfieldposition
from sports_aggregator.cfb import game_projection
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


class FieldPositionFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle); os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        special.initialize(self.repository)
        self.play_id = 0

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path): os.unlink(path)

    def game(self, game_id, *, home="Michigan", away="Ohio State", week=1,
             date="2026-08-30"):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
              start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
              away_team_id,away_team,updated_at) VALUES(?,?,?,'regular',?,0,1,0,0,1,?,2,?,?)""",
              (game_id, 2026, week, date, home, away, NOW))
            connection.commit()

    def play(self, *, game_id=1, drive="d1", drive_number=1, play_number=1,
             offense="Michigan", defense="Ohio State", play_type="Rush",
             rush_pass="rush", ytg=75, down=1, distance=10, scoring=0):
        self.play_id += 1; pid = f"p{self.play_id}"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO cfb_plays(
              play_id,game_id,drive_id,drive_number,play_number,offense,defense,period,
              clock_minutes,clock_seconds,offense_score,defense_score,yards_to_goal,down,
              distance,yards_gained,scoring,play_type,play_text,season,week,raw_json,imported_at)
              VALUES(?,?,?,?,?,?,?,1,10,0,0,0,?,?,?,5,?,?,?,2026,1,'{}',?)""",
              (pid, game_id, drive, drive_number, play_number, offense, defense, ytg,
               down, distance, scoring, play_type, play_type, NOW))
            connection.execute("""INSERT INTO cfb_play_metrics(
              play_id,metric_version,rush_pass,garbage_time,derived_at)
              VALUES(?,'pbp-v1',?,0,?)""", (pid, rush_pass, NOW))
            connection.commit()

    def actual(self, game_id, team, opponent, *, start=75, punt_start=82, fgm=1, fga=1):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO cfb_team_game_special_teams VALUES(
              ?,?,?,'team-game-special-teams-v1',10,?, ?,
              2,150,75,2,?, ?,3,120,40,1,.333,?, ?,
              3,225,75,?,?,?, ?,?,?)""",
              (game_id, team, opponent, start * 10, start,
               punt_start * 2, punt_start, punt_start * 3, punt_start,
               fga, fgm, 40 * fga, 40 if fga else None,
               fgm / fga if fga else None, NOW))
            connection.commit()


class ReconstructionTests(FieldPositionFixture):
    def test_post_kick_spot_comes_from_first_snap_not_kick_row(self):
        self.game(1)
        # Kickoff row's 35 is the pre-kick spot; the first snap says the
        # receiving offense actually starts 75 yards from goal.
        self.play(drive="d1", drive_number=1, play_number=1,
                  play_type="Kickoff", rush_pass=None, ytg=35, down=0)
        self.play(drive="d1", drive_number=1, play_number=2, ytg=75)
        self.play(drive="d2", drive_number=2, play_number=1, ytg=70)
        self.play(drive="d2", drive_number=2, play_number=2,
                  play_type="Punt", rush_pass=None, ytg=70, down=4)
        self.play(drive="d3", drive_number=3, play_number=1,
                  offense="Ohio State", defense="Michigan", ytg=85)
        self.play(drive="d4", drive_number=4, play_number=1, ytg=20)
        self.play(drive="d4", drive_number=4, play_number=2,
                  play_type="Field Goal Good", rush_pass=None, ytg=20, down=4, scoring=1)
        special.build(self.repository)
        rows = {row["team"]: row for row in special.game_summary(self.repository, 1)}
        michigan = rows["Michigan"]
        self.assertAlmostEqual(michigan["average_start_after_kickoff"], 75)
        self.assertAlmostEqual(michigan["average_net_punt_yards"], 55)
        self.assertAlmostEqual(michigan["average_opponent_start_after_punt"], 85)
        self.assertEqual(michigan["punts_inside_20"], 1)
        self.assertAlmostEqual(michigan["average_field_goal_distance"], 37)
        self.assertAlmostEqual(michigan["field_goal_accuracy"], 1)
        self.assertAlmostEqual(rows["Ohio State"]["average_start_after_punt"], 85)


class DatasetTests(FieldPositionFixture):
    def test_current_game_does_not_enter_its_own_field_position_priors(self):
        self.game(1, week=1, date="2026-08-30")
        self.game(2, week=2, date="2026-09-06")
        self.actual(1, "Michigan", "Ohio State", start=75, punt_start=82)
        self.actual(1, "Ohio State", "Michigan", start=70, punt_start=78)
        self.actual(2, "Michigan", "Ohio State", start=55, punt_start=60)
        self.actual(2, "Ohio State", "Michigan", start=90, punt_start=92)
        xfieldposition.build_dataset(self.repository)
        with self.repository._reader() as connection:
            row = dict(connection.execute("""SELECT * FROM cfb_xfieldposition_dataset
              WHERE game_id=2 AND team='Michigan'""").fetchone())
        self.assertEqual(row["team_prior_games"], 1)
        self.assertAlmostEqual(row["team_prior_start_yards_to_goal"], 75)
        self.assertAlmostEqual(row["opponent_prior_start_yards_to_goal_allowed"], 75)
        self.assertAlmostEqual(row["team_prior_start_after_punt"], 82)
        self.assertAlmostEqual(row["opponent_prior_start_after_punt_imposed"], 78)

        snapshot = game_projection.team_special_teams_snapshot(
            self.repository, "Michigan", before_date="2026-09-06")
        environment = game_projection.field_position_environment(
            self.repository, before_date="2026-09-06")
        self.assertAlmostEqual(snapshot["start_yards_to_goal"], 75)
        self.assertAlmostEqual(snapshot["start_yards_to_goal_allowed"], 70)
        self.assertAlmostEqual(snapshot["net_punt_yards"], 40)
        self.assertAlmostEqual(environment["start_yards_to_goal"], 72.5)
        self.assertAlmostEqual(environment["start_after_punt"], 80)


if __name__ == "__main__":
    unittest.main()
