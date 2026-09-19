"""Leak-safety and combination math for the live game_projection module."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import game_projection as gp
from sports_aggregator.cfb import team_game_pace as tgp
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


class ProjectionFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        tgp.initialize(self.repository)

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

    def pace(self, *, game_id, team, opponent, drives=10, plays_per_drive=6.0, pass_rate=0.5,
             yards_per_dropback=7.0, yards_per_rush=4.0):
        scrimmage_plays = round(drives * plays_per_drive)
        pass_plays = round(scrimmage_plays * pass_rate)
        rush_plays = scrimmage_plays - pass_plays
        pass_yards = round(pass_plays * yards_per_dropback)
        rush_yards = round(rush_plays * yards_per_rush)
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_team_game_pace(game_id,team,opponent,metric_version,
                raw_drives,meaningful_drives,ot_drives,three_and_out_drives,scrimmage_plays,
                pass_plays,rush_plays,pass_yards,rush_yards,yards_per_dropback,yards_per_rush,
                plays_per_meaningful_drive,seconds_per_play,neutral_seconds_per_play,
                pass_rate,neutral_pass_rate,success_rate,explosive_rate,first_down_rate,three_and_out_rate,
                tempo_intervals,neutral_tempo_intervals,built_at)
                VALUES(?,?,?,?,?,?,0,0,?,?,?,?,?,?,?,?,25.0,25.0,?,?,0.45,0.1,0.3,0.2,10,10,?)""",
                (game_id, team, opponent, tgp.METRIC_VERSION, drives, drives, scrimmage_plays,
                 pass_plays, rush_plays, pass_yards, rush_yards, yards_per_dropback, yards_per_rush,
                 plays_per_drive, pass_rate, pass_rate, NOW))
            c.commit()


class TeamSnapshotTests(ProjectionFixture):
    def test_cold_start_returns_none_filled_snapshot_not_an_exception(self):
        snapshot = gp.team_snapshot(self.repository, "Michigan", before_date="2026-08-30")
        self.assertEqual(snapshot["games"], 0)
        self.assertIsNone(snapshot["drives"])
        self.assertIsNone(snapshot["drives_allowed"])

    def test_as_of_date_excludes_the_named_date_and_everything_after(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=9)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", drives=14)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", drives=8)

        # As of week 2's own kickoff, only week 1 should count -- the same
        # leak-safety guarantee the batch xdrives dataset enforces.
        before_week2 = gp.team_snapshot(self.repository, "Michigan", before_date="2026-09-06")
        self.assertEqual(before_week2["games"], 1)
        self.assertAlmostEqual(before_week2["drives"], 10.0)

        # As of a later date, week 2 has joined the trailing window too.
        after_week2 = gp.team_snapshot(self.repository, "Michigan", before_date="2026-09-13")
        self.assertEqual(after_week2["games"], 2)

    def test_allowed_mirror_reads_the_opponents_own_value_that_game(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10, yards_per_dropback=6.0)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=13, yards_per_dropback=9.0)
        snapshot = gp.team_snapshot(self.repository, "Michigan", before_date="2026-09-06")
        self.assertAlmostEqual(snapshot["drives_allowed"], 13.0)
        self.assertAlmostEqual(snapshot["yards_per_dropback_allowed"], 9.0)


