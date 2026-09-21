"""Leak-safety and baseline math for the xDrives dataset."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import lines as cfb_lines
from sports_aggregator.cfb import team_game_pace as tgp
from sports_aggregator.cfb import xdrives
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


def _decayed_mean(values_oldest_first):
    """The same exponential-decay mean `xdrives._trailing_summary` computes,
    spelled out independently so tests read as a check on the documented
    formula (`weight = exp(-lambda * gamesAgo)`) rather than a tautology."""
    n = len(values_oldest_first)
    weights = [math.exp(-xdrives.RECENCY_LAMBDA * (n - 1 - i)) for i in range(n)]
    return sum(w * v for w, v in zip(weights, values_oldest_first)) / sum(weights)


class XDrivesFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        tgp.initialize(self.repository)
        xdrives.initialize(self.repository)
        cfb_lines.initialize(self.repository)

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def game(self, game_id, *, home, away, season, week, start_date,
             home_elo=1500, away_elo=1500):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
                start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
                home_pregame_elo,away_team_id,away_team,away_pregame_elo,updated_at)
                VALUES(?,?,?,'regular',?,0,1,0,0,1,?,?,2,?,?,?)""",
                (game_id, season, week, start_date, home, home_elo, away, away_elo, NOW))
            c.commit()

    def pace(self, *, game_id, team, opponent, drives, seconds_per_play=25.0, pass_rate=0.5,
             success_rate=0.45, explosive_rate=0.1, first_down_rate=0.3, three_and_out_rate=0.2):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_team_game_pace(game_id,team,opponent,metric_version,
                raw_drives,meaningful_drives,ot_drives,three_and_out_drives,scrimmage_plays,
                plays_per_meaningful_drive,seconds_per_play,neutral_seconds_per_play,pass_rate,
                neutral_pass_rate,success_rate,explosive_rate,first_down_rate,three_and_out_rate,
                tempo_intervals,neutral_tempo_intervals,built_at)
                VALUES(?,?,?,?,?,?,0,0,?,?,?,?,?,?,?,?,?,?,10,10,?)""",
                (game_id, team, opponent, tgp.METRIC_VERSION, drives, drives, drives * 6,
                 6.0, seconds_per_play, seconds_per_play, pass_rate, pass_rate,
                 success_rate, explosive_rate, first_down_rate, three_and_out_rate, NOW))
            c.commit()

    def lines(self, game_id, *, spread, total):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO game_lines(game_id,season,provider,spread,over_under,
                fetched_at) VALUES(?,2026,'TestBook',?,?,?)""", (game_id, spread, total, NOW))
            c.commit()

    def _seed_round_robin(self, *, season=2026, start_week=1, start_date="2026-08-30"):
        """Michigan alternates home/away against three opponents for six weeks.

        Every game carries full pace, Elo and market data so every feature
        `xdrives.ADVANCED_FEATURES` needs is populated once a team has at
        least one prior game -- enough rows for the ridge fit to be
        well-posed without hand-deriving an exact expected coefficient.
        """
        from datetime import date, timedelta
        opponents = ["Ohio State", "Rutgers", "Iowa", "Purdue", "Ohio State", "Rutgers"]
        team_drives = [10, 12, 9, 14, 11, 13]
        opp_drives = [11, 8, 12, 9, 10, 12]
        base = date.fromisoformat(start_date)
        for i, opponent in enumerate(opponents):
            game_id = season * 10000 + 100 * (start_week + i)
            week = start_week + i
            game_date = (base + timedelta(days=7 * i)).isoformat()
            home = "Michigan" if i % 2 == 0 else opponent
            away = opponent if i % 2 == 0 else "Michigan"
            self.game(game_id, home=home, away=away, season=season, week=week, start_date=game_date,
                      home_elo=1600 + i * 5, away_elo=1550 - i * 3)
            self.pace(game_id=game_id, team="Michigan", opponent=opponent, drives=team_drives[i],
                      seconds_per_play=24.0 + i, pass_rate=0.45 + i * 0.01, success_rate=0.44,
                      explosive_rate=0.09, first_down_rate=0.31, three_and_out_rate=0.18)
            self.pace(game_id=game_id, team=opponent, opponent="Michigan", drives=opp_drives[i],
                      seconds_per_play=26.0 - i, pass_rate=0.5, success_rate=0.42,
                      explosive_rate=0.1, first_down_rate=0.29, three_and_out_rate=0.2)
            self.lines(game_id, spread=-3.5, total=52.0 + i)


