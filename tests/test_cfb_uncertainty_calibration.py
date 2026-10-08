import math
import random
import unittest

from sports_aggregator.cfb import uncertainty_calibration as U


def _history(seasons=(2021, 2022, 2023), per_season=400, noise=14.0, seed=3):
    rng = random.Random(seed)
    rows = []
    for season in seasons:
        for index in range(per_season):
            skill = rng.gauss(0, 10)
            rows.append({
                "game_id": season * 10000 + index, "season": season,
                "raw_margin": skill, "ppd_diff": skill / 10, "drive_diff": rng.gauss(0, 1),
                "elo_diff": skill * 2, "core_margin": skill, "fpi_margin": skill,
                "recent_margin_diff": skill, "red_zone_diff": rng.gauss(0, .1),
                "hc_diff": None, "qb_diff": None,
                "actual_margin": skill + rng.gauss(0, noise)})
    return rows


class UncertaintyCalibrationTests(unittest.TestCase):
    def test_scale_uses_only_earlier_seasons(self):
        residuals = U.out_of_fold(_history())
        self.assertEqual({row["season"] for row in residuals}, {2022, 2023})
        self.assertIsNone(U.scale_for(residuals, 2022))     # nothing earlier, nothing served
        scale = U.scale_for(residuals, 2023)
        self.assertAlmostEqual(scale["margin_residual_scale"], 14.0, delta=1.5)

    def test_gaussian_is_calibrated_and_beats_the_base_rate(self):
        report = U.evaluate(_history(seasons=(2021, 2022, 2023, 2024)))
        pooled = report["pooled"]
        self.assertLess(pooled["log_loss_gaussian"], pooled["log_loss_base_rate"])
        self.assertAlmostEqual(pooled["coverage"]["80%"], 0.80, delta=0.05)

    def test_probability_and_intervals_follow_the_margin(self):
        history = _history()
        scale = U.scale_for(U.out_of_fold(history), 2024)
        original = U._scale_table
        U._scale_table = lambda repository, season: scale
        try:
            even = U.live_packet(None, target_season=2023, margin=0.0)
            favourite = U.live_packet(None, target_season=2023, margin=14.0)
            self.assertEqual(even["home_win_probability"], 0.5)
            self.assertGreater(favourite["home_win_probability"], 0.8)
            low, high = favourite["intervals"]["80%"]
            self.assertAlmostEqual((low + high) / 2, 14.0, places=1)
            self.assertIsNone(U.live_packet(None, target_season=2023, margin=None))
        finally:
            U._scale_table = original


if __name__ == "__main__":
    unittest.main()