class ProjectMatchupTests(ProjectionFixture):
    def test_combination_matches_the_documented_baseline_c_chain_by_hand(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Rutgers", away="Purdue", season=2026, week=1, start_date="2026-08-30")
        self.game(3, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10, plays_per_drive=6.0,
                 pass_rate=0.5, yards_per_dropback=7.0, yards_per_rush=4.0)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=11, plays_per_drive=6.5,
                 pass_rate=0.55, yards_per_dropback=8.0, yards_per_rush=4.5)
        self.pace(game_id=2, team="Rutgers", opponent="Purdue", drives=12, plays_per_drive=5.5,
                 pass_rate=0.45, yards_per_dropback=6.5, yards_per_rush=3.5)
        self.pace(game_id=2, team="Purdue", opponent="Rutgers", drives=9, plays_per_drive=6.0,
                 pass_rate=0.5, yards_per_dropback=7.5, yards_per_rush=4.0)

        projection = gp.project_matchup(self.repository, "Michigan", "Rutgers", as_of_date="2026-09-06")
        self.assertFalse(projection["insufficient_data"])

        home_snap, away_snap = projection["home_snapshot"], projection["away_snapshot"]
        # Michigan (home, offense) vs Rutgers (away, defense-allowed):
        expected_drives = (home_snap["drives"] + away_snap["drives_allowed"]) / 2
        expected_ppd = (home_snap["plays_per_drive"] + away_snap["plays_per_drive_allowed"]) / 2
        expected_pass_rate = (home_snap["pass_rate"] + away_snap["pass_rate_allowed"]) / 2
        expected_plays = expected_drives * expected_ppd
        expected_dropbacks = expected_plays * expected_pass_rate
        expected_rushes = expected_plays * (1 - expected_pass_rate)
        expected_ypd = (home_snap["yards_per_dropback"] + away_snap["yards_per_dropback_allowed"]) / 2
        expected_ypr = (home_snap["yards_per_rush"] + away_snap["yards_per_rush_allowed"]) / 2

        home_side = projection["home"]
        self.assertAlmostEqual(home_side["drives"], round(expected_drives, 1))
        self.assertAlmostEqual(home_side["plays"], round(expected_plays, 1))
        self.assertAlmostEqual(home_side["plays_per_drive"], round(expected_ppd, 2))
        self.assertAlmostEqual(home_side["dropbacks"], round(expected_dropbacks, 1))
        self.assertAlmostEqual(home_side["rush_attempts"], round(expected_rushes, 1))
        self.assertAlmostEqual(home_side["yards_per_dropback"], round(expected_ypd, 2))
        self.assertAlmostEqual(home_side["yards_per_rush"], round(expected_ypr, 2))
        self.assertAlmostEqual(home_side["pass_yards"], round(expected_dropbacks * expected_ypd, 1))
        self.assertAlmostEqual(home_side["rush_yards"], round(expected_rushes * expected_ypr, 1))
        self.assertAlmostEqual(
            home_side["total_yards"],
            round(round(expected_dropbacks * expected_ypd, 1) + round(expected_rushes * expected_ypr, 1), 1))

    def test_the_games_own_result_never_leaks_into_its_own_projection(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=11)
        # Project game 1 as of its own kickoff: neither team has any prior
        # history, so this must come back as a cold start, never using
        # game 1's own (10, 11) result to project game 1 itself.
        projection = gp.project_matchup(self.repository, "Michigan", "Ohio State", as_of_date="2026-08-30")
        self.assertTrue(projection["insufficient_data"])
        self.assertIsNone(projection["home"]["drives"])

    def test_insufficient_data_flag_and_missing_side(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=11)
        # "Iowa" has never played in this database at all.
        projection = gp.project_matchup(self.repository, "Michigan", "Iowa", as_of_date="2026-09-06")
        self.assertTrue(projection["insufficient_data"])
        self.assertIsNone(projection["away"]["drives"])
        # Michigan's own side is still fully computable even though Iowa's isn't.
        self.assertIsNotNone(projection["home_snapshot"]["drives"])


class NarrativeTests(ProjectionFixture):
    def test_narrative_reports_a_line_per_team(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=11)
        projection = gp.project_matchup(self.repository, "Michigan", "Ohio State", as_of_date="2026-09-06")
        lines = gp.narrative(projection)
        self.assertEqual(len(lines), 2)
        self.assertIn("Michigan", lines[0])
        self.assertIn("Ohio State", lines[1])

    def test_narrative_reports_cold_start_plainly_instead_of_crashing(self):
        projection = gp.project_matchup(self.repository, "Michigan", "Ohio State", as_of_date="2026-08-30")
        lines = gp.narrative(projection)
        self.assertEqual(len(lines), 2)
        self.assertIn("Not enough trailing data", lines[0])


if __name__ == "__main__":
    unittest.main()