class LeakSafetyTests(XDrivesFixture):
    def test_first_game_has_no_trailing_history(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=11)
        xdrives.build_dataset(self.repository)
        row = self._row(1, "Michigan")
        self.assertEqual(row["team_prior_games"], 0)
        self.assertIsNone(row["team_prior_drives"])
        self.assertEqual(row["actual_meaningful_drives"], 10)
        self.assertEqual(row["actual_game_total_drives"], 21)

    def test_trailing_average_excludes_the_current_and_future_games(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.game(3, home="Michigan", away="Iowa", season=2026, week=3, start_date="2026-09-13")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=9)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", drives=12)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", drives=8)
        self.pace(game_id=3, team="Michigan", opponent="Iowa", drives=14)
        self.pace(game_id=3, team="Iowa", opponent="Michigan", drives=7)
        xdrives.build_dataset(self.repository)

        week3 = self._row(3, "Michigan")
        self.assertEqual(week3["team_prior_games"], 2)
        self.assertAlmostEqual(week3["team_prior_drives"], _decayed_mean([10, 12]))
        self.assertEqual(week3["actual_meaningful_drives"], 14, "week 3's own result must not leak into its own average")

        week2 = self._row(2, "Michigan")
        self.assertEqual(week2["team_prior_games"], 1)
        self.assertAlmostEqual(week2["team_prior_drives"], 10)

    def test_drives_allowed_tracks_what_opponents_did_against_this_defense(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Michigan", away="Rutgers", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=13)
        self.pace(game_id=2, team="Michigan", opponent="Rutgers", drives=12)
        self.pace(game_id=2, team="Rutgers", opponent="Michigan", drives=9)
        xdrives.build_dataset(self.repository)
        week2 = self._row(2, "Michigan")
        # Ohio State (Michigan's week-1 opponent) got 13 meaningful drives
        # against Michigan's defense; that is what "allowed" should trend.
        self.assertAlmostEqual(week2["team_prior_drives_allowed"], 13)

    def _row(self, game_id, team):
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM cfb_xdrives_dataset WHERE game_id=? AND team=?",
                             (game_id, team)).fetchone()
        self.assertIsNotNone(row, f"no dataset row for game {game_id} team {team}")
        return dict(row)


class RecencyWeightingTests(XDrivesFixture):
    def setUp(self):
        super().setUp()
        dates = ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20"]
        drives = [10, 10, 20]  # two flat games, then a sharp jump right before the observed game
        for i, d in enumerate(drives):
            self.game(i + 1, home="Michigan", away="Rutgers", season=2026, week=i + 1, start_date=dates[i])
            self.pace(game_id=i + 1, team="Michigan", opponent="Rutgers", drives=d)
            self.pace(game_id=i + 1, team="Rutgers", opponent="Michigan", drives=10)
        self.game(4, home="Michigan", away="Iowa", season=2026, week=4, start_date=dates[3])
        self.pace(game_id=4, team="Michigan", opponent="Iowa", drives=15)
        self.pace(game_id=4, team="Iowa", opponent="Michigan", drives=10)

    def _row(self, dataset_version):
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM cfb_xdrives_dataset WHERE game_id=4 AND team='Michigan' AND dataset_version=?",
                (dataset_version,)).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def test_the_most_recent_game_pulls_the_trailing_mean_toward_it(self):
        xdrives.build_dataset(self.repository)
        row = self._row(xdrives.DATASET_VERSION)
        flat_mean = (10 + 10 + 20) / 3
        # The most recent prior game (20) is the odd one out after two 10s;
        # decay weighting should pull the trailing mean above the flat mean,
        # toward that most recent value, not leave it exactly at the flat mean.
        self.assertGreater(row["team_prior_drives"], flat_mean)
        self.assertAlmostEqual(row["team_prior_drives"], _decayed_mean([10, 10, 20]))

    def test_infinite_half_life_recovers_the_flat_mean(self):
        xdrives.build_dataset(self.repository, dataset_version="xdrives-dataset-flat-test",
                              half_life_games=math.inf)
        row = self._row("xdrives-dataset-flat-test")
        self.assertAlmostEqual(row["team_prior_drives"], (10 + 10 + 20) / 3)

    def test_v1_and_v2_dataset_versions_coexist_independently(self):
        # v1 (flat mean) was never deleted when v2 (decay-weighted) shipped;
        # both can be built into the same table and read back separately.
        xdrives.build_dataset(self.repository)
        xdrives.build_dataset(self.repository, dataset_version="xdrives-dataset-v1", half_life_games=math.inf)
        weighted = self._row(xdrives.DATASET_VERSION)
        flat = self._row("xdrives-dataset-v1")
        self.assertNotAlmostEqual(weighted["team_prior_drives"], flat["team_prior_drives"])
        self.assertAlmostEqual(flat["team_prior_drives"], (10 + 10 + 20) / 3)


