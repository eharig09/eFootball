"""margin-v2's live feature wiring, including the red-zone differential
feature validated by margin_feature_ablation.py's walk-forward test."""
from __future__ import annotations

import unittest

from sports_aggregator.cfb import live_margin_calibration as lmc


class LiveFeaturesTests(unittest.TestCase):
    def test_red_zone_diff_is_home_minus_away_scoring_rate(self):
        projection = {
            "home": {"expected_points": 28.0, "points_per_drive": 2.3, "drives": 12.0,
                     "red_zone_scoring_rate": 0.30},
            "away": {"expected_points": 21.0, "points_per_drive": 1.9, "drives": 11.0,
                     "red_zone_scoring_rate": 0.22},
        }
        features = lmc.live_features(projection)
        self.assertAlmostEqual(features["red_zone_diff"], 0.08)

    def test_red_zone_diff_is_none_when_either_side_lacks_the_rate(self):
        projection = {
            "home": {"expected_points": 28.0, "points_per_drive": 2.3, "drives": 12.0,
                     "red_zone_scoring_rate": None},
            "away": {"expected_points": 21.0, "points_per_drive": 1.9, "drives": 11.0,
                     "red_zone_scoring_rate": 0.22},
        }
        features = lmc.live_features(projection)
        self.assertIsNone(features["red_zone_diff"])

    def test_missing_projection_sides_do_not_raise(self):
        features = lmc.live_features({})
        self.assertIsNone(features["red_zone_diff"])
        self.assertIsNone(features["raw_margin"])


class FeatureSetOrderingTests(unittest.TestCase):
    def test_richest_tier_is_plus_hc_qb_elo_falling_back_through_redzone_to_recent(self):
        names = [name for name, _ in lmc.FEATURE_SETS]
        self.assertEqual(names[0], "plus_hc_qb_elo")
        self.assertEqual(names[1], "plus_redzone")
        self.assertEqual(names[2], "plus_recent")
        by_name = dict(lmc.FEATURE_SETS)
        richest, mid, base = by_name["plus_hc_qb_elo"], by_name["plus_redzone"], by_name["plus_recent"]
        self.assertNotIn("red_zone_diff", base)
        self.assertEqual(set(mid) - set(base), {"red_zone_diff"})
        # plus_hc_qb_elo is plus_redzone plus exactly the two new features --
        # never a silent respecification of an already-validated tier.
        self.assertEqual(set(richest) - set(mid), {"hc_diff", "qb_diff"})


class HcQbDiffTests(unittest.TestCase):
    def test_none_game_returns_none_none(self):
        self.assertEqual(lmc._hc_qb_diff(repository=None, game=None), (None, None))

    def test_missing_game_id_returns_none_none(self):
        self.assertEqual(lmc._hc_qb_diff(repository=None, game={}), (None, None))


class SideRedZoneRateTests(unittest.TestCase):
    def test_shrinks_toward_league_at_low_sample_size(self):
        row = {
            "team_prior_trips_per_drive": 0.40, "opponent_prior_trips_allowed_per_drive": 0.30,
            "team_prior_red_zone_td_rate": 0.90, "opponent_prior_red_zone_td_rate_allowed": 0.80,
            "league_prior_trips_per_drive": 0.25, "league_prior_red_zone_td_rate": 0.60,
            "rz_team_games": 1, "rz_opp_games": 1,
        }
        rate = lmc._side_red_zone_rate(row)
        # With only 1 prior game each, the shrunk trips/TD-rate blends sit
        # well below the raw matchup blend (0.35 trips x 0.85 td = .2975)
        # and well above the pure league read (0.25 x 0.60 = .15).
        self.assertGreater(rate, 0.15)
        self.assertLess(rate, 0.2975)

    def test_returns_none_without_a_league_rate(self):
        self.assertIsNone(lmc._side_red_zone_rate({}))


if __name__ == "__main__":
    unittest.main()
