"""The incremental feature sets margin_feature_ablation.py tests, and the
turnover/red-zone differential helpers those sets are built from."""
from __future__ import annotations

import unittest

from sports_aggregator.cfb import margin_feature_ablation as mfa


class FeatureSetShapeTests(unittest.TestCase):
    def test_turnover_and_redzone_tiers_extend_plus_recent_by_one_feature(self):
        base = set(mfa.FEATURE_SETS["plus_recent"])
        self.assertEqual(set(mfa.FEATURE_SETS["plus_turnovers"]) - base, {"turnover_diff"})
        self.assertEqual(set(mfa.FEATURE_SETS["plus_redzone"]) - base, {"red_zone_diff"})
        self.assertEqual(
            set(mfa.FEATURE_SETS["plus_turnovers_redzone"]) - base,
            {"turnover_diff", "red_zone_diff"},
        )


class SideRateHelperTests(unittest.TestCase):
    def test_turnover_rate_falls_back_to_league_without_a_team_blend(self):
        row = {"league_prior_giveaway_rate": 0.05}
        self.assertAlmostEqual(mfa._side_turnover_rate(row), 0.05)

    def test_turnover_rate_is_none_without_any_league_prior(self):
        self.assertIsNone(mfa._side_turnover_rate({}))

    def test_red_zone_rate_blends_trips_and_td_rate(self):
        row = {
            "team_prior_trips_per_drive": 0.35, "opponent_prior_trips_allowed_per_drive": 0.35,
            "team_prior_red_zone_td_rate": 0.70, "opponent_prior_red_zone_td_rate_allowed": 0.70,
            "league_prior_trips_per_drive": 0.35, "league_prior_red_zone_td_rate": 0.70,
            "rz_team_games": 20, "rz_opp_games": 20,
        }
        # Team, opponent-allowed and league all agree here, so shrinkage is a
        # no-op and the result is exactly trips x td_rate.
        self.assertAlmostEqual(mfa._side_red_zone_rate(row), 0.35 * 0.70)

    def test_red_zone_rate_is_none_without_a_league_prior(self):
        self.assertIsNone(mfa._side_red_zone_rate({}))


if __name__ == "__main__":
    unittest.main()
