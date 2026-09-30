import unittest

from sports_aggregator.nfl.team_panels import defense_panel, percentile, ranked_stats_panel, tone


class TeamPanelTests(unittest.TestCase):
    def test_percentile_is_direction_free_rank_scaling(self):
        self.assertEqual(percentile(1, 32), 100)
        self.assertEqual(percentile(32, 32), 0)
        self.assertEqual(percentile(16, 31), 50)
        self.assertIsNone(percentile(None, 32))
        self.assertEqual(percentile(1, 1), 100)

    def test_tone_bands_and_neutral_override(self):
        self.assertEqual(tone(90), "good")
        self.assertEqual(tone(50), "mid")
        self.assertEqual(tone(10), "poor")
        self.assertEqual(tone(90, neutral=True), "neutral")
        self.assertEqual(tone(None), "neutral")

    def test_defense_panel_reuses_ranked_rows_and_grays_identity_stats(self):
        profile = {"season": 2025, "groups": [{"label": "Next Gen allowed", "rows": [
            {"label": "Time to throw faced", "key": "pass_time_to_throw", "value": 2.7,
             "format": "f2", "rank": 3, "of": 32},
            {"label": "CPOE allowed", "key": "pass_cpoe", "value": -1.0,
             "format": "pct", "rank": 30, "of": 32},
        ]}]}
        rows = defense_panel(profile)["groups"][0]["rows"]
        self.assertEqual(rows[0]["tone"], "neutral")
        self.assertEqual(rows[1]["tone"], "poor")
        self.assertEqual(rows[1]["pctl"], 6)

    def test_ranked_stats_panel_keeps_ranked_rows_and_drops_missing_values(self):
        panel = ranked_stats_panel((("Production", [
            {"label": "Yards", "key": "y", "value": 4000, "format": "big", "rank": 1, "of": 30},
            {"label": "Ghost", "key": "g", "value": None, "rank": 2, "of": 30},
        ]), ("Empty", [])))
        self.assertEqual([g["label"] for g in panel["groups"]], ["Production"])
        self.assertEqual(panel["groups"][0]["rows"][0]["pctl"], 100)
        self.assertEqual(len(panel["groups"][0]["rows"]), 1)


if __name__ == "__main__":
    unittest.main()
