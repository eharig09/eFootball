"""Milestone 9 actuals, leakage guards, and live projection math."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import game_projection
from sports_aggregator.cfb import team_game_scoring as scoring
from sports_aggregator.cfb import xredzone, xturnovers
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


class ScoringFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle); os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        scoring.initialize(self.repository)
        self.play_number = 0

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def game(self, game_id, *, home="Michigan", away="Ohio State", week=1,
             start_date="2026-08-30"):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
              start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
              away_team_id,away_team,updated_at)
              VALUES(?,?,?,'regular',?,0,1,0,0,1,?,2,?,?)""",
              (game_id, 2026, week, start_date, home, away, NOW))
            connection.commit()

    def play(self, *, game_id, drive, offense, defense, play_type="Rush", rush_pass="rush",
             yards_to_goal=50, down=1, distance=10, scoring_play=0, garbage=0):
        self.play_number += 1
        play_id = f"p{self.play_number}"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO cfb_plays(
              play_id,game_id,drive_id,drive_number,play_number,offense,defense,
              period,clock_minutes,clock_seconds,offense_score,defense_score,yards_to_goal,
              down,distance,yards_gained,scoring,play_type,play_text,season,week,raw_json,imported_at)
              VALUES(?,?,?,1,?,?,?,1,10,0,0,0,?,?,?,5,?,?,?,2026,1,'{}',?)""",
              (play_id, game_id, drive, self.play_number, offense, defense, yards_to_goal,
               down, distance, scoring_play, play_type, play_type, NOW))
            connection.execute("""INSERT INTO cfb_play_metrics(
              play_id,metric_version,rush_pass,garbage_time,derived_at)
              VALUES(?,'pbp-v1',?,?,?)""", (play_id, rush_pass, garbage, NOW))
            connection.execute("""INSERT OR IGNORE INTO cfb_drive_metrics(
              game_id,drive_id,metric_version,offense,defense,plays,scrimmage_plays,
              points,derived_at) VALUES(?,?,'pbp-v1',?,?,1,1,0,?)""",
              (game_id, drive, offense, defense, NOW))
            connection.commit()

    def actual(self, *, game_id, team, opponent, drives=10, plays=60, giveaways=1,
               trips=3, touchdowns=2):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO cfb_team_game_scoring(
              game_id,team,opponent,metric_version,meaningful_drives,competitive_plays,
              giveaways,interceptions,fumbles_lost,giveaway_rate,red_zone_trips,
              red_zone_touchdowns,red_zone_field_goal_attempts,red_zone_td_rate,
              red_zone_pass_plays,red_zone_rush_plays,red_zone_pass_rate,
              goal_to_go_trips,goal_to_go_touchdowns,goal_to_go_td_rate,built_at)
              VALUES(?,?,?,'team-game-scoring-v1',?,?,?,?,?,?,?,?,0,?,0,0,NULL,0,0,NULL,?)""",
              (game_id, team, opponent, drives, plays, giveaways, giveaways, 0,
               giveaways / plays, trips, touchdowns, touchdowns / trips, NOW))
            connection.commit()


class ActualsTests(ScoringFixture):
    def test_turnover_and_red_zone_rules_do_not_credit_a_defensive_score(self):
        self.game(1)
        self.play(game_id=1, drive="d1", offense="Michigan", defense="Ohio State",
                  yards_to_goal=8, play_type="Passing Touchdown", rush_pass="pass", scoring_play=1)
        self.play(game_id=1, drive="d2", offense="Michigan", defense="Ohio State",
                  yards_to_goal=12, play_type="Interception Return Touchdown",
                  rush_pass="pass", scoring_play=1)
        self.play(game_id=1, drive="d3", offense="Ohio State", defense="Michigan",
                  play_type="Fumble Recovery (Opponent)")
        result = scoring.build(self.repository)
        self.assertEqual(result["team_game_rows"], 2)
        rows = {row["team"]: row for row in scoring.game_summary(self.repository, 1)}
        michigan = rows["Michigan"]
        self.assertEqual(michigan["giveaways"], 1)
        self.assertEqual(michigan["red_zone_trips"], 2)
        self.assertEqual(michigan["red_zone_touchdowns"], 1)
        self.assertEqual(rows["Ohio State"]["fumbles_lost"], 1)

    def test_garbage_time_event_does_not_enter_model_actuals(self):
        self.game(1)
        self.play(game_id=1, drive="d1", offense="Michigan", defense="Ohio State",
                  yards_to_goal=10, play_type="Pass Interception Return", rush_pass="pass",
                  garbage=1)
        scoring.build(self.repository)
        row = scoring.game_summary(self.repository, 1)[0]
        self.assertEqual(row["giveaways"], 0)
        self.assertEqual(row["red_zone_trips"], 0)


