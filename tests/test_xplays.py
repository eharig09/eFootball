"""Leak-safety and baseline math for the xPlaysPerDrive dataset (Milestone 5)."""
from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import team_game_pace as tgp
from sports_aggregator.cfb import xdrives
from sports_aggregator.cfb import xplays
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


def _decayed_mean(values_oldest_first):
    n = len(values_oldest_first)
    weights = [math.exp(-xplays.RECENCY_LAMBDA * (n - 1 - i)) for i in range(n)]
    return sum(w * v for w, v in zip(weights, values_oldest_first)) / sum(weights)


class XPlaysFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        tgp.initialize(self.repository)
        xplays.initialize(self.repository)

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

    def pace(self, *, game_id, team, opponent, plays_per_drive, meaningful_drives=10,
             success_rate=0.45, explosive_rate=0.1, first_down_rate=0.3, three_and_out_rate=0.2):
        scrimmage_plays = round(plays_per_drive * meaningful_drives)
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_team_game_pace(game_id,team,opponent,metric_version,
                raw_drives,meaningful_drives,ot_drives,three_and_out_drives,scrimmage_plays,
                plays_per_meaningful_drive,seconds_per_play,neutral_seconds_per_play,pass_rate,
                neutral_pass_rate,success_rate,explosive_rate,first_down_rate,three_and_out_rate,
                tempo_intervals,neutral_tempo_intervals,built_at)
                VALUES(?,?,?,?,?,?,0,0,?,?,25.0,25.0,0.5,0.5,?,?,?,?,10,10,?)""",
                (game_id, team, opponent, tgp.METRIC_VERSION, meaningful_drives, meaningful_drives,
                 scrimmage_plays, plays_per_drive, success_rate, explosive_rate,
                 first_down_rate, three_and_out_rate, NOW))
            c.commit()

    def row(self, game_id, team, dataset_version=xplays.DATASET_VERSION):
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM cfb_xplays_dataset WHERE game_id=? AND team=? AND dataset_version=?",
                (game_id, team, dataset_version)).fetchone()
        self.assertIsNotNone(row, f"no row for game {game_id} team {team}")
        return dict(row)


class LeakSafetyTests(XPlaysFixture):
    def test_trailing_plays_per_drive_excludes_current_and_future_games(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.game(3, home="Michigan", away="Iowa", season=2026, week=3, start_date="2026-09-13")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", plays_per_drive=5.0)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", plays_per_drive=6.0)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", plays_per_drive=6.0)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", plays_per_drive=5.0)
        self.pace(game_id=3, team="Michigan", opponent="Iowa", plays_per_drive=9.0)
        self.pace(game_id=3, team="Iowa", opponent="Michigan", plays_per_drive=5.0)
        xplays.build_dataset(self.repository)

        week3 = self.row(3, "Michigan")
        self.assertEqual(week3["team_prior_games"], 2)
        self.assertAlmostEqual(week3["team_prior_plays_per_drive"], _decayed_mean([5.0, 6.0]))
        self.assertAlmostEqual(week3["actual_plays_per_drive"], 9.0,
                               msg="week 3's own result must not leak into its own trailing average")

        week1 = self.row(1, "Michigan")
        self.assertEqual(week1["team_prior_games"], 0)
        self.assertIsNone(week1["team_prior_plays_per_drive"])

    def test_allowed_mirror_tracks_what_opponents_needed_against_this_defense(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", plays_per_drive=5.0)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", plays_per_drive=8.0)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", plays_per_drive=6.0)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", plays_per_drive=5.0)
        xplays.build_dataset(self.repository)
        week2 = self.row(2, "Michigan")
        # Ohio State needed 8 plays/drive against Michigan's defense in week 1;
        # that is what "allowed" should trend for Michigan going into week 2.
        self.assertAlmostEqual(week2["team_prior_plays_per_drive_allowed"], 8.0)


class RecencyTests(XPlaysFixture):
    def test_reuses_xdrives_tuned_half_life_by_default(self):
        self.assertEqual(xplays.RECENCY_LAMBDA, xdrives.RECENCY_LAMBDA)

    def test_infinite_half_life_recovers_the_flat_mean(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.game(3, home="Michigan", away="Iowa", season=2026, week=3, start_date="2026-09-13")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", plays_per_drive=5.0)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", plays_per_drive=6.0)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", plays_per_drive=7.0)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", plays_per_drive=6.0)
        self.pace(game_id=3, team="Michigan", opponent="Iowa", plays_per_drive=9.0)
        self.pace(game_id=3, team="Iowa", opponent="Michigan", plays_per_drive=6.0)
        xplays.build_dataset(self.repository, dataset_version="xplays-flat-test", half_life_games=math.inf)
        row = self.row(3, "Michigan", dataset_version="xplays-flat-test")
        self.assertAlmostEqual(row["team_prior_plays_per_drive"], (5.0 + 7.0) / 2)


class BaselineTests(XPlaysFixture):
    def _seed(self):
        dates = ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20"]
        opponents = ["Ohio State", "Rutgers", "Iowa", "Purdue"]
        team_ppd = [5.0, 6.0, 7.0, 4.0]
        opp_ppd = [6.0, 5.0, 6.5, 5.5]
        for i, (opponent, date) in enumerate(zip(opponents, dates)):
            game_id = i + 1
            self.game(game_id, home="Michigan", away=opponent, season=2026, week=i + 1, start_date=date)
            self.pace(game_id=game_id, team="Michigan", opponent=opponent, plays_per_drive=team_ppd[i])
            self.pace(game_id=game_id, team=opponent, opponent="Michigan", plays_per_drive=opp_ppd[i])
        xplays.build_dataset(self.repository)

    def test_cold_start_rows_are_dropped_and_counted(self):
        self._seed()
        result = xplays.evaluate_baselines(self.repository)
        self.assertGreaterEqual(result["rows_dropped_cold_start"], 5)
        self.assertEqual(result["overall"]["B_team_average"]["games"], 3)

    def test_baseline_b_mae_matches_hand_computed_trailing_average_error(self):
        self._seed()
        result = xplays.evaluate_baselines(self.repository)
        predictions = [_decayed_mean([5.0]), _decayed_mean([5.0, 6.0]), _decayed_mean([5.0, 6.0, 7.0])]
        actuals = [6.0, 7.0, 4.0]
        expected_mae = sum(abs(p - a) for p, a in zip(predictions, actuals)) / 3
        self.assertAlmostEqual(result["overall"]["B_team_average"]["mae"], expected_mae, places=4)


class ExpectedPlaysTests(XPlaysFixture):
    def test_combined_projection_matches_hand_computed_multiplication(self):
        xdrives.initialize(self.repository)
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        # Rutgers needs its own prior game too, or its opponent_prior_*_allowed
        # (its own trailing history as a defense) stays undefined and week 2
        # below never reaches "matched".
        self.game(2, home="Rutgers", away="Purdue", season=2026, week=1, start_date="2026-08-30")
        self.game(3, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", plays_per_drive=5.0, meaningful_drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", plays_per_drive=6.0, meaningful_drives=11)
        self.pace(game_id=2, team="Rutgers", opponent="Purdue", plays_per_drive=6.0, meaningful_drives=13)
        self.pace(game_id=2, team="Purdue", opponent="Rutgers", plays_per_drive=5.0, meaningful_drives=8)
        self.pace(game_id=3, team="Michigan", opponent="Rutgers", plays_per_drive=7.0, meaningful_drives=12)
        self.pace(game_id=3, team="Rutgers", opponent="Michigan", plays_per_drive=5.0, meaningful_drives=9)
        xplays.build_dataset(self.repository)
        xdrives.build_dataset(self.repository)

        result = xplays.evaluate_expected_plays(self.repository)
        # Game 3 (Michigan vs Rutgers) is the only game where BOTH sides
        # already have their own trailing history, and both perspectives of
        # that one game satisfy the "matched" criteria independently.
        self.assertEqual(result["rows_matched"], 2)

        errors = []
        for team in ("Michigan", "Rutgers"):
            row = self.row(3, team)
            with closing(sqlite3.connect(self.path)) as c:
                c.row_factory = sqlite3.Row
                drive_row = dict(c.execute(
                    "SELECT * FROM cfb_xdrives_dataset WHERE game_id=3 AND team=?", (team,)).fetchone())
            expected_drives = (drive_row["team_prior_drives"] + drive_row["opponent_prior_drives_allowed"]) / 2
            expected_ppd = (row["team_prior_plays_per_drive"] + row["opponent_prior_plays_per_drive_allowed"]) / 2
            errors.append(abs(expected_drives * expected_ppd - row["actual_scrimmage_plays"]))
        self.assertAlmostEqual(result["combined_matchup_blend"]["mae"], sum(errors) / len(errors))


if __name__ == "__main__":
    unittest.main()
