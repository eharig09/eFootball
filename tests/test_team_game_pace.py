"""Meaningful-drive classification and pace aggregation for cfb_team_game_pace."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import team_game_pace as tgp
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


class PaceFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        tgp.initialize(self.repository)
        self.play = 0

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def game(self, game_id, *, home, away, season=2026, week=1):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
                start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
                away_team_id,away_team,updated_at) VALUES(?,?,?,'regular',?,0,1,0,0,1,?,2,?,?)""",
                (game_id, season, week, NOW, home, away, NOW))
            c.commit()

    def play_(self, *, game_id, drive_id, offense, defense, down, distance=10, yards_gained=5,
              period=1, clock=(10, 0), play_type="Rush", play_text="run",
              garbage=0, success=1, explosive=0, rush_pass="rush", scoring=0):
        self.play += 1
        pid = f"p{self.play}"
        minutes, seconds = clock
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_plays(play_id,game_id,drive_id,drive_number,play_number,
                offense,defense,home_team,away_team,period,clock_minutes,clock_seconds,
                offense_score,defense_score,down,distance,yards_gained,scoring,play_type,play_text,
                season,week,raw_json,imported_at) VALUES(?,?,?,1,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?,?,2026,1,'{}',?)""",
                (pid, game_id, drive_id, self.play, offense, defense, offense, defense, period,
                 minutes, seconds, down, distance, yards_gained, scoring, play_type, play_text, NOW))
            c.execute("""INSERT INTO cfb_play_metrics(play_id,metric_version,rush_pass,success,
                explosive,garbage_time,derived_at) VALUES(?,'pbp-v1',?,?,?,?,?)""",
                (pid, rush_pass, success, explosive, garbage, NOW))
            c.commit()

    def drive_metric(self, *, game_id, drive_id, offense, defense, points=0, plays=1, scrimmage_plays=1):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_drive_metrics(game_id,drive_id,metric_version,offense,defense,
                plays,scrimmage_plays,yards,start_yards_to_goal,end_yards_to_goal,points,
                scoring_opportunity,success_rate,explosive_plays,havoc_allowed,derived_at)
                VALUES(?,?,'pbp-v1',?,?,?,?,0,75,70,?,0,0.5,0,0,?)""",
                (game_id, drive_id, offense, defense, plays, scrimmage_plays, points, NOW))
            c.commit()


