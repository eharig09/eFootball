"""Leak-safety and baseline math for the xYards (expected yardage) dataset (spec section 20)."""
from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import lines as cfb_lines
from sports_aggregator.cfb import team_game_pace as tgp
from sports_aggregator.cfb import xdrives
from sports_aggregator.cfb import xplays
from sports_aggregator.cfb import xvolume
from sports_aggregator.cfb import xyards
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


def _decayed_mean(values_oldest_first):
    n = len(values_oldest_first)
    weights = [math.exp(-xyards.RECENCY_LAMBDA * (n - 1 - i)) for i in range(n)]
    return sum(w * v for w, v in zip(weights, values_oldest_first)) / sum(weights)


class XYardsFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        tgp.initialize(self.repository)
        xyards.initialize(self.repository)

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

    def pace(self, *, game_id, team, opponent, yards_per_dropback, yards_per_rush,
             pass_plays=30, rush_plays=30, success_rate=0.45, explosive_rate=0.1):
        pass_yards = round(yards_per_dropback * pass_plays)
        rush_yards = round(yards_per_rush * rush_plays)
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_team_game_pace(game_id,team,opponent,metric_version,
                raw_drives,meaningful_drives,ot_drives,three_and_out_drives,scrimmage_plays,
                pass_plays,rush_plays,pass_yards,rush_yards,yards_per_dropback,yards_per_rush,
                plays_per_meaningful_drive,seconds_per_play,neutral_seconds_per_play,
                pass_rate,neutral_pass_rate,success_rate,explosive_rate,first_down_rate,three_and_out_rate,
                tempo_intervals,neutral_tempo_intervals,built_at)
                VALUES(?,?,?,?,10,10,0,0,?,?,?,?,?,?,?,6.0,25.0,25.0,0.5,0.5,?,?,0.3,0.2,10,10,?)""",
                (game_id, team, opponent, tgp.METRIC_VERSION, pass_plays + rush_plays,
                 pass_plays, rush_plays, pass_yards, rush_yards, yards_per_dropback, yards_per_rush,
                 success_rate, explosive_rate, NOW))
            c.commit()

    def row(self, game_id, team, dataset_version=xyards.DATASET_VERSION):
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM cfb_xyards_dataset WHERE game_id=? AND team=? AND dataset_version=?",
                (game_id, team, dataset_version)).fetchone()
        self.assertIsNotNone(row, f"no row for game {game_id} team {team}")
        return dict(row)


class LeakSafetyTests(XYardsFixture):
    def test_trailing_yards_per_dropback_excludes_current_and_future_games(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.game(3, home="Michigan", away="Iowa", season=2026, week=3, start_date="2026-09-13")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", yards_per_dropback=6.0, yards_per_rush=4.0)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", yards_per_dropback=7.0, yards_per_rush=4.5)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", yards_per_dropback=8.0, yards_per_rush=4.5)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", yards_per_dropback=6.5, yards_per_rush=3.5)
        self.pace(game_id=3, team="Michigan", opponent="Iowa", yards_per_dropback=12.0, yards_per_rush=5.0)
        self.pace(game_id=3, team="Iowa", opponent="Michigan", yards_per_dropback=6.0, yards_per_rush=4.0)
        xyards.build_dataset(self.repository)

        week3 = self.row(3, "Michigan")
        self.assertEqual(week3["team_prior_games"], 2)
        self.assertAlmostEqual(week3["team_prior_yards_per_dropback"], _decayed_mean([6.0, 8.0]))
        self.assertAlmostEqual(week3["actual_yards_per_dropback"], 12.0,
                               msg="week 3's own result must not leak into its own trailing average")

    def test_allowed_mirror_is_separate_for_pass_and_rush(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", yards_per_dropback=6.0, yards_per_rush=4.0)
        # Ohio State was explosive through the air (9.0 ypd) but stuffed on the ground (2.0 ypr)
        # against Michigan's defense -- the two allowed trends must not blend into each other.
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", yards_per_dropback=9.0, yards_per_rush=2.0)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", yards_per_dropback=7.0, yards_per_rush=4.5)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", yards_per_dropback=6.0, yards_per_rush=4.0)
        xyards.build_dataset(self.repository)
        week2 = self.row(2, "Michigan")
        self.assertAlmostEqual(week2["team_prior_yards_per_dropback_allowed"], 9.0)
        self.assertAlmostEqual(week2["team_prior_yards_per_rush_allowed"], 2.0)


class BaselineTests(XYardsFixture):
    def _seed(self):
        dates = ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20"]
        opponents = ["Ohio State", "Rutgers", "Iowa", "Purdue"]
        team_ypd = [6.0, 7.0, 8.0, 5.0]
        opp_ypd = [7.0, 6.0, 9.0, 6.5]
        for i, (opponent, date) in enumerate(zip(opponents, dates)):
            game_id = i + 1
            self.game(game_id, home="Michigan", away=opponent, season=2026, week=i + 1, start_date=date)
            self.pace(game_id=game_id, team="Michigan", opponent=opponent,
                     yards_per_dropback=team_ypd[i], yards_per_rush=4.0)
            self.pace(game_id=game_id, team=opponent, opponent="Michigan",
                     yards_per_dropback=opp_ypd[i], yards_per_rush=4.0)
        xyards.build_dataset(self.repository)

    def test_cold_start_rows_are_dropped_and_counted(self):
        self._seed()
        result = xyards.evaluate_baselines(self.repository)
        self.assertGreaterEqual(result["passing"]["rows_dropped_cold_start"], 5)
        self.assertEqual(result["passing"]["overall"]["B_team_average"]["games"], 3)

    def test_baseline_b_mae_matches_hand_computed_trailing_average_error(self):
        self._seed()
        result = xyards.evaluate_baselines(self.repository)
        predictions = [_decayed_mean([6.0]), _decayed_mean([6.0, 7.0]), _decayed_mean([6.0, 7.0, 8.0])]
        actuals = [7.0, 8.0, 5.0]
        expected_mae = sum(abs(p - a) for p, a in zip(predictions, actuals)) / 3
        self.assertAlmostEqual(result["passing"]["overall"]["B_team_average"]["mae"], expected_mae, places=4)

    def test_passing_and_rushing_are_evaluated_independently(self):
        self._seed()
        result = xyards.evaluate_baselines(self.repository)
        self.assertIn("passing", result)
        self.assertIn("rushing", result)
        # Rush yards/rush is flat at 4.0 for every team in this fixture, so
        # a perfectly-informed trailing average should have ~zero error,
        # unlike the varying passing numbers.
        self.assertLess(result["rushing"]["overall"]["B_team_average"]["mae"],
                        result["passing"]["overall"]["B_team_average"]["mae"])


class ExpectedYardageTests(XYardsFixture):
    def test_combined_projection_runs_end_to_end(self):
        xdrives.initialize(self.repository)
        xplays.initialize(self.repository)
        xvolume.initialize(self.repository)
        cfb_lines.initialize(self.repository)
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Rutgers", away="Purdue", season=2026, week=1, start_date="2026-08-30")
        self.game(3, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", yards_per_dropback=6.0, yards_per_rush=4.0)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", yards_per_dropback=7.0, yards_per_rush=4.5)
        self.pace(game_id=2, team="Rutgers", opponent="Purdue", yards_per_dropback=7.5, yards_per_rush=3.5)
        self.pace(game_id=2, team="Purdue", opponent="Rutgers", yards_per_dropback=6.5, yards_per_rush=4.2)
        self.pace(game_id=3, team="Michigan", opponent="Rutgers", yards_per_dropback=8.0, yards_per_rush=5.0)
        self.pace(game_id=3, team="Rutgers", opponent="Michigan", yards_per_dropback=6.0, yards_per_rush=3.8)
        xyards.build_dataset(self.repository)
        xvolume.build_dataset(self.repository)
        xplays.build_dataset(self.repository)
        xdrives.build_dataset(self.repository)

        result = xyards.evaluate_expected_yardage(self.repository)
        self.assertGreaterEqual(result["rows_matched"], 1)
        combined = result["combined_matchup_blend"]
        self.assertGreater(combined["expected_pass_yards"]["games"], 0)
        self.assertEqual(combined["expected_pass_yards"]["games"], combined["expected_rush_yards"]["games"])


if __name__ == "__main__":
    unittest.main()
