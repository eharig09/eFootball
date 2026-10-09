"""The totals-regime audit: in-sample table vs the walk-forward test that decides whether it predicts anything."""
from __future__ import annotations

import unittest

from sports_aggregator.cfb import matchup_research as mr
from sports_aggregator.cfb import totals_regime_audit as tra


def row(season, bucket, move, result, residual=1.0):
    return {"season": season, "opening_edge_bucket": bucket, "movement_state": move,
            "closing_result": result, "close_result_aligned": residual if result == "win" else -residual}


def cell(season, bucket, move, wins, losses):
    return [row(season, bucket, move, "win") for _ in range(wins)] + [row(season, bucket, move, "loss") for _ in range(losses)]


class RegimeTableTests(unittest.TestCase):
    def test_win_rate_games_and_mean_residual_per_cell_for_the_three_large_buckets(self):
        rows = cell(2023, "5-7.99", "unchanged", 6, 4) + cell(2023, "<1", "unchanged", 9, 1)
        table = tra.regime_table(rows)
        self.assertEqual(set(table), {("5-7.99", "unchanged")})        # buckets under 3 points are not tabulated
        self.assertEqual(table[("5-7.99", "unchanged")], (60.0, 10, 0.2))

    def test_overall_counts_pushes_separately(self):
        rows = cell(2023, "8+", "away_1_plus", 3, 2) + [row(2023, "8+", "away_1_plus", "push")]
        self.assertEqual(tra.overall(rows)["record"], "3-2-1")
        self.assertEqual(tra.overall(rows)["win_rate"], 60.0)


class WalkForwardTests(unittest.TestCase):
    def test_cells_are_chosen_on_earlier_seasons_only(self):
        # a cell that looks great in 2022 and then loses in 2023: the pick must be made from 2022 alone
        rows = cell(2022, "8+", "unchanged", 24, 8) + cell(2023, "8+", "unchanged", 10, 20)
        out = tra.walk_forward(rows)
        self.assertEqual(out["picks"], 30)
        self.assertEqual((out["wins"], out["losses"]), (10, 20))
        self.assertEqual(out["by_season"][2023]["cells"], 1)

    def test_a_cell_that_never_cleared_the_bar_is_not_followed(self):
        rows = cell(2022, "8+", "unchanged", 15, 17) + cell(2023, "8+", "unchanged", 30, 0)   # 46.9% in 2022
        self.assertEqual(tra.walk_forward(rows)["picks"], 0)

    def test_a_thin_cell_is_not_followed_even_at_a_perfect_record(self):
        rows = cell(2022, "8+", "unchanged", 12, 0) + cell(2023, "8+", "unchanged", 5, 5)    # 12 games < 30
        self.assertEqual(tra.walk_forward(rows)["picks"], 0)

    def test_the_first_season_is_never_a_test_season(self):
        rows = cell(2022, "8+", "unchanged", 40, 0)
        self.assertEqual(tra.walk_forward(rows)["by_season"], {})


class StoredConstantsTests(unittest.TestCase):
    def test_the_walk_forward_summary_is_internally_consistent(self):
        wf = mr.TOTALS_WALK_FORWARD
        self.assertEqual(wf["wins"] + wf["losses"], wf["picks"])
        self.assertAlmostEqual(wf["win_rate"], 100 * wf["wins"] / wf["picks"], delta=0.06)
        self.assertLess(wf["win_rate"], wf["break_even"])          # the page must not present regimes as an edge

    def test_the_regime_table_only_has_the_tabulated_buckets_and_five_movement_states(self):
        buckets = {key[0] for key in mr.TOTAL_REGIME_BENCHMARKS}
        self.assertEqual(buckets, set(tra.TABLE_BUCKETS))
        self.assertEqual(len(mr.TOTAL_REGIME_BENCHMARKS), 15)

    def test_the_stored_records_match_their_win_rates(self):
        for research in (mr.SPREAD_RESEARCH["full_convergence"], mr.SPREAD_RESEARCH["full_convergence_lt14"],
                         mr.TOTAL_RESEARCH["overall"]):
            wins, losses, pushes = (int(part) for part in research["record"].split("-"))
            self.assertEqual(wins + losses + pushes, research["n"])
            self.assertAlmostEqual(research["win_rate"], 100 * wins / (wins + losses), delta=0.01)


if __name__ == "__main__":
    unittest.main()