class LeagueEnvironmentTests(XDrivesFixture):
    def _row(self, game_id, team):
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM cfb_xdrives_dataset WHERE game_id=? AND team=?",
                             (game_id, team)).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def test_same_date_games_do_not_inform_each_other(self):
        # Two unrelated games, same Saturday: nothing has happened before it,
        # so every row from that date must see zero leaguewide history.
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Texas", away="Georgia", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=12)
        self.pace(game_id=2, team="Texas", opponent="Georgia", drives=8)
        self.pace(game_id=2, team="Georgia", opponent="Texas", drives=14)
        xdrives.build_dataset(self.repository)
        for game_id, team in ((1, "Michigan"), (1, "Ohio State"), (2, "Texas"), (2, "Georgia")):
            row = self._row(game_id, team)
            self.assertEqual(row["league_prior_games"], 0)
            self.assertIsNone(row["league_prior_drives"])

    def test_a_later_date_sees_the_whole_earlier_slate_not_just_its_own_matchup(self):
        # Two unrelated games on the earlier date, both tied 10-10 so the
        # weighted mean is unambiguously 10 regardless of within-batch order.
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.game(2, home="Texas", away="Georgia", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=10)
        self.pace(game_id=2, team="Texas", opponent="Georgia", drives=10)
        self.pace(game_id=2, team="Georgia", opponent="Texas", drives=10)
        # A later game between two teams that played nobody on 2026-08-30:
        # its league signal must still come from that whole earlier slate.
        self.game(3, home="Iowa", away="Purdue", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=3, team="Iowa", opponent="Purdue", drives=20)
        self.pace(game_id=3, team="Purdue", opponent="Iowa", drives=20)
        xdrives.build_dataset(self.repository)
        row = self._row(3, "Iowa")
        self.assertEqual(row["league_prior_games"], 4)
        self.assertAlmostEqual(row["league_prior_drives"], 10.0)
        # Iowa's own actual result (20) must not have leaked into its own
        # league-level feature either.
        self.assertNotAlmostEqual(row["league_prior_drives"], 20.0)


