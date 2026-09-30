import unittest

from sports_aggregator.nfl.draft_trade_value import combined_value, pick_value


class PickValueTests(unittest.TestCase):
    def test_the_first_pick_is_worth_the_most(self):
        self.assertEqual(pick_value(1), 3000)
        self.assertGreater(pick_value(1), pick_value(2))

    def test_value_decreases_as_the_pick_number_increases(self):
        values = [pick_value(pick) for pick in range(1, 225)]
        self.assertTrue(all(a >= b for a, b in zip(values, values[1:])))

    def test_a_pick_beyond_the_tabulated_chart_falls_back_to_the_floor_value(self):
        self.assertEqual(pick_value(225), pick_value(224))
        self.assertEqual(pick_value(500), pick_value(224))

    def test_pick_zero_or_negative_has_no_value(self):
        self.assertIsNone(pick_value(0))
        self.assertIsNone(pick_value(-1))


class CombinedValueTests(unittest.TestCase):
    def test_a_package_sums_its_picks(self):
        self.assertEqual(combined_value([1, 32]), pick_value(1) + pick_value(32))

    def test_an_empty_package_is_worth_nothing(self):
        self.assertEqual(combined_value([]), 0)


if __name__ == "__main__":
    unittest.main()
