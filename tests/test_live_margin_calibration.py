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


class MissingEloTests(unittest.TestCase):
    """A team with no pregame Elo (almost always an FCS opponent CFBD
    doesn't rate) must not be assessed by margin-v2 at all -- applying a
    model fit on well-matched FBS games to that kind of blowout produced
    wildly unstable margins (see the module docstring)."""

    def _rich_row(self, **overrides):
        row = {
            "raw_margin": 3.0, "ppd_diff": 0.2, "drive_diff": 1.0, "elo_diff": 50.0,
            "core_margin": 2.0, "fpi_margin": 1.5, "recent_margin_diff": 4.0,
            "red_zone_diff": 0.05, "hc_diff": 10.0, "qb_diff": -5.0,
        }
        row.update(overrides)
        return row

    def test_no_tier_requires_only_raw_margin_ppd_drive(self):
        # The old elo-less "base" tier is gone -- every remaining tier
        # requires elo_diff.
        for _, features in lmc.FEATURE_SETS:
            self.assertIn("elo_diff", features)

    def test_predict_with_models_skips_a_row_missing_elo(self):
        history = [self._rich_row(actual_margin=float(i % 9) - 4) for i in range(150)]
        models = lmc.fit_models(history)
        label, value, model = lmc.predict_with_models(models, self._rich_row(elo_diff=None))
        self.assertIsNone(label)
        self.assertIsNone(value)
        self.assertIsNone(model)

    def test_predict_live_reports_not_assessed_instead_of_a_raw_fallback_value(self):
        from unittest.mock import patch

        projection = {
            "home": {"expected_points": 28.0, "points_per_drive": 2.3, "drives": 12.0},
            "away": {"expected_points": 3.0, "points_per_drive": 0.4, "drives": 11.0},
            "opponent_quality": {"home": {"components": {}}},
        }
        # elo_diff is None (no quality components supplied) with no repository
        # history behind it -> the old code returned a raw_margin fallback
        # that looked like a real prediction; it must now report "not
        # assessed" instead.
        with patch.object(lmc, "_historical_rows", return_value=[]):
            result = lmc.predict_live(
                repository=None, target_season=2026, projection=projection, game=None)
        self.assertIsNone(result["value"])
        self.assertEqual(result["variant"], "not_assessed_missing_elo")


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