class DatasetTests(ScoringFixture):
    def test_current_game_is_excluded_from_both_trailing_datasets(self):
        self.game(1, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Ohio State", week=2, start_date="2026-09-06")
        self.actual(game_id=1, team="Michigan", opponent="Ohio State", giveaways=0, trips=2, touchdowns=1)
        self.actual(game_id=1, team="Ohio State", opponent="Michigan", giveaways=2, trips=4, touchdowns=3)
        self.actual(game_id=2, team="Michigan", opponent="Ohio State", giveaways=3, trips=5, touchdowns=4)
        self.actual(game_id=2, team="Ohio State", opponent="Michigan", giveaways=1, trips=1, touchdowns=0)
        xturnovers.build_dataset(self.repository)
        xredzone.build_dataset(self.repository)
        with self.repository._reader() as connection:
            turnover = dict(connection.execute("""SELECT * FROM cfb_xturnovers_dataset
              WHERE game_id=2 AND team='Michigan'""").fetchone())
            redzone = dict(connection.execute("""SELECT * FROM cfb_xredzone_dataset
              WHERE game_id=2 AND team='Michigan'""").fetchone())
        self.assertEqual(turnover["team_prior_games"], 1)
        self.assertEqual(turnover["team_prior_giveaways"], 0)
        self.assertAlmostEqual(redzone["team_prior_trips_per_drive"], .2)
        self.assertAlmostEqual(redzone["opponent_prior_trips_allowed_per_drive"], .2)

    def test_live_projection_shrinks_turnovers_and_projects_red_zone_opportunity(self):
        self.game(1, week=1, start_date="2026-08-30")
        self.actual(game_id=1, team="Michigan", opponent="Ohio State", giveaways=0, trips=2, touchdowns=1)
        self.actual(game_id=1, team="Ohio State", opponent="Michigan", giveaways=2, trips=4, touchdowns=3)
        home = game_projection.team_scoring_snapshot(
            self.repository, "Michigan", before_date="2026-09-06")
        away = game_projection.team_scoring_snapshot(
            self.repository, "Ohio State", before_date="2026-09-06")
        self.assertEqual(home["games"], 1)
        self.assertAlmostEqual(home["giveaway_rate"], 0)
        self.assertAlmostEqual(home["giveaway_rate_allowed"], 2 / 60)
        self.assertAlmostEqual(away["trips_per_drive_allowed"], .2)

    def test_live_side_uses_matchup_trips_but_league_turnover_and_td_rates(self):
        offense = {"drives": 10.0, "plays_per_drive": 6.0, "pass_rate": .5,
                   "yards_per_dropback": 7.0, "yards_per_rush": 4.0}
        defense = {"drives_allowed": 10.0, "plays_per_drive_allowed": 6.0,
                   "pass_rate_allowed": .5, "yards_per_dropback_allowed": 7.0,
                   "yards_per_rush_allowed": 4.0}
        scoring_offense = {"games": 8, "giveaway_rate": .01,
                           "trips_per_drive": .30, "red_zone_td_rate": .80}
        scoring_defense = {"games": 8, "giveaway_rate_allowed": .03,
                           "trips_per_drive_allowed": .20,
                           "red_zone_td_rate_allowed": .70}
        side = game_projection._project_side(
            offense, defense, scoring_offense, scoring_defense,
            {"giveaway_rate": .02, "red_zone_td_rate": .60})
        self.assertAlmostEqual(side["giveaways"], 1.2)
        self.assertAlmostEqual(side["red_zone_trips"], 2.5)
        self.assertAlmostEqual(side["red_zone_touchdowns"], 1.5)
        self.assertAlmostEqual(side["matchup_turnover_rate"], .02)
        self.assertAlmostEqual(side["matchup_red_zone_td_rate"], .75)


if __name__ == "__main__":
    unittest.main()
