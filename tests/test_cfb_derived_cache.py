import sqlite3
import tempfile
import unittest
from pathlib import Path

from sports_aggregator.cfb import derived_cache


class _Repository:
    def __init__(self, path):
        self.path = str(path)


class DerivedCacheTests(unittest.TestCase):
    def setUp(self):
        derived_cache.clear_memory()
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "cfb.sqlite3"
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (x INTEGER)")
        connection.commit()
        connection.close()
        self.repository = _Repository(self.path)

    def tearDown(self):
        derived_cache.clear_memory()
        self._directory.cleanup()

    def test_value_is_built_once_per_database_state(self):
        calls = []
        build = lambda: calls.append(1) or {"value": len(calls)}
        first = derived_cache.derived(self.repository, "x", build, 2026)
        second = derived_cache.derived(self.repository, "x", build, 2026)
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)
        derived_cache.derived(self.repository, "x", build, 2025)         # a different key builds again
        self.assertEqual(len(calls), 2)

    def test_a_write_retires_the_value(self):
        calls = []
        build = lambda: calls.append(1) or 1
        derived_cache.derived(self.repository, "x", build)
        connection = sqlite3.connect(self.path)
        connection.executemany("INSERT INTO t VALUES (?)", [(index,) for index in range(5000)])
        connection.commit()
        connection.close()
        derived_cache.derived(self.repository, "x", build)
        self.assertEqual(len(calls), 2)

    def test_an_idle_open_and_close_does_not_retire_the_value(self):
        calls = []
        build = lambda: calls.append(1) or 1
        derived_cache.derived(self.repository, "x", build)
        for _ in range(3):                      # opening a WAL database recreates its log file
            connection = sqlite3.connect(self.path)
            connection.execute("SELECT COUNT(*) FROM t").fetchone()
            connection.close()
        derived_cache.derived(self.repository, "x", build)
        self.assertEqual(len(calls), 1)

    def test_persisted_value_survives_and_is_replaced_when_the_fingerprint_changes(self):
        calls = []
        build = lambda: calls.append(1) or 0.35
        self.assertEqual(derived_cache.persisted(self.repository, "rate", "aaa", build), 0.35)
        self.assertEqual(derived_cache.persisted(self.repository, "rate", "aaa", build), 0.35)
        self.assertEqual(len(calls), 1)
        derived_cache.persisted(self.repository, "rate", "bbb", build)
        self.assertEqual(len(calls), 2)
        files = sorted(item.name for item in (self.path.parent / "model_cache").glob("rate-*.json"))
        self.assertEqual(files, ["rate-bbb.json"])                       # the stale fingerprint is removed


if __name__ == "__main__":
    unittest.main()
