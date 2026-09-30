"""Read paths must not write, and cached aggregates must not outlive the data they came from."""

import tempfile
import unittest
from pathlib import Path

from sports_aggregator.nfl import repository as repository_module
from sports_aggregator.nfl.models import Team
from sports_aggregator.nfl.repository import NFLRepository, clone, database_stamp, memoized


def _team(code="GB", name="Green Bay Packers"):
    return Team(code, name, "Packers", "NFC", "NFC North", "#203731", "#FFB612", None)


class InitializeTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "nfl.sqlite3"
        self.repository = NFLRepository(self.path)

    def tearDown(self):
        self._directory.cleanup()

    def test_repeated_initialize_does_not_touch_the_database(self):
        self.repository.initialize()
        before = self.repository.data_stamp()
        for _ in range(5):
            self.repository.initialize()
            self.repository.list_teams()
        self.assertEqual(self.repository.data_stamp(), before)

    def test_venue_seeding_is_a_no_op_once_current(self):
        self.repository.initialize()
        before = self.repository.data_stamp()
        self.repository.seed_team_venues()
        self.assertEqual(self.repository.data_stamp(), before)

    def test_venue_seeding_repairs_a_drifted_row(self):
        self.repository.initialize()
        with repository_module.closing(self.repository._connect()) as connection:
            connection.execute("UPDATE team_venues SET venue_name='Wrong' WHERE team=(SELECT MIN(team) FROM team_venues)")
            connection.commit()
        self.repository.seed_team_venues()
        with repository_module.closing(self.repository._connect()) as connection:
            names = [row[0] for row in connection.execute("SELECT venue_name FROM team_venues")]
        self.assertNotIn("Wrong", names)

    def test_the_planner_gets_statistics_and_the_metric_index_leads_with_metric(self):
        self.repository.initialize()
        with repository_module.closing(self.repository._connect()) as connection:
            connection.execute("INSERT INTO player_weekly_stats VALUES (2025,1,'REG','g','p','P','GB','CHI','QB','passing_yards',1)")
            connection.commit()
            NFLRepository._ensure_statistics(connection)             # an empty table has nothing to analyze
            self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_stat1 LIMIT 1").fetchone())
            columns = [row[2] for row in connection.execute("PRAGMA index_info(idx_nfl_weekly_by_metric)")]
            self.assertEqual(columns, ["metric", "season", "week"])
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='idx_nfl_weekly_metric'").fetchone())

    def test_a_per_player_season_query_cannot_be_hijacked_by_the_metric_index(self):
        self.repository.initialize()
        with repository_module.closing(self.repository._connect()) as connection:
            plan = " ".join(str(row[3]) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT metric,SUM(value) FROM player_weekly_stats "
                "WHERE season=? AND player_id=? GROUP BY metric", (2025, "p")))
        self.assertNotIn("idx_nfl_weekly_by_metric", plan)

    def test_a_recreated_database_file_is_initialized_again(self):
        self.repository.initialize()
        self.path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        self.repository.initialize()
        self.assertEqual(self.repository.list_teams(), [])          # would raise if the schema were missing


class StampAndMemoTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.repository = NFLRepository(Path(self._directory.name) / "nfl.sqlite3")
        self.repository.initialize()

    def tearDown(self):
        self._directory.cleanup()

    def test_stamp_changes_on_every_write_and_not_on_reads(self):
        empty = self.repository.data_stamp()
        self.repository.replace_teams([_team()])
        written = self.repository.data_stamp()
        self.assertNotEqual(empty, written)
        self.repository.list_teams()
        self.assertEqual(self.repository.data_stamp(), written)
        self.repository.replace_teams([_team(name="Green Bay Packers!")])      # same-size rewrite still counts
        self.assertNotEqual(self.repository.data_stamp(), written)

    def test_memo_computes_once_until_the_data_changes(self):
        calls = []

        def compute():
            calls.append(1)
            return [{"n": len(calls)}]

        self.assertEqual(self.repository.memo(("probe",), compute), [{"n": 1}])
        self.assertEqual(self.repository.memo(("probe",), compute), [{"n": 1}])
        self.assertEqual(len(calls), 1)
        self.repository.replace_teams([_team()])
        self.assertEqual(self.repository.memo(("probe",), compute), [{"n": 2}])

    def test_callers_get_private_copies(self):
        first = self.repository.memo(("rows",), lambda: [{"team": "GB"}])
        first[0]["team"] = "MUTATED"
        first.append({"team": "EXTRA"})
        self.assertEqual(self.repository.memo(("rows",), lambda: [{"team": "never"}]), [{"team": "GB"}])

    def test_memo_keys_are_per_database(self):
        other = NFLRepository(Path(self._directory.name) / "other.sqlite3")
        other.initialize()
        self.assertEqual(self.repository.memo(("k",), lambda: "a"), "a")
        self.assertEqual(other.memo(("k",), lambda: "b"), "b")

    def test_memoized_decorator_freezes_arguments_and_survives_unhashable_ones(self):
        calls = []

        class Probe:
            def __init__(self, repository):
                self.repository = repository

            def memo(self, key, compute):
                return self.repository.memo(key, compute)

            @memoized
            def metrics(self, season, names=(), *, flag=None):
                calls.append((season, tuple(names), flag))
                return {"season": season, "names": list(names)}

        probe = Probe(self.repository)
        probe.metrics(2025, ["a", "b"], flag=True)
        probe.metrics(2025, ("a", "b"), flag=True)          # list and tuple are the same key
        self.assertEqual(len(calls), 1)
        probe.metrics(2025, ["a", "b"], flag=False)
        self.assertEqual(len(calls), 2)
        probe.metrics(2025, [{"unhashable": object()}])      # falls back to computing directly
        self.assertEqual(len(calls), 3)

    def test_memo_is_bounded(self):
        original = repository_module.MEMO_LIMIT
        repository_module.MEMO_LIMIT = 3
        try:
            for index in range(10):
                self.repository.memo(("bounded", index), lambda index=index: index)
            with repository_module._MEMO_LOCK:
                mine = [key for key in repository_module._MEMO if "bounded" in key]
            self.assertLessEqual(len(mine), 3)
        finally:
            repository_module.MEMO_LIMIT = original

    def test_database_stamp_of_a_missing_file_is_stable(self):
        missing = str(Path(self._directory.name) / "nope.sqlite3")
        self.assertEqual(database_stamp(missing), database_stamp(missing))


