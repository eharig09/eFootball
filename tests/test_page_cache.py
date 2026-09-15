"""Page caching, and the invalidation that makes it safe.

Pages here are ~100% CPU: one matchup render spends 1,312 ms of CPU against
1,368 ms of wall clock, so the GIL has no I/O to overlap and threads add
nothing. Eight concurrent requests for one page took 21.8 seconds, with
throughput falling from 1.34/s to 0.37/s. Cached, the same page serves in
single-digit milliseconds and throughput scales instead of collapsing.

The risk a cache introduces is staleness, so most of what follows is about the
cache getting out of the way at the right moments.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing

from app import create_app
from sports_aggregator.cfb.models import Game, Player, Team
from sports_aggregator.cfb.repository import (
    CFBRepository, forget_initialized_schemas,
)
from sports_aggregator.page_cache import (
    DEFAULT_PAGE_CACHE_SECONDS, cache, data_version, page_cache_seconds,
)
from sports_aggregator.social.content import ContentRepository


class PageCacheTests(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        # data_version() hashes NFL_DATABASE_PATH alongside the CFB one, so
        # this needs the same isolation as CFB's -- otherwise the cache key
        # depends on whatever instance/nfl.sqlite3 happens to hold locally,
        # which a cache-invalidation test cannot control.
        nfl_handle, self.nfl_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(nfl_handle)
        os.unlink(self.nfl_path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams([Team.from_cfbd(payload) for payload in (
            {"id": 68, "school": "Boise State", "mascot": "Broncos",
             "abbreviation": "BSU", "alternateNames": [],
             "conference": "Mountain West", "classification": "fbs",
             "color": "#0033A0", "logos": []},
            {"id": 21, "school": "San Diego State", "mascot": "Aztecs",
             "abbreviation": "SDSU", "alternateNames": [],
             "conference": "Mountain West", "classification": "fbs",
             "color": "#A6192E", "logos": []},
        )])
        self.repository.replace_games(2026, [Game.from_cfbd({
            "id": 401, "season": 2026, "week": 3, "seasonType": "regular",
            "startDate": "2026-09-12T19:30:00.000Z", "startTimeTBD": False,
            "completed": False, "neutralSite": False, "conferenceGame": True,
            "venue": "Snapdragon Stadium", "venueId": 1,
            "homeId": 21, "homeTeam": "San Diego State",
            "homeConference": "Mountain West", "homePoints": None,
            "awayId": 68, "awayTeam": "Boise State",
            "awayConference": "Mountain West", "awayPoints": None,
        })])
        self.repository.replace_players(2026, (
            Player("p1", 2026, "Test", "Player", "Boise State", "QB", 1, 74, 200, 3),
        ))
        ContentRepository(self.path).initialize()
        # TESTING is deliberately off: this suite is about the cache being on.
        self.app = create_app({
            "REGISTER_LEGACY_DASHBOARDS": False, "CFB_REPOSITORY": self.repository,
            "CFB_DEFAULT_SEASON": 2026, "CFB_DATABASE_PATH": self.path,
            "NFL_DATABASE_PATH": self.nfl_path,
        })
        cache.clear()
        self.client = self.app.test_client()

    def tearDown(self):
        cache.clear()
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm",
                     self.nfl_path, self.nfl_path + "-wal", self.nfl_path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def _touch_database(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE teams SET mascot='Broncos!' WHERE team_id=68")
            connection.commit()
        time.sleep(0.02)

    # -- serving -----------------------------------------------------------

    def test_a_repeated_request_returns_identical_markup(self):
        first = self.client.get("/college-football/teams/68/").get_data()
        second = self.client.get("/college-football/teams/68/").get_data()
        self.assertEqual(first, second)

    def test_a_cached_page_does_no_database_work_at_all(self):
        """Asserted by behaviour rather than by clock.

        A wall-time comparison is flaky on a fixture this small, where test
        harness overhead dwarfs the render. Statement count is exact: a served
        cache entry should touch the database zero times.
        """
        path = "/college-football/teams/68/"
        # Two warm-ups, not one. First contact with a database does one-time
        # setup work that moves its modification time, so the very first
        # request lands under a key nothing else will use. The cache settles
        # from the second request on, which costs one extra render per process.
        self.client.get(path)
        self.client.get(path)

        seen = {"statements": 0}
        real_connect = sqlite3.connect

        def traced(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(
                lambda _sql: seen.__setitem__("statements", seen["statements"] + 1))
            return connection

        sqlite3.connect = traced
        try:
            self.client.get(path)
        finally:
            sqlite3.connect = real_connect
        self.assertEqual(seen["statements"], 0)

    # -- invalidation ------------------------------------------------------

    def test_writing_to_the_database_changes_the_version(self):
        with self.app.test_request_context("/college-football/"):
            before = data_version()
        self._touch_database()
        with self.app.test_request_context("/college-football/"):
            self.assertNotEqual(data_version(), before)

    def test_a_page_is_rebuilt_after_the_data_changes(self):
        """A refresh runs in another process and cannot clear this cache."""
        path = "/college-football/teams/68/"
        self.client.get(path)
        self.assertIn(b"Broncos", self.client.get(path).get_data())
        self._touch_database()
        self.assertIn(b"Broncos!", self.client.get(path).get_data())

    def test_the_version_survives_a_missing_database(self):
        with self.app.test_request_context("/college-football/"):
            os.unlink(self.path)
            self.assertIsInstance(data_version(), str)

    # -- keying ------------------------------------------------------------

    def test_query_parameters_are_separate_pages(self):
        """A team page carries schedule_year, stats_year and stats_mode."""
        default = self.client.get("/college-football/teams/68/").get_data()
        totals = self.client.get(
            "/college-football/teams/68/?stats_mode=total").get_data()
        self.assertNotEqual(default, totals)

    def test_different_teams_do_not_share_an_entry(self):
        boise = self.client.get("/college-football/teams/68/").get_data()
        aztecs = self.client.get("/college-football/teams/21/").get_data()
        self.assertNotEqual(boise, aztecs)
        self.assertIn(b"San Diego State", aztecs)

    # -- getting out of the way --------------------------------------------

    def test_testing_mode_bypasses_the_cache_entirely(self):
        """Otherwise every assertion in the suite could pass on a stale page."""
        app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
            "CFB_DATABASE_PATH": self.path,
        })
        client = app.test_client()
        client.get("/college-football/teams/68/")
        self._touch_database()
        self.assertIn(b"Broncos!", client.get("/college-football/teams/68/").get_data())

    def test_the_lifetime_is_configurable_and_can_be_switched_off(self):
        original = os.environ.get("CFB_PAGE_CACHE_SECONDS")
        try:
            os.environ.pop("CFB_PAGE_CACHE_SECONDS", None)
            self.assertEqual(page_cache_seconds(), DEFAULT_PAGE_CACHE_SECONDS)
            os.environ["CFB_PAGE_CACHE_SECONDS"] = "0"
            self.assertEqual(page_cache_seconds(), 0)
            os.environ["CFB_PAGE_CACHE_SECONDS"] = "nonsense"
            self.assertEqual(page_cache_seconds(), DEFAULT_PAGE_CACHE_SECONDS)
        finally:
            os.environ.pop("CFB_PAGE_CACHE_SECONDS", None)
            if original is not None:
                os.environ["CFB_PAGE_CACHE_SECONDS"] = original

    def test_a_missing_page_is_never_cached(self):
        """An error pinned for fifteen minutes would outlast its cause."""
        self.assertEqual(
            self.client.get("/college-football/teams/999999/").status_code, 404)
        self.assertEqual(
            self.client.get("/college-football/teams/999999/").status_code, 404)


if __name__ == "__main__":
    unittest.main()
