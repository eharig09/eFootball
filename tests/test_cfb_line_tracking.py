import tempfile
import unittest
from pathlib import Path

from sports_aggregator.cfb import line_tracking
from sports_aggregator.cfb.lines import game_lines, line_history, store_lines
from sports_aggregator.cfb.repository import CFBRepository


def _payload(spread, total):
    return [{"id": 1, "lines": [
        {"provider": "A", "spread": spread, "spreadOpen": -3, "overUnder": total, "overUnderOpen": 50},
        {"provider": "B", "spread": spread + 1, "spreadOpen": -3, "overUnder": total, "overUnderOpen": 50}]}]


class LineTrackingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.repository = CFBRepository(Path(self.folder.name) / "cfb.sqlite3")
        self.repository.initialize()

    def tearDown(self):
        self.folder.cleanup()

    def test_history_only_records_a_move(self):
        store_lines(self.repository, 2026, _payload(-4, 51))
        store_lines(self.repository, 2026, _payload(-4, 51))
        store_lines(self.repository, 2026, _payload(-5, 52))
        readings = line_history(self.repository, 1)
        self.assertEqual([(r["spread"], r["over_under"]) for r in readings], [(-3.5, 51.0), (-4.5, 52.0)])

    def test_chart_marks_a_confirmed_pick_with_the_team_logo(self):
        store_lines(self.repository, 2026, _payload(-4, 51))
        market = game_lines(self.repository, 1)
        history = [{"recorded_at": "2020-01-01T00:00:00+00:00", "selected_team": "X",
                    "state": "engine_a_only", "market_spread": -3.5}]
        packet = line_tracking.build(
            {"home_team": "X", "away_team": "Y"}, market, line_history(self.repository, 1), history,
            {"margin": 6, "total": 48}, None, {"X": {"logo": "x.png"}, "Y": {"logo": "y.png"}})
        self.assertEqual([m["logo"] for m in packet["spread_chart"]["marks"]], ["x.png"])
        self.assertEqual(packet["picks"][0]["engine"], "Engine A")
        self.assertEqual(packet["spread_chart"]["series"][1]["label"], "Football Lab")

    def test_no_projection_still_draws_the_market(self):
        store_lines(self.repository, 2026, _payload(-4, 51))
        packet = line_tracking.build({"home_team": "X", "away_team": "Y"}, game_lines(self.repository, 1),
                                     line_history(self.repository, 1), [], None, None, {})
        self.assertTrue(packet["total_chart"]["has_data"])
        self.assertEqual(len(packet["total_chart"]["series"]), 1)
        self.assertEqual(packet["picks"], [])


if __name__ == "__main__":
    unittest.main()