class CloneTests(unittest.TestCase):
    def test_clone_is_independent_at_every_level(self):
        original = {"rows": [{"a": 1, "tags": ["x", "y"], "pair": (1, [2])}], "n": 3}
        copy_ = clone(original)
        self.assertEqual(copy_, original)
        copy_["rows"][0]["tags"].append("z")
        copy_["rows"][0]["pair"][1].append(3)
        copy_["rows"].append({})
        self.assertEqual(original, {"rows": [{"a": 1, "tags": ["x", "y"], "pair": (1, [2])}], "n": 3})

    def test_clone_handles_other_types_by_deep_copy(self):
        class Holder:
            def __init__(self):
                self.items = [1, 2]
        holder = Holder()
        duplicate = clone({"holder": holder})["holder"]
        duplicate.items.append(3)
        self.assertEqual(holder.items, [1, 2])

    def test_clone_is_much_faster_than_deepcopy_on_result_rows(self):
        import copy, time
        rows = [{f"m{i}": i * 1.5 for i in range(150)} for _ in range(600)]
        started = time.perf_counter(); copy.deepcopy(rows); slow = time.perf_counter() - started
        started = time.perf_counter(); clone(rows); fast = time.perf_counter() - started
        self.assertLess(fast * 3, slow, f"clone {fast:.3f}s vs deepcopy {slow:.3f}s")


