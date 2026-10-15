"""The scheduled FPI step also fills in missing prior seasons, so margin-v2 can fit its FPI tiers."""

from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from sports_aggregator.cfb.models_cli import FPI_HISTORY_SEASONS, missing_fpi_seasons
from sports_aggregator.cfb.repository import CFBRepository


class MissingFpiSeasonsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.repository = CFBRepository(Path(self.directory.name) / "cfb.sqlite3")
        self.repository.initialize()

    def store(self, *seasons):
        with closing(self.repository._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS fpi_game_projections (season INTEGER, game_id INTEGER)")
            connection.executemany("INSERT INTO fpi_game_projections VALUES (?,?)",
                                   [(season, index) for index, season in enumerate(seasons)])
            connection.commit()

    def test_a_database_with_no_fpi_table_wants_every_prior_season(self):
        self.assertEqual(missing_fpi_seasons(self.repository, 2026), [2022, 2023, 2024, 2025])
        self.assertEqual(FPI_HISTORY_SEASONS, 4)

    def test_only_seasons_without_rows_are_requested(self):
        self.store(2022, 2023, 2026)
        self.assertEqual(missing_fpi_seasons(self.repository, 2026), [2024, 2025])

    def test_a_complete_history_requests_nothing(self):
        self.store(2022, 2023, 2024, 2025, 2026)
        self.assertEqual(missing_fpi_seasons(self.repository, 2026), [])

    def test_the_current_season_is_not_part_of_the_history(self):
        self.store(2026)
        self.assertNotIn(2026, missing_fpi_seasons(self.repository, 2026))


if __name__ == "__main__":
    unittest.main()
