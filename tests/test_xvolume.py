"""Leak-safety and baseline math for the xVolume (run/pass split) dataset (Milestone 6)."""
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
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


def _decayed_mean(values_oldest_first):
    n = len(values_oldest_first)
    weights = [math.exp(-xvolume.RECENCY_LAMBDA * (n - 1 - i)) for i in range(n)]
    return sum(w * v for w, v in zip(weights, values_oldest_first)) / sum(weights)


class XVolumeFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        tgp.initialize(self.repository)
        xvolume.initialize(self.repository)
        cfb_lines.initialize(self.repository)

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def game(self, game_id, *, home, away, season, week, start_date, home_elo=1500, away_elo=1500):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
                start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
                home_pregame_elo,away_team_id,away_team,away_pregame_elo,updated_at)
                VALUES(?,?,?,'regular',?,0,1,0,0,1,?,?,2,?,?,?)""",
                (game_id, season, week, start_date, home, home_elo, away, away_elo, NOW))
            c.commit()

    def pace(self, *, game_id, team, opponent, pass_rate, neutral_pass_rate=None, scrimmage_plays=60):
        neutral_pass_rate = pass_rate if neutral_pass_rate is None else neutral_pass_rate
        pass_plays = round(pass_rate * scrimmage_plays)
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_team_game_pace(game_id,team,opponent,metric_version,
                raw_drives,meaningful_drives,ot_drives,three_and_out_drives,scrimmage_plays,
                pass_plays,rush_plays,plays_per_meaningful_drive,seconds_per_play,neutral_seconds_per_play,
                pass_rate,neutral_pass_rate,success_rate,explosive_rate,first_down_rate,three_and_out_rate,
                tempo_intervals,neutral_tempo_intervals,built_at)
                VALUES(?,?,?,?,10,10,0,0,?,?,?,6.0,25.0,25.0,?,?,0.45,0.1,0.3,0.2,10,10,?)""",
                (game_id, team, opponent, tgp.METRIC_VERSION, scrimmage_plays, pass_plays,
                 scrimmage_plays - pass_plays, pass_rate, neutral_pass_rate, NOW))
            c.commit()

    def lines(self, game_id, *, spread, total=50.0):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO game_lines(game_id,season,provider,spread,over_under,
                fetched_at) VALUES(?,2026,'TestBook',?,?,?)""", (game_id, spread, total, NOW))
            c.commit()

    def row(self, game_id, team, dataset_version=xvolume.DATASET_VERSION):
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM cfb_xvolume_dataset WHERE game_id=? AND team=? AND dataset_version=?",
                (game_id, team, dataset_version)).fetchone()
        self.assertIsNotNone(row, f"no row for game {game_id} team {team}")
        return dict(row)


class LeakSafetyTests(XVolumeFixture):
    def test_trailing_pass_rate_excludes_current_and_future_games(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.game(3, home="Michigan", away="Iowa", season=2026, week=3, start_date="2026-09-13")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", pass_rate=0.40)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", pass_rate=0.50)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", pass_rate=0.50)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", pass_rate=0.45)
        self.pace(game_id=3, team="Michigan", opponent="Iowa", pass_rate=0.70)
        self.pace(game_id=3, team="Iowa", opponent="Michigan", pass_rate=0.45)
        xvolume.build_dataset(self.repository)

        week3 = self.row(3, "Michigan")
        self.assertEqual(week3["team_prior_games"], 2)
        self.assertAlmostEqual(week3["team_prior_pass_rate"], _decayed_mean([0.40, 0.50]))
        self.assertAlmostEqual(week3["actual_pass_rate"], 0.70,
                               msg="week 3's own result must not leak into its own trailing average")

    def test_allowed_mirror_tracks_what_opponents_did_against_this_defense(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", pass_rate=0.40)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", pass_rate=0.65)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", pass_rate=0.50)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", pass_rate=0.45)
        xvolume.build_dataset(self.repository)
        week2 = self.row(2, "Michigan")
        # Ohio State passed at 0.65 against Michigan's defense in week 1;
        # that is what "allowed" should trend for Michigan going into week 2.
        self.assertAlmostEqual(week2["team_prior_pass_rate_allowed"], 0.65)


class MarketTests(XVolumeFixture):
    def test_market_spread_is_flipped_for_the_away_team(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", pass_rate=0.4)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", pass_rate=0.5)
        self.lines(1, spread=-7.0)  # home (Michigan) favored by 7
        xvolume.build_dataset(self.repository)
        home_row = self.row(1, "Michigan")
        away_row = self.row(1, "Ohio State")
        self.assertAlmostEqual(home_row["market_spread"], -7.0)
        self.assertAlmostEqual(away_row["market_spread"], 7.0)


class BaselineTests(XVolumeFixture):
    def _seed(self):
        dates = ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20"]
        opponents = ["Ohio State", "Rutgers", "Iowa", "Purdue"]
        team_rate = [0.40, 0.50, 0.55, 0.35]
        opp_rate = [0.50, 0.45, 0.60, 0.50]
        for i, (opponent, date) in enumerate(zip(opponents, dates)):
            game_id = i + 1
            self.game(game_id, home="Michigan", away=opponent, season=2026, week=i + 1, start_date=date)
            self.pace(game_id=game_id, team="Michigan", opponent=opponent, pass_rate=team_rate[i])
            self.pace(game_id=game_id, team=opponent, opponent="Michigan", pass_rate=opp_rate[i])
            self.lines(game_id, spread=-3.0)
        xvolume.build_dataset(self.repository)

    def test_cold_start_rows_are_dropped_and_counted(self):
        self._seed()
        result = xvolume.evaluate_baselines(self.repository)
        self.assertGreaterEqual(result["rows_dropped_cold_start"], 5)
        self.assertEqual(result["overall"]["B_team_average"]["games"], 3)

    def test_baseline_b_mae_matches_hand_computed_trailing_average_error(self):
        self._seed()
        result = xvolume.evaluate_baselines(self.repository)
        predictions = [_decayed_mean([0.40]), _decayed_mean([0.40, 0.50]), _decayed_mean([0.40, 0.50, 0.55])]
        actuals = [0.50, 0.55, 0.35]
        expected_mae = sum(abs(p - a) for p, a in zip(predictions, actuals)) / 3
        self.assertAlmostEqual(result["overall"]["B_team_average"]["mae"], expected_mae, places=4)


class ExpectedVolumeTests(XVolumeFixture):
    def test_combined_projection_splits_expected_plays_by_pass_rate(self):
        xdrives.initialize(self.repository)
        xplays.initialize(self.repository)
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Rutgers", away="Purdue", season=2026, week=1, start_date="2026-08-30")
        self.game(3, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", pass_rate=0.4, scrimmage_plays=60)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", pass_rate=0.5, scrimmage_plays=55)
        self.pace(game_id=2, team="Rutgers", opponent="Purdue", pass_rate=0.5, scrimmage_plays=65)
        self.pace(game_id=2, team="Purdue", opponent="Rutgers", pass_rate=0.45, scrimmage_plays=50)
        self.pace(game_id=3, team="Michigan", opponent="Rutgers", pass_rate=0.55, scrimmage_plays=70)
        self.pace(game_id=3, team="Rutgers", opponent="Michigan", pass_rate=0.45, scrimmage_plays=60)
        xvolume.build_dataset(self.repository)
        xplays.build_dataset(self.repository)
        xdrives.build_dataset(self.repository)

        result = xvolume.evaluate_expected_volume(self.repository)
        self.assertGreaterEqual(result["rows_matched"], 1)
        pass_stats = result["combined_matchup_blend"]["expected_dropbacks"]
        rush_stats = result["combined_matchup_blend"]["expected_rush_attempts"]
        self.assertGreater(pass_stats["games"], 0)
        self.assertEqual(pass_stats["games"], rush_stats["games"])


if __name__ == "__main__":
    unittest.main()
