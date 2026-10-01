import unittest

from sports_aggregator.charting_edges import PRIOR_ATTEMPTS, interaction_share, lean, overall_edge, shrink, zone_edge


class ShrinkTests(unittest.TestCase):
    def test_more_volume_keeps_more_of_the_signal(self):
        self.assertAlmostEqual(shrink(1.0, PRIOR_ATTEMPTS), 0.5)            # prior attempts keep half
        self.assertGreater(shrink(1.0, 60), shrink(1.0, 6))
        self.assertLess(shrink(1.0, 1000), 1.0)
        self.assertIsNone(shrink(None, 10))
        self.assertIsNone(shrink(1.0, 0))

    def test_a_hot_zone_on_a_few_plays_loses_to_a_solid_one_on_many(self):
        thin = shrink(1.80, 2)          # +1.80 on two plays
        deep = shrink(0.35, 60)         # +0.35 on sixty
        self.assertLess(thin, deep)


class ZoneEdgeTests(unittest.TestCase):
    def test_sum_and_mean_conventions(self):
        total = zone_edge(.3, 24, .5, 12, combine="sum")
        mean = zone_edge(.3, 24, .5, 12, combine="mean")
        self.assertAlmostEqual(total["raw"], .8)
        self.assertAlmostEqual(mean["raw"], .4)
        self.assertAlmostEqual(total["edge"], .3 * 24 / 36 + .5 * 12 / 24)
        self.assertAlmostEqual(mean["edge"], total["edge"] / 2)

    def test_the_thinner_side_limits_reliability(self):
        self.assertAlmostEqual(zone_edge(.3, 4, .5, 100)["reliability"], 4 / 16)

    def test_missing_side_is_not_comparable(self):
        self.assertIsNone(zone_edge(.3, 0, .5, 10)["edge"])
        self.assertIsNone(zone_edge(None, 10, .5, 10)["edge"])


class OverallTests(unittest.TestCase):
    def test_busy_zones_decide_the_headline(self):
        overall = overall_edge([(0.50, 0.05), (0.10, 0.80)])
        self.assertAlmostEqual(overall["edge"], (0.50 * 0.05 + 0.10 * 0.80) / 0.85)
        self.assertEqual(overall["lean"], "offense")

    def test_empty_and_even(self):
        self.assertEqual(overall_edge([(None, 0.5), (0.2, 0)])["lean"], "neutral")
        self.assertEqual(lean(0.01), "even")
        self.assertEqual(lean(-0.2), "defense")

    def test_interaction_share_is_the_geometric_mean_of_the_two_shares(self):
        self.assertAlmostEqual(interaction_share(4, 10, 9, 36), (0.4 * 0.25) ** .5)
        self.assertIsNone(interaction_share(4, 0, 9, 36))


if __name__ == "__main__":
    unittest.main()