class ShrinkageTests(XDrivesFixture):
    def _row(self, game_id, team):
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM cfb_xdrives_dataset WHERE game_id=? AND team=?",
                             (game_id, team)).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def test_zero_trailing_games_falls_back_entirely_to_the_league_prior(self):
        # Two teams establish a league level of 10 drives/game; a third team's
        # very first tracked game has no trailing history of its own at all.
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=10)
        self.game(2, home="Iowa", away="Purdue", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=2, team="Iowa", opponent="Purdue", drives=99)  # actual result; must not leak
        self.pace(game_id=2, team="Purdue", opponent="Iowa", drives=99)
        xdrives.build_dataset(self.repository)
        row = self._row(2, "Iowa")
        self.assertEqual(row["team_prior_games"], 0)
        self.assertIsNone(row["team_prior_drives"], "raw trailing average is still undefined at zero games")
        self.assertAlmostEqual(row["team_prior_drives_shrunk"], row["league_prior_drives"])
        self.assertAlmostEqual(row["team_prior_drives_shrunk"], 10.0)

    def test_shrinkage_weight_grows_with_trailing_sample_size(self):
        # Michigan is a genuinely fast team (20 drives/game, steady every
        # week) against a league running well below that; each additional
        # game should trust Michigan's own number more and the league prior
        # less, per the documented formula, so the shrunk estimate should
        # climb monotonically toward 20 as team_prior_games grows.
        # Five league-establishing dates; Michigan's own four games start one
        # date later so a league prior already exists at Michigan's very
        # first game (otherwise both team_prior_drives_shrunk and
        # league_prior_drives are None there, same as the true-cold-start
        # case covered separately above).
        dates = ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20", "2026-09-27"]
        for i, date in enumerate(dates):
            self.game(i + 1, home="Texas", away="Georgia", season=2026, week=i + 1, start_date=date)
            self.pace(game_id=i + 1, team="Texas", opponent="Georgia", drives=10)
            self.pace(game_id=i + 1, team="Georgia", opponent="Texas", drives=10)
        for i, date in enumerate(dates[1:]):
            game_id = 10 + i
            self.game(game_id, home="Michigan", away="Ohio State", season=2026, week=i + 1, start_date=date)
            self.pace(game_id=game_id, team="Michigan", opponent="Ohio State", drives=20)
            self.pace(game_id=game_id, team="Ohio State", opponent="Michigan", drives=10)
        xdrives.build_dataset(self.repository)

        shrunk_by_week = []
        for i in range(4):
            row = self._row(10 + i, "Michigan")
            if row["team_prior_drives"] is not None:
                self.assertAlmostEqual(row["team_prior_drives"], 20.0)
            shrunk_by_week.append((row["team_prior_games"], row["team_prior_drives_shrunk"]))
        games_seen = [g for g, _ in shrunk_by_week]
        self.assertEqual(games_seen, sorted(games_seen))
        for (g1, v1), (g2, v2) in zip(shrunk_by_week, shrunk_by_week[1:]):
            if g2 > g1:
                self.assertGreater(v2, v1, "more trailing games should pull the shrunk estimate closer to 20")

    def test_shrunk_blend_baseline_matches_the_documented_formula(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=12)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=8)
        self.game(2, home="Michigan", away="Iowa", season=2026, week=2, start_date="2026-09-06")
        self.pace(game_id=2, team="Michigan", opponent="Iowa", drives=14)
        self.pace(game_id=2, team="Iowa", opponent="Michigan", drives=9)
        xdrives.build_dataset(self.repository)
        row = self._row(2, "Michigan")

        weight = 1 / (1 + xdrives.SHRINKAGE_PSEUDO_GAMES)  # team_prior_games == 1 here
        expected_team_shrunk = weight * row["team_prior_drives"] + (1 - weight) * row["league_prior_drives"]
        self.assertAlmostEqual(row["team_prior_drives_shrunk"], expected_team_shrunk)

        expected_g = (row["team_prior_drives_shrunk"] + row["opponent_prior_drives_allowed_shrunk"]) / 2
        self.assertAlmostEqual(xdrives._shrunk_blend_prediction(row), expected_g)


