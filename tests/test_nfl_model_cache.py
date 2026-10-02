import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sports_aggregator.nfl import model_cache
from sports_aggregator.nfl.model_cache import cache_dir, history_cached, history_fingerprint
from sports_aggregator.nfl.models import Game
from sports_aggregator.nfl.repository import NFLRepository


def _game(game_id, season, home_score=24):
    return Game(game_id=game_id, season=season, season_type="REG", week=1, game_date=f"{season}-09-07",
                game_time="13:00", away_team="GNB", home_team="CHI", away_score=17, home_score=home_score,
                overtime=False, division_game=True, stadium=None, roof=None, surface=None, temperature=None,
                wind=None, spread_line=-3.0, total_line=44.0)


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.repository = NFLRepository(Path(self._directory.name) / "nfl.sqlite3")
        self.repository.initialize()
        self.repository.replace_games(2024, [_game("2024_01_GNB_CHI", 2024)])
        self.calls = []

        @history_cached("probe")
        def compute(repository, *, start_season, end_season, flavor="a"):
            self.calls.append((start_season, end_season, flavor))
            return [{"end": end_season, "call": len(self.calls)}]

        self.compute = compute
        model_cache.clear_memory()

    def tearDown(self):
        model_cache.clear_memory()
        self._directory.cleanup()

    def test_computes_once_then_serves_memory_and_disk(self):
        self.assertEqual(self.compute(self.repository, start_season=2010, end_season=2024)[0]["call"], 1)
        self.assertEqual(self.compute(self.repository, start_season=2010, end_season=2024)[0]["call"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(list(cache_dir(self.repository).glob("probe-*.pkl"))), 1)
        model_cache.clear_memory()                                     # a new process: disk still answers
        self.assertEqual(self.compute(self.repository, start_season=2010, end_season=2024)[0]["call"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_different_arguments_are_different_entries(self):
        self.compute(self.repository, start_season=2010, end_season=2024)
        self.compute(self.repository, start_season=2010, end_season=2024, flavor="b")
        self.compute(self.repository, start_season=2016, end_season=2024)
        self.assertEqual(len(self.calls), 3)

    def test_changed_history_retires_the_entry_and_the_stale_file(self):
        self.compute(self.repository, start_season=2010, end_season=2024)
        self.repository.replace_games(2024, [_game("2024_01_GNB_CHI", 2024, home_score=30)])   # a corrected score
        self.assertEqual(self.compute(self.repository, start_season=2010, end_season=2024)[0]["call"], 2)
        self.assertEqual(len(list(cache_dir(self.repository).glob("probe-*.pkl"))), 1)        # stale one removed

    def test_later_seasons_do_not_invalidate_earlier_results(self):
        before = history_fingerprint(self.repository, 2024)
        self.repository.replace_games(2025, [_game("2025_01_GNB_CHI", 2025)])
        self.assertEqual(history_fingerprint(self.repository, 2024), before)
        self.assertNotEqual(history_fingerprint(self.repository, 2025), before)
        self.compute(self.repository, start_season=2010, end_season=2024)
        self.repository.replace_games(2025, [_game("2025_01_GNB_CHI", 2025, home_score=10)])
        self.compute(self.repository, start_season=2010, end_season=2024)
        self.assertEqual(len(self.calls), 1)

    def test_editing_the_model_code_retires_the_cache(self):
        self.compute(self.repository, start_season=2010, end_season=2024)
        with mock.patch.object(model_cache, "code_hash", return_value="different-model"):
            self.compute(self.repository, start_season=2010, end_season=2024)
        self.assertEqual(len(self.calls), 2)

    def test_a_corrupt_cache_file_is_recomputed(self):
        self.compute(self.repository, start_season=2010, end_season=2024)
        model_cache.clear_memory()
        (path,) = cache_dir(self.repository).glob("probe-*.pkl")
        path.write_bytes(b"not a pickle")
        self.assertEqual(self.compute(self.repository, start_season=2010, end_season=2024)[0]["call"], 2)

    def test_callers_get_private_copies(self):
        first = self.compute(self.repository, start_season=2010, end_season=2024)
        first[0]["end"] = "MUTATED"
        self.assertEqual(self.compute(self.repository, start_season=2010, end_season=2024)[0]["end"], 2024)

    def test_non_repository_objects_bypass_the_cache(self):
        self.compute(object(), start_season=2010, end_season=2024)
        self.compute(object(), start_season=2010, end_season=2024)
        self.assertEqual(len(self.calls), 2)

    def test_an_unwritable_cache_directory_still_returns_the_value(self):
        with mock.patch.object(model_cache.Path, "mkdir", side_effect=OSError("read-only")):
            self.assertEqual(self.compute(self.repository, start_season=2010, end_season=2024)[0]["call"], 1)

    def test_the_code_hash_follows_model_modules_but_not_infrastructure(self):
        modules = model_cache.code_modules()
        for name in ("drive_projection", "scoring_bridge", "score_calibration", "uncertainty_calibration"):
            self.assertIn(name, modules)
        for name in ("repository", "web", "views", "models", "sync"):
            self.assertNotIn(name, modules)        # editing these must not retire a cache that takes minutes to rebuild

    def test_a_scoped_code_hash_is_independent_of_the_default_one(self):
        default_before = model_cache.code_hash()
        scoped = model_cache.code_hash(("live_margin", "qb_player_ablation"))
        self.assertNotEqual(scoped, default_before)
        self.assertEqual(model_cache.code_hash(), default_before)          # adding a scoped cache never retires the old ones
        self.assertEqual(model_cache.code_hash(("live_margin", "qb_player_ablation")), scoped)
        self.assertIn("margin_strength_ablation", model_cache.code_modules(("live_margin",)))   # imports are followed
        self.assertEqual(model_cache.code_modules(("line_elo",)), ["line_elo"])         # a scope really is narrower than the default

    def test_extra_fingerprint_queries_retire_the_entry_when_their_table_changes(self):
        calls = []

        @history_cached("extra", extra_queries=("SELECT COUNT(*) FROM team_venues WHERE ?>0",))
        def compute(repository, *, start_season, end_season):
            calls.append(1)
            return len(calls)

        self.assertEqual(compute(self.repository, start_season=2010, end_season=2024), 1)
        self.assertEqual(compute(self.repository, start_season=2010, end_season=2024), 1)
        from contextlib import closing
        with closing(self.repository._connect()) as connection:
            connection.execute("DELETE FROM team_venues")                    # a table only the extra query watches
            connection.commit()
        model_cache.clear_memory()
        self.assertEqual(compute(self.repository, start_season=2010, end_season=2024), 2)

    def test_the_live_margin_fit_is_computed_once_and_then_served_from_the_cache(self):
        from sports_aggregator.nfl import live_margin
        with mock.patch.object(live_margin, "build_rows", return_value=[]) as build:
            first = live_margin.fit(self.repository, 2026)
            second = live_margin.fit(self.repository, 2026)
            model_cache.clear_memory()                                       # a new process: the disk copy answers
            third = live_margin.fit(self.repository, 2026)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(first, (None, None))
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_warm_builds_every_cached_piece_and_is_instant_the_second_time(self):
        first = model_cache.warm(self.repository, 2025)
        expected = {"core_oof", "game_rows", "calibrated_oof", "drive_rows", "live_margin_fit"}
        self.assertEqual(set(first), expected)
        names = {path.name.split("-")[0] for path in cache_dir(self.repository).glob("*.pkl")}
        self.assertEqual(names, expected)
        model_cache.clear_memory()
        second = model_cache.warm(self.repository, 2025)
        self.assertLess(sum(second.values()), max(sum(first.values()), 0.5))

    def test_code_hash_is_stable_and_covers_the_model_modules(self):
        first = model_cache.code_hash()
        self.assertEqual(first, model_cache.code_hash())
        self.assertEqual(len(first), 16)


if __name__ == "__main__":
    unittest.main()
