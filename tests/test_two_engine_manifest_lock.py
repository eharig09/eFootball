"""Viewing a game page must not need the database's write lock.

`frozen_manifest_for_game` is called on every game-page view. It used to start with `_initialize_manifest`, which opened a
`BEGIN IMMEDIATE` write transaction just to replay `CREATE TABLE IF NOT EXISTS`. While a long refresh or build held the
write lock (CFB's `build-team-advanced` can for minutes) every page view waited out the busy timeout and then failed
with "database is locked". Schema setup now runs once per process, so a read-only view never takes the writer slot.
"""
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing

from sports_aggregator.cfb import two_engine_live as live
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas


class ManifestReadsDoNotNeedTheWriteLock(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        self.previous_timeout = os.environ.get("CFB_SQLITE_BUSY_TIMEOUT_MS")
        os.environ["CFB_SQLITE_BUSY_TIMEOUT_MS"] = "1000"        # the real default is 60 s; keep the test quick

    def tearDown(self):
        if self.previous_timeout is None:
            os.environ.pop("CFB_SQLITE_BUSY_TIMEOUT_MS", None)
        else:
            os.environ["CFB_SQLITE_BUSY_TIMEOUT_MS"] = self.previous_timeout
        forget_initialized_schemas()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def hold_write_lock(self):
        blocker = sqlite3.connect(self.path, timeout=1)
        blocker.execute("BEGIN IMMEDIATE")                     # what a long build does
        return blocker

    def test_a_game_view_succeeds_while_another_process_holds_the_write_lock(self):
        live.frozen_manifest_for_game(self.repository, 1)       # first use in this process creates the tables
        blocker = self.hold_write_lock()
        try:
            started = time.time()
            result = live.frozen_manifest_for_game(self.repository, 1)
            elapsed = time.time() - started
        finally:
            blocker.rollback()
            blocker.close()
        self.assertIsNone(result)                               # no manifest yet, and no error
        self.assertLess(elapsed, 0.5, "a read must not wait for the writer")

    def test_schema_setup_runs_once_per_process_not_once_per_call(self):
        opened = []
        original = self.repository.transaction

        def counting():
            opened.append(1)
            return original()

        self.repository.transaction = counting
        for _ in range(5):
            live._initialize_manifest(self.repository)
        self.assertEqual(len(opened), 1)

    def test_even_the_first_view_after_a_restart_takes_no_write_lock_on_an_existing_database(self):
        live._initialize_manifest(self.repository)              # the database already has the tables (as in production)
        forget_initialized_schemas()                            # ...and the server has just restarted
        fresh = CFBRepository(self.path)
        blocker = self.hold_write_lock()
        try:
            started = time.time()
            self.assertIsNone(live.frozen_manifest_for_game(fresh, 1))
            elapsed = time.time() - started
        finally:
            blocker.rollback()
            blocker.close()
        self.assertLess(elapsed, 0.5)

    def test_a_missing_piece_is_still_created(self):
        live._initialize_manifest(self.repository)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP INDEX idx_two_engine_manifest_history_game")
            connection.commit()
        forget_initialized_schemas()
        live._initialize_manifest(CFBRepository(self.path))
        with closing(sqlite3.connect(self.path)) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        self.assertIn("idx_two_engine_manifest_history_game", names)

    def test_the_tables_exist_after_setup(self):
        live._initialize_manifest(self.repository)
        with closing(sqlite3.connect(self.path)) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("cfb_two_engine_manifest", names)


if __name__ == "__main__":
    unittest.main()