class MeaningfulDriveTests(PaceFixture):
    def test_normal_drives_count_as_meaningful(self):
        self.game(1, home="Michigan", away="Ohio State")
        for i in range(5):
            self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State",
                       down=1, clock=(10, 30 - i * 25))
        self.drive_metric(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", points=7)
        result = tgp.build(self.repository)
        self.assertEqual(result["team_game_rows"], 1)
        row = tgp.game_summary(self.repository, 1)[0]
        self.assertEqual(row["team"], "Michigan")
        self.assertEqual(row["opponent"], "Ohio State")
        self.assertEqual(row["raw_drives"], 1)
        self.assertEqual(row["meaningful_drives"], 1)
        self.assertEqual(row["ot_drives"], 0)

    def test_all_kneel_drive_is_excluded_from_meaningful(self):
        self.game(1, home="Michigan", away="Ohio State")
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=1,
                   period=1, clock=(10, 30))
        self.drive_metric(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State")
        # A victory-formation drive: every down-play is a kneel.
        self.play_(game_id=1, drive_id="d2", offense="Michigan", defense="Ohio State", down=1,
                   period=2, clock=(0, 40), play_text="Q.Back kneels at the MICH 35.",
                   yards_gained=-1, success=0)
        self.play_(game_id=1, drive_id="d2", offense="Michigan", defense="Ohio State", down=2,
                   period=2, clock=(0, 5), play_text="Q.Back kneels at the MICH 34.",
                   yards_gained=-1, success=0)
        self.drive_metric(game_id=1, drive_id="d2", offense="Michigan", defense="Ohio State")
        tgp.build(self.repository)
        row = tgp.game_summary(self.repository, 1)[0]
        self.assertEqual(row["raw_drives"], 2)
        self.assertEqual(row["meaningful_drives"], 1, "the all-kneel drive must not count")

    def test_a_real_drive_that_ends_in_a_kneel_still_counts(self):
        self.game(1, home="Michigan", away="Ohio State")
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=1,
                   period=4, clock=(2, 0), yards_gained=12, distance=10)
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=1,
                   period=4, clock=(1, 40), yards_gained=4, distance=10)
        # Clock-out kneel after the first down was already converted.
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=2,
                   period=4, clock=(1, 20), play_text="Q.Back kneels at the OSU 40.",
                   yards_gained=-1, success=0)
        self.drive_metric(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State")
        tgp.build(self.repository)
        row = tgp.game_summary(self.repository, 1)[0]
        self.assertEqual(row["meaningful_drives"], 1)

    def test_overtime_drives_are_tracked_separately(self):
        self.game(1, home="Michigan", away="Ohio State")
        self.play_(game_id=1, drive_id="ot1", offense="Michigan", defense="Ohio State", down=1,
                   period=5, clock=(0, 0), scoring=1, play_type="Rushing Touchdown")
        self.drive_metric(game_id=1, drive_id="ot1", offense="Michigan", defense="Ohio State", points=7)
        tgp.build(self.repository)
        row = tgp.game_summary(self.repository, 1)[0]
        self.assertEqual(row["raw_drives"], 1)
        self.assertEqual(row["meaningful_drives"], 0)
        self.assertEqual(row["ot_drives"], 1)

    def test_a_drive_with_only_special_teams_markers_is_not_a_possession(self):
        self.game(1, home="Michigan", away="Ohio State")
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=None,
                   play_type="Kickoff", play_text="kickoff", period=1, clock=(15, 0))
        self.drive_metric(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State")
        tgp.build(self.repository)
        row = tgp.game_summary(self.repository, 1)[0]
        self.assertEqual(row["meaningful_drives"], 0)


class ThreeAndOutTests(PaceFixture):
    def test_a_short_punted_drive_is_a_three_and_out(self):
        self.game(1, home="Michigan", away="Ohio State")
        for i in range(3):
            self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State",
                       down=i + 1, distance=10, yards_gained=2, period=1, clock=(10, 30 - i * 5),
                       success=0)
        self.drive_metric(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", points=0)
        tgp.build(self.repository)
        row = tgp.game_summary(self.repository, 1)[0]
        self.assertEqual(row["three_and_out_drives"], 1)
        self.assertEqual(row["three_and_out_rate"], 1.0)

    def test_a_quick_turnover_is_not_a_three_and_out(self):
        self.game(1, home="Michigan", away="Ohio State")
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=1,
                   distance=10, yards_gained=-5, period=1, clock=(10, 30),
                   play_type="Interception Return Touchdown", rush_pass="pass", success=0)
        self.drive_metric(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", points=0)
        tgp.build(self.repository)
        row = tgp.game_summary(self.repository, 1)[0]
        self.assertEqual(row["three_and_out_drives"], 0)


class RateTests(PaceFixture):
    def test_pass_rate_success_rate_and_tempo_over_meaningful_drives(self):
        self.game(1, home="Michigan", away="Ohio State")
        # Three plays, 25 seconds apart, two rushes then a pass; two successes.
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=1,
                   period=1, clock=(10, 0), rush_pass="rush", success=1)
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=2,
                   period=1, clock=(9, 35), rush_pass="rush", success=1)
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=3,
                   period=1, clock=(9, 10), rush_pass="pass", success=0)
        self.drive_metric(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State")
        tgp.build(self.repository)
        row = tgp.game_summary(self.repository, 1)[0]
        self.assertEqual(row["scrimmage_plays"], 3)
        self.assertAlmostEqual(row["pass_rate"], 1 / 3)
        self.assertAlmostEqual(row["success_rate"], 2 / 3)
        self.assertEqual(row["tempo_intervals"], 2)
        self.assertAlmostEqual(row["seconds_per_play"], 25.0)
        self.assertEqual(row["pass_plays"], 1)
        self.assertEqual(row["rush_plays"], 2)
        # Every play in this fixture defaults to 5 yards gained.
        self.assertEqual(row["pass_yards"], 5)
        self.assertEqual(row["rush_yards"], 10)
        self.assertAlmostEqual(row["yards_per_dropback"], 5.0)
        self.assertAlmostEqual(row["yards_per_rush"], 5.0)

    def test_garbage_time_plays_are_excluded_from_rates(self):
        self.game(1, home="Michigan", away="Ohio State")
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=1,
                   period=4, clock=(2, 0), rush_pass="pass", success=1, garbage=1)
        self.drive_metric(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State")
        tgp.build(self.repository)
        row = tgp.game_summary(self.repository, 1)[0]
        # The drive is still meaningful (it isn't OT or a kneel), but the one
        # play in it is garbage time, so no rate has anything to divide by.
        self.assertEqual(row["meaningful_drives"], 1)
        self.assertEqual(row["scrimmage_plays"], 0)
        self.assertIsNone(row["pass_rate"])


class TrendTests(PaceFixture):
    def test_team_weekly_trend_orders_by_week(self):
        self.game(1, home="Michigan", away="Ohio State", week=1)
        self.game(2, home="Michigan", away="Rutgers", week=2)
        self.play_(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State", down=1)
        self.drive_metric(game_id=1, drive_id="d1", offense="Michigan", defense="Ohio State")
        self.play_(game_id=2, drive_id="d1", offense="Michigan", defense="Rutgers", down=1)
        self.play_(game_id=2, drive_id="d2", offense="Michigan", defense="Rutgers", down=1)
        self.drive_metric(game_id=2, drive_id="d1", offense="Michigan", defense="Rutgers")
        self.drive_metric(game_id=2, drive_id="d2", offense="Michigan", defense="Rutgers")
        tgp.build(self.repository)
        trend = tgp.team_weekly_trend(self.repository, "Michigan", 2026)
        self.assertEqual([row["week"] for row in trend], [1, 2])
        self.assertEqual(trend[0]["meaningful_drives"], 1)
        self.assertEqual(trend[1]["meaningful_drives"], 2)


if __name__ == "__main__":
    unittest.main()
