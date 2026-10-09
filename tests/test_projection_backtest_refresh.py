"""The production backtest step rebuilds one stale season per run, keyed by an inputs-version marker."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sports_aggregator.cfb import projection_backtest as pb
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class RefreshFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        pb.initialize(self.repository)
        for index, season in enumerate((2021, 2022, 2023, 2026), start=1):
            self.game(index, season)

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def game(self, game_id, season):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """INSERT INTO games(game_id,season,week,season_type,start_date,start_time_tbd,completed,
                   neutral_site,conference_game,home_team_id,home_team,home_points,away_team_id,away_team,
                   away_points,updated_at) VALUES(?,?,3,'regular',?,0,1,0,0,1,'A',30,2,'B',20,?)""",
                (game_id, season, f"{season}-09-20T16:00:00Z", NOW.isoformat()))
            connection.commit()

    def mark(self, season, *, inputs=pb.INPUTS_VERSION, built=NOW):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(pb.META_SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO cfb_projection_backtest_meta VALUES(?,?,?,?,?)",
                (pb.BACKTEST_VERSION, season, inputs, built.isoformat(), 1))
            connection.commit()

    def stale(self):
        return pb.stale_seasons(self.repository, current_season=2026, now=NOW)


class StaleSeasonTests(RefreshFixture):
    def test_every_season_is_stale_until_it_carries_the_current_marker(self):
        self.assertEqual(self.stale(), [2021, 2022, 2023, 2026])

    def test_a_marker_for_older_inputs_does_not_count(self):
        for season in (2021, 2022, 2023, 2026):
            self.mark(season, inputs="before-the-fix")
        self.assertEqual(self.stale(), [2021, 2022, 2023, 2026])

    def test_current_markers_make_finished_seasons_fresh_and_the_current_one_fresh_for_a_few_days(self):
        for season in (2021, 2022, 2023):
            self.mark(season, built=NOW - timedelta(days=400))      # a finished season never ages out
        self.mark(2026, built=NOW - timedelta(days=2))
        self.assertEqual(self.stale(), [])
        self.mark(2026, built=NOW - timedelta(days=7))
        self.assertEqual(self.stale(), [2026])

    def test_a_season_with_no_completed_games_is_never_listed(self):
        self.assertNotIn(2024, self.stale())        # no 2024 games were seeded, and no marker either
        self.assertNotIn(2020, self.stale())        # before the first backtest season


class RefreshTests(RefreshFixture):
    def test_rebuilds_only_the_oldest_stale_season_and_reports_the_rest(self):
        built = []

        def fake_build(repository, *, from_season, to_season, **kwargs):
            built.append((from_season, to_season))
            return {"games_projected": 1, "team_game_rows": 2}

        with patch.object(pb, "build", fake_build):
            result = pb.refresh(self.repository, current_season=2026)
        self.assertEqual(built, [(2021, 2021)])
        self.assertEqual(result["status"], "rebuilt")
        self.assertEqual(result["remaining_seasons"], [2022, 2023, 2026])

    def test_reports_current_when_nothing_is_stale(self):
        for season in (2021, 2022, 2023, 2026):
            self.mark(season, built=datetime.now(timezone.utc))
        with patch.object(pb, "build", side_effect=AssertionError("nothing to rebuild")):
            result = pb.refresh(self.repository, current_season=2026)
        self.assertEqual(result["status"], "current")


if __name__ == "__main__":
    unittest.main()