class SchemaMigrationTests(unittest.TestCase):
    """A table created before `league_prior_*` existed, migrated in place.

    `ALTER TABLE ADD COLUMN` always appends at the end of the table's actual
    physical layout -- after `built_at`, not before it as this file's own
    CREATE TABLE text (which puts league_prior_* before built_at) would
    suggest for a brand-new table. A positional `INSERT ... VALUES(...)`
    silently writes by that physical position and misaligns every value once
    the two layouts disagree; this reproduces exactly the table shape a real,
    already-deployed database has and checks that build_dataset's INSERT
    still lands values in the right named columns despite it.
    """

    _PRE_MIGRATION_SCHEMA = """
        CREATE TABLE cfb_xdrives_dataset (
          game_id INTEGER NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
          dataset_version TEXT NOT NULL, season INTEGER NOT NULL, week INTEGER NOT NULL,
          home_away TEXT NOT NULL, actual_meaningful_drives INTEGER, actual_game_total_drives INTEGER,
          team_prior_games INTEGER NOT NULL, team_prior_drives REAL, team_prior_drives_allowed REAL,
          team_prior_seconds_per_play REAL, team_prior_neutral_seconds_per_play REAL,
          team_prior_pass_rate REAL, team_prior_neutral_pass_rate REAL, team_prior_success_rate REAL,
          team_prior_explosive_rate REAL, team_prior_first_down_rate REAL, team_prior_three_and_out_rate REAL,
          opponent_prior_games INTEGER NOT NULL, opponent_prior_drives REAL, opponent_prior_drives_allowed REAL,
          opponent_prior_seconds_per_play REAL, opponent_prior_neutral_seconds_per_play REAL,
          opponent_prior_pass_rate REAL, opponent_prior_neutral_pass_rate REAL, opponent_prior_success_rate REAL,
          opponent_prior_explosive_rate REAL, opponent_prior_first_down_rate REAL,
          opponent_prior_three_and_out_rate REAL,
          team_elo INTEGER, opponent_elo INTEGER, market_spread REAL, market_total REAL,
          team_implied_points REAL, opponent_implied_points REAL,
          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,dataset_version)
        );
    """

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        tgp.initialize(self.repository)
        cfb_lines.initialize(self.repository)
        with closing(sqlite3.connect(self.path)) as c:
            c.executescript(self._PRE_MIGRATION_SCHEMA)
            c.commit()

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def test_values_land_in_the_correct_named_columns_after_migration(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
                start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
                home_pregame_elo,away_team_id,away_team,away_pregame_elo,updated_at)
                VALUES(1,2026,1,'regular','2026-08-30',0,1,0,0,1,'Michigan',1500,2,'Ohio State',1500,?)""",
                (NOW,))
            c.execute("""INSERT INTO cfb_team_game_pace(game_id,team,opponent,metric_version,
                raw_drives,meaningful_drives,ot_drives,three_and_out_drives,scrimmage_plays,
                plays_per_meaningful_drive,seconds_per_play,neutral_seconds_per_play,pass_rate,
                neutral_pass_rate,success_rate,explosive_rate,first_down_rate,three_and_out_rate,
                tempo_intervals,neutral_tempo_intervals,built_at)
                VALUES(1,'Michigan','Ohio State',?,10,10,0,0,60,6.0,25.0,25.0,0.5,0.5,0.45,0.1,0.3,0.2,10,10,?)""",
                (tgp.METRIC_VERSION, NOW))
            c.execute("""INSERT INTO cfb_team_game_pace(game_id,team,opponent,metric_version,
                raw_drives,meaningful_drives,ot_drives,three_and_out_drives,scrimmage_plays,
                plays_per_meaningful_drive,seconds_per_play,neutral_seconds_per_play,pass_rate,
                neutral_pass_rate,success_rate,explosive_rate,first_down_rate,three_and_out_rate,
                tempo_intervals,neutral_tempo_intervals,built_at)
                VALUES(1,'Ohio State','Michigan',?,12,12,0,0,60,5.0,24.0,24.0,0.5,0.5,0.45,0.1,0.3,0.2,10,10,?)""",
                (tgp.METRIC_VERSION, NOW))
            c.commit()

        result = xdrives.build_dataset(self.repository)
        self.assertEqual(result["rows"], 2)

        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            row = dict(c.execute(
                "SELECT * FROM cfb_xdrives_dataset WHERE game_id=1 AND team='Michigan'").fetchone())

        # built_at must be an ISO timestamp string, not a games-count or a
        # league-drives float landed there by a misaligned positional INSERT.
        self.assertIsInstance(row["built_at"], str)
        self.assertRegex(row["built_at"], r"^\d{4}-\d{2}-\d{2}T")
        # league_prior_drives is the very first game in this database, so it
        # must be a clean None -- not a stray timestamp string that a
        # misaligned INSERT would have written into a REAL-affinity column.
        self.assertIsNone(row["league_prior_drives"])
        self.assertEqual(row["league_prior_games"], 0)
        self.assertEqual(row["team_prior_games"], 0)
        self.assertEqual(row["actual_meaningful_drives"], 10)
        # Same check for the shrinkage columns added in the same migration
        # pass: no league prior and no trailing games means a clean None,
        # not a value from whichever neighboring column ended up at that
        # physical offset.
        self.assertIsNone(row["team_prior_drives_shrunk"])
        self.assertIsNone(row["opponent_prior_drives_allowed_shrunk"])


class MarketAndEloTests(XDrivesFixture):
    def test_elo_is_read_from_the_correct_home_away_side(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30",
                  home_elo=1700, away_elo=1900)
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=11)
        xdrives.build_dataset(self.repository)
        home_row = self._row(1, "Michigan")
        away_row = self._row(1, "Ohio State")
        self.assertEqual(home_row["team_elo"], 1700)
        self.assertEqual(home_row["opponent_elo"], 1900)
        self.assertEqual(away_row["team_elo"], 1900)
        self.assertEqual(away_row["opponent_elo"], 1700)

    def test_market_spread_is_flipped_for_the_away_team_and_implied_points_split_the_total(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1, start_date="2026-08-30")
        self.pace(game_id=1, team="Michigan", opponent="Ohio State", drives=10)
        self.pace(game_id=1, team="Ohio State", opponent="Michigan", drives=11)
        self.lines(1, spread=-7.0, total=50.0)  # home (Michigan) favored by 7
        xdrives.build_dataset(self.repository)
        home_row = self._row(1, "Michigan")
        away_row = self._row(1, "Ohio State")
        self.assertAlmostEqual(home_row["market_spread"], -7.0)
        self.assertAlmostEqual(home_row["team_implied_points"], 28.5)
        self.assertAlmostEqual(home_row["opponent_implied_points"], 21.5)
        self.assertAlmostEqual(away_row["team_implied_points"], 21.5)
        self.assertAlmostEqual(away_row["opponent_implied_points"], 28.5)

    def _row(self, game_id, team):
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM cfb_xdrives_dataset WHERE game_id=? AND team=?",
                             (game_id, team)).fetchone()
        return dict(row)


class BaselineTests(XDrivesFixture):
    def _seed_two_team_history(self):
        # Michigan plays four games; only games 2-4 have one prior game of
        # history, so baseline evaluation should drop game 1 as a cold start.
        dates = ["2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20"]
        opponents = ["Ohio State", "Rutgers", "Iowa", "Purdue"]
        team_drives = [10, 12, 14, 8]
        opp_drives = [11, 9, 13, 12]
        for i, (opponent, date) in enumerate(zip(opponents, dates)):
            game_id = i + 1
            self.game(game_id, home="Michigan", away=opponent, season=2026, week=i + 1, start_date=date)
            self.pace(game_id=game_id, team="Michigan", opponent=opponent, drives=team_drives[i])
            self.pace(game_id=game_id, team=opponent, opponent="Michigan", drives=opp_drives[i])
        xdrives.build_dataset(self.repository)

    def test_cold_start_rows_are_dropped_and_counted(self):
        self._seed_two_team_history()
        result = xdrives.evaluate_baselines(self.repository)
        # Michigan's game 1 and each opponent's own single appearance are all
        # cold starts (zero trailing games); only Michigan's games 2-4 have history.
        self.assertGreaterEqual(result["rows_dropped_cold_start"], 5)
        self.assertEqual(result["overall"]["B_team_average"]["games"], 3)

    def test_overall_matched_scores_every_baseline_on_identical_rows(self):
        self._seed_two_team_history()
        result = xdrives.evaluate_baselines(self.repository)
        matched_counts = {label: packet["games"] for label, packet in result["overall_matched"].items()}
        self.assertGreater(len(set(matched_counts.values())), 0)
        self.assertEqual(len(set(matched_counts.values())), 1,
                         f"overall_matched should score every baseline on the same rows: {matched_counts}")
        # G's un-matched view can only have as many or more eligible rows
        # than the matched one, since its league-prior fallback needs less
        # than C/D/E do.
        self.assertGreaterEqual(result["overall"]["G_shrunk_blend"]["games"],
                                result["overall_matched"]["G_shrunk_blend"]["games"])

    def test_baseline_b_mae_matches_hand_computed_trailing_average_error(self):
        self._seed_two_team_history()
        result = xdrives.evaluate_baselines(self.repository)
        # Michigan week 2: prior=[10] -> predict 10, actual 12
        # Michigan week 3: prior=[10,12] (decay-weighted) -> actual 14
        # Michigan week 4: prior=[10,12,14] (decay-weighted) -> actual 8
        predictions = [_decayed_mean([10]), _decayed_mean([10, 12]), _decayed_mean([10, 12, 14])]
        actuals = [12, 14, 8]
        expected_mae = sum(abs(p - a) for p, a in zip(predictions, actuals)) / 3
        self.assertAlmostEqual(result["overall"]["B_team_average"]["mae"], expected_mae, places=4)

    def test_baseline_a_uses_the_evaluated_rows_league_mean(self):
        self._seed_two_team_history()
        result = xdrives.evaluate_baselines(self.repository)
        # Only rows with >=1 prior game are evaluated: Michigan's weeks 2-4 (12,14,8).
        self.assertAlmostEqual(result["league_average_drives"], (12 + 14 + 8) / 3, places=4)


class AdvancedModelTests(XDrivesFixture):
    def test_fit_advanced_model_persists_coefficients_and_feature_order(self):
        self._seed_round_robin()
        xdrives.build_dataset(self.repository)
        result = xdrives.fit_advanced_model(self.repository)
        self.assertEqual(result["feature_keys"], list(xdrives.ADVANCED_FEATURES))
        self.assertIsNotNone(result["coefficients"])
        self.assertEqual(len(result["coefficients"]), len(xdrives.ADVANCED_FEATURES) + 1)
        self.assertGreater(result["training_rows"], 0)

        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            stored = c.execute("SELECT * FROM cfb_xdrives_model WHERE model_version=?",
                               (xdrives.ADVANCED_MODEL_VERSION,)).fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(json.loads(stored["coefficients_json"]), result["coefficients"])

    def test_fit_advanced_model_is_none_with_no_eligible_rows(self):
        # Nothing seeded at all: zero rows, zero history.
        xdrives.build_dataset(self.repository)
        result = xdrives.fit_advanced_model(self.repository)
        self.assertIsNone(result["coefficients"])
        self.assertEqual(result["training_rows"], 0)

    def test_evaluate_advanced_model_splits_rows_by_season_not_just_by_row_count(self):
        self._seed_round_robin(season=2025, start_week=1, start_date="2025-08-30")
        self._seed_round_robin(season=2026, start_week=1, start_date="2026-08-30")
        xdrives.build_dataset(self.repository)

        result = xdrives.evaluate_advanced_model(
            self.repository, train_from=2025, train_to=2025, test_from=2026, test_to=2026)

        # Every ADVANCED_FEATURES key must be present, not just team_prior_games>=1:
        # a team's own trailing stats can be populated while its opponent's
        # trailing "drives allowed" is still None because that opponent hasn't
        # played yet -- exactly the leak-safe cold-start case this dataset is
        # built to represent honestly rather than paper over.
        eligibility_sql = (
            "SELECT COUNT(*) AS n FROM cfb_xdrives_dataset WHERE season=? "
            "AND actual_meaningful_drives IS NOT NULL AND team_prior_drives IS NOT NULL "
            "AND opponent_prior_drives_allowed IS NOT NULL AND league_prior_drives IS NOT NULL "
            "AND team_prior_seconds_per_play IS NOT NULL "
            "AND opponent_prior_seconds_per_play IS NOT NULL AND team_prior_pass_rate IS NOT NULL "
            "AND team_prior_success_rate IS NOT NULL AND opponent_prior_success_rate IS NOT NULL "
            "AND team_elo IS NOT NULL AND opponent_elo IS NOT NULL AND market_total IS NOT NULL"
        )
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            train_eligible = c.execute(eligibility_sql, (2025,)).fetchone()["n"]
            test_eligible = c.execute(eligibility_sql, (2026,)).fetchone()["n"]
        self.assertEqual(result["training_rows"], train_eligible)
        self.assertEqual(result["test_rows_evaluated"], test_eligible)
        self.assertGreater(result["training_rows"], 0)
        self.assertGreater(result["test_rows_evaluated"], 0)
        # The model was fit only on 2025 rows; the 2026 second-season prior
        # window (which now includes 2025's tail) must not have influenced it.
        self.assertIsNotNone(result["coefficients"])
        for label in ("A_league_average", "B_team_average", "C_team_and_opponent_allowed_blend",
                      "E_environment_blend", "F_advanced_regression", "G_shrunk_blend"):
            self.assertGreaterEqual(result["test_results"][label]["games"], 0)

    def test_evaluate_advanced_model_requires_explicit_season_bounds(self):
        with self.assertRaises(TypeError):
            xdrives.evaluate_advanced_model(self.repository)


class LiveModelTests(XDrivesFixture):
    """load_model/predict_drives/league_prior_drives_live: the pieces
    game_projection.py's live drive-count path calls directly, as opposed to
    the batch fit/evaluate functions above."""

    def test_load_model_round_trips_the_live_novegas_feature_set(self):
        self._seed_round_robin()
        xdrives.build_dataset(self.repository)
        xdrives.fit_advanced_model(
            self.repository, feature_keys=xdrives.ADVANCED_FEATURES_LIVE,
            model_version=xdrives.ADVANCED_MODEL_VERSION_LIVE)

        model = xdrives.load_model(self.repository)
        self.assertIsNotNone(model)
        self.assertEqual(model["model_version"], xdrives.ADVANCED_MODEL_VERSION_LIVE)
        self.assertEqual(model["features"], xdrives.ADVANCED_FEATURES_LIVE)
        self.assertNotIn("market_total", model["features"])
        self.assertEqual(len(model["coefficients"]), len(xdrives.ADVANCED_FEATURES_LIVE) + 1)
        self.assertGreater(model["training_rows"], 0)

    def test_load_model_returns_none_when_never_fit(self):
        self.assertIsNone(xdrives.load_model(self.repository))
        self.assertIsNone(xdrives.load_model(
            self.repository, model_version="some-version-nobody-fit"))

    def test_predict_drives_matches_hand_computed_linear_combination(self):
        coefficients = [5.0, 0.5, -0.25]
        feature_keys = ("team_prior_drives", "opponent_prior_drives_allowed")
        row = {"team_prior_drives": 12.0, "opponent_prior_drives_allowed": 10.0}
        expected = 5.0 + 0.5 * 12.0 + (-0.25) * 10.0
        self.assertAlmostEqual(
            xdrives.predict_drives(coefficients, feature_keys, row), expected)

    def test_predict_drives_derives_elo_diff_and_is_home_like_fitting_does(self):
        coefficients = [0.0, 1.0, 3.0]
        feature_keys = ("elo_diff", "is_home")
        row = {"team_elo": 1550, "opponent_elo": 1500, "home_away": "home"}
        self.assertAlmostEqual(
            xdrives.predict_drives(coefficients, feature_keys, row), 50.0 + 3.0)
        away_row = {**row, "home_away": "away"}
        self.assertAlmostEqual(
            xdrives.predict_drives(coefficients, feature_keys, away_row), 50.0)

    def test_predict_drives_returns_none_when_a_feature_is_missing(self):
        coefficients = [5.0, 0.5]
        feature_keys = ("team_prior_drives",)
        self.assertIsNone(xdrives.predict_drives(coefficients, feature_keys, {}))

    def test_league_prior_drives_live_matches_the_documented_decay_formula(self):
        self._seed_round_robin(season=2026, start_week=1, start_date="2026-08-30")
        # league_prior_drives_live walks cfb_team_game_pace directly (not the
        # built dataset), so it doesn't need build_dataset() to have run.
        # Uses its own slower-decaying LEAGUE_LAMBDA, not RECENCY_LAMBDA (the
        # module-level _decayed_mean helper's hardcoded lambda), so the
        # expected value is computed by hand here rather than via that helper.
        # Same query, ORDER BY and LIMIT as league_prior_drives_live itself
        # (then reversed to oldest-first) -- some games share a start_date, and
        # DESC-then-reverse can break those ties differently than a plain ASC
        # query would, which would otherwise assign the tied rows' weights
        # the wrong way round and fail this on a false negative.
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            rows = [row["meaningful_drives"] for row in c.execute("""
                SELECT a.meaningful_drives FROM cfb_team_game_pace a
                JOIN games g ON g.game_id=a.game_id
                WHERE g.start_date<? ORDER BY g.start_date DESC LIMIT ?""",
                ("2026-12-31", xdrives.LEAGUE_WINDOW_GAMES))]
        rows.reverse()
        n = len(rows)
        weights = [math.exp(-xdrives.LEAGUE_LAMBDA * (n - 1 - i)) for i in range(n)]
        expected = sum(w * v for w, v in zip(weights, rows)) / sum(weights)

        result = xdrives.league_prior_drives_live(self.repository, before_date="2026-12-31")
        self.assertAlmostEqual(result, expected)

    def test_league_prior_drives_live_excludes_games_on_or_after_before_date(self):
        self._seed_round_robin(season=2026, start_week=1, start_date="2026-08-30")
        early = xdrives.league_prior_drives_live(self.repository, before_date="2026-08-30")
        self.assertIsNone(early)  # nothing strictly before the first game's own date

    def test_league_prior_drives_live_returns_none_with_no_history(self):
        self.assertIsNone(
            xdrives.league_prior_drives_live(self.repository, before_date="2026-08-30"))


if __name__ == "__main__":
    unittest.main()
