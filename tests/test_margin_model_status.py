"""The data-status margin-model panel reports which tier a database can reach and why."""

from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sports_aggregator.cfb import margin_model_status as status
from sports_aggregator.cfb.live_margin_calibration import FEATURE_SETS
from sports_aggregator.cfb.repository import CFBRepository


def _model(rows):
    return {"training_rows": rows, "beta": [0.0], "means": {}, "scales": {}}


class MarginModelStatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.repository = CFBRepository(Path(self.directory.name) / "cfb.sqlite3")
        self.repository.initialize()
        with closing(self.repository._connect()) as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS fpi_game_projections (season INTEGER, game_id INTEGER);"
                "CREATE TABLE IF NOT EXISTS cfb_projection_backtest_meta "
                "(season INTEGER, games INTEGER, inputs_version TEXT);"
                "INSERT INTO cfb_projection_backtest_meta VALUES (2024,900,'v'),(2025,930,'v');")
            connection.commit()

    def run_with(self, models):
        with patch.object(status, "_season", return_value=2026),                 patch.object(status, "_fitted_models", return_value=models):
            return status.margin_model_status(self.repository)

    def test_the_richest_fitted_tier_is_the_best_reachable(self):
        report = self.run_with({label: _model(500) for label, _ in FEATURE_SETS})
        self.assertEqual(report["best"], FEATURE_SETS[0][0])
        self.assertEqual([tier["label"] for tier in report["tiers"]], [label for label, _ in FEATURE_SETS])
        self.assertIn("is fitted", report["note"])

    def test_missing_fpi_history_explains_the_fall_back_to_the_base_tier(self):
        models = {label: None for label, _ in FEATURE_SETS}
        models["plus_elo"] = _model(3500)
        report = self.run_with(models)
        self.assertEqual(report["best"], "plus_elo")
        self.assertEqual(report["missing_fpi_seasons"], [2024, 2025])
        self.assertIn("no FPI history for 2024, 2025", report["note"])
        self.assertFalse(report["tiers"][0]["fitted"])
        self.assertEqual(report["tiers"][-1]["training_rows"], 3500)

    def test_no_fitted_tier_is_stated_plainly(self):
        report = self.run_with({label: None for label, _ in FEATURE_SETS})
        self.assertIsNone(report["best"])
        self.assertIn("raw projection", report["note"])

    def test_a_fit_failure_is_reported_not_raised(self):
        with patch.object(status, "_season", return_value=2026),                 patch.object(status, "_fitted_models", side_effect=RuntimeError("boom")):
            report = status.margin_model_status(self.repository)
        self.assertFalse(report["available"])
        self.assertIn("boom", report["reason"])


if __name__ == "__main__":
    unittest.main()
