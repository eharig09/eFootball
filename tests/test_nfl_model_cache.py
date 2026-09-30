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

    def test_warm_builds_every_cached_piece_and_is_instant_the_second_time(self):
        first = model_cache.warm(self.repository, 2025)
        self.assertEqual(set(first), {"core_oof", "game_rows", "calibrated_oof", "drive_rows"})
        names = {path.name.split("-")[0] for path in cache_dir(self.repository).glob("*.pkl")}
        self.assertEqual(names, {"core_oof", "game_rows", "calibrated_oof", "drive_rows"})
        model_cache.clear_memory()
        second = model_cache.warm(self.repository, 2025)
        self.assertLess(sum(second.values()), max(sum(first.values()), 0.5))

    def test_code_hash_is_stable_and_covers_the_model_modules(self):
        first = model_cache.code_hash()
        self.assertEqual(first, model_cache.code_hash())
        self.assertEqual(len(first), 16)


if __name__ == "__main__":
    unittest.main()