class SeasonStatsEquivalenceTests(unittest.TestCase):
    """player_season_stats was rewritten into two joined passes; it must match the original single pass."""

    REFERENCE = '''SELECT player_id, MAX(player_name) player_name, MAX(team) team,
                          MAX(position) position, COUNT(DISTINCT game_id) games, {case}
                   FROM player_weekly_stats WHERE {where}
                   GROUP BY player_id HAVING games >= ?'''

    def setUp(self):
        import random
        self._directory = tempfile.TemporaryDirectory()
        self.repository = NFLRepository(Path(self._directory.name) / "nfl.sqlite3")
        self.repository.initialize()
        rng = random.Random(7)
        metrics = ["passing_yards", "attempts", "carries", "rushing_yards", "targets", "receiving_yards"]
        rows = []
        for player in range(40):
            position = rng.choice(["QB", "RB", "WR", "TE"])
            team = rng.choice(["GB", "CHI", "DET"])
            for week in range(1, 8):
                if rng.random() < 0.25:
                    continue                                             # missed a game
                game = f"2025_{week:02d}_{team}"
                # some player-weeks only have rows for unrelated metrics (they played, but not this role)
                for metric in rng.sample(metrics, rng.randint(1, len(metrics))):
                    rows.append((2025 if week < 7 else 2024, week, "REG", game, f"p{player}", f"Player {player}",
                                 team, "XXX", position, metric, round(rng.random() * 100, 2)))
        with repository_module.closing(self.repository._connect()) as connection:
            connection.executemany("INSERT OR REPLACE INTO player_weekly_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            connection.commit()

    def tearDown(self):
        self._directory.cleanup()

    def reference(self, season, metrics, *, team=None, position=None, week_from=None, week_to=None, minimum_games=1):
        where, params = ["season=?"], [season]
        for clause, value in (("team=?", team), ("position=?", position), ("week>=?", week_from), ("week<=?", week_to)):
            if value is not None:
                where.append(clause)
                params.append(value)
        case = ",".join(f'SUM(CASE WHEN metric=? THEN value END) "{metric}"' for metric in metrics)
        sql = self.REFERENCE.format(case=case, where=" AND ".join(where))
        with repository_module.closing(self.repository._connect()) as connection:
            return sorted((dict(row) for row in connection.execute(sql, [*metrics, *params, minimum_games])),
                          key=lambda row: row["player_id"])

    def assert_same(self, **kwargs):
        metrics = kwargs.pop("metrics")
        expected = self.reference(2025, metrics, **kwargs)
        actual = sorted(self.repository.player_season_stats(2025, metrics, **kwargs), key=lambda row: row["player_id"])
        self.assertEqual(len(actual), len(expected))
        for want, got in zip(expected, actual):
            self.assertEqual(set(want), set(got))
            for key, value in want.items():
                if isinstance(value, float):
                    self.assertAlmostEqual(value, got[key], places=6, msg=f"{want['player_id']} {key}")
                else:
                    self.assertEqual(value, got[key], f"{want['player_id']} {key}")

    def test_matches_the_single_pass_form_across_filters(self):
        metrics = ["passing_yards", "attempts", "carries", "rushing_yards", "targets", "receiving_yards"]
        self.assert_same(metrics=metrics)
        self.assert_same(metrics=metrics[:2])
        self.assert_same(metrics=["targets"], position="WR")
        self.assert_same(metrics=metrics, team="GB", minimum_games=3)
        self.assert_same(metrics=metrics, week_from=2, week_to=4)
        self.assert_same(metrics=["rushing_yards", "carries"], position="RB", minimum_games=2, week_from=3)

    def test_a_metric_no_player_recorded_is_null_not_zero_and_games_still_count(self):
        result = self.repository.player_season_stats(2025, ["passing_yards", "not_a_metric"])
        self.assertTrue(result)
        self.assertTrue(all(row["not_a_metric"] is None for row in result))
        self.assertTrue(all(row["games"] >= 1 for row in result))

    def test_stat_positions_matches_the_old_pivot_based_derivation(self):
        old = sorted({row["position"] for row in self.repository.player_season_stats(2025, ("attempts",), minimum_games=1)
                      if row.get("position")})
        self.assertEqual(self.repository.stat_positions(2025), old)
        self.assertEqual(self.repository.stat_positions(1999), [])

    def test_no_metrics_is_an_empty_result(self):
        self.assertEqual(self.repository.player_season_stats(2025, []), [])


if __name__ == "__main__":
    unittest.main()


class StatDiskCacheTests(unittest.TestCase):
    """player_season_stats & co. persist per season and stay valid only until that season's stats are rewritten."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "nfl.sqlite3"
        self.repository = NFLRepository(self.path)
        self.repository.initialize()

    def tearDown(self):
        self._directory.cleanup()

    def _rows(self, season, yards):
        return [{"season": season, "week": 1, "season_type": "REG", "game_id": f"{season}_01_A_B",
                 "player_id": "p1", "player_name": "P One", "team": "A", "opponent_team": "B",
                 "position": "WR", "receiving_yards": yards}]

    def _yards(self, season):
        rows = self.repository.player_season_stats(season, ("receiving_yards",))
        return rows[0]["receiving_yards"] if rows else None

    def test_rewriting_one_season_leaves_the_other_cached_and_refreshes_itself(self):
        self.repository.replace_weekly_stats(2025, self._rows(2025, 100))
        self.repository.replace_weekly_stats(2026, self._rows(2026, 10))
        self.assertEqual((self._yards(2025), self._yards(2026)), (100, 10))
        cache = self.path.parent / "stat_cache"
        before = {item.name for item in cache.glob("2025-v*.pkl")}
        self.assertTrue(before)
        self.repository.replace_weekly_stats(2026, self._rows(2026, 55))
        self.assertEqual(self._yards(2026), 55)                      # new counter: recomputed
        self.assertEqual({item.name for item in cache.glob("2025-v*.pkl")}, before)   # 2025 untouched
        self.assertEqual(self._yards(2025), 100)

    def test_upsert_also_invalidates(self):
        self.repository.replace_weekly_stats(2026, self._rows(2026, 10))
        self.assertEqual(self._yards(2026), 10)
        self.repository.upsert_weekly_stats(self._rows(2026, 40))
        self.assertEqual(self._yards(2026), 40)

    def test_results_come_from_disk_after_the_process_memo_is_gone(self):
        self.repository.replace_weekly_stats(2025, self._rows(2025, 100))
        self.assertEqual(self._yards(2025), 100)
        repository_module._MEMO.clear()
        original = NFLRepository._connect
        queries = []

        def spy(repository):
            connection = original(repository)
            connection.set_trace_callback(queries.append)
            return connection
        NFLRepository._connect = spy
        try:
            self.assertEqual(self._yards(2025), 100)
        finally:
            NFLRepository._connect = original
        self.assertFalse([query for query in queries if "player_weekly_stats" in query])
