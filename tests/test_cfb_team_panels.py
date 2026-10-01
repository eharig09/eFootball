import tempfile
import unittest
from pathlib import Path

from sports_aggregator.cfb import team_panels
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.ranked_panels import percentile, tone


class RankedPanelHelperTests(unittest.TestCase):
    def test_percentile_is_best_first(self):
        self.assertEqual(percentile(1, 138), 100)
        self.assertEqual(percentile(138, 138), 0)
        self.assertIsNone(percentile(None, 138))
        self.assertEqual(percentile(1, 1), 100)

    def test_tone_bands(self):
        self.assertEqual((tone(90), tone(50), tone(10), tone(None), tone(90, True)), ("good", "mid", "poor", "neutral", "neutral"))


def _pool_row(team, **values):
    return {"team": team, "games": 5, **values}


class _Repository:
    """Just enough repository for the pool-based helpers; the pool is injected."""
    path = None

    def __init__(self, pool_by_season):
        self.pools = pool_by_season


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.pool = [_pool_row(name, epa_per_play=epa, def_epa_per_play=allowed, points_per_game=ppg, points_allowed_per_game=pa)
                     for name, epa, allowed, ppg, pa in (("A", .3, -.2, 40, 14), ("B", .1, 0.0, 30, 21),
                                                         ("C", -.1, .1, 20, 28), ("D", 0.0, .3, 25, 35))]
        original = team_panels.season_pool
        team_panels.season_pool = lambda repository, season: self.pools.get(season, [])
        self.addCleanup(setattr, team_panels, "season_pool", original)
        self.pools = {2026: self.pool, 2025: self.pool}

    def test_lower_is_better_stats_rank_the_smallest_first(self):
        panels = team_panels.team_panels(_Repository(self.pools), 2026, "A")
        by_label = {row["label"]: row for group in panels["defense"]["groups"] for row in group["rows"]}
        self.assertEqual(by_label["EPA / play allowed"]["rank"], 1)          # -0.2 is the best defense
        self.assertEqual(by_label["Pts allowed / game"]["rank"], 1)
        self.assertEqual(by_label["Pts allowed / game"]["pctl"], 100)
        offense = {row["label"]: row for group in panels["offense"]["groups"] for row in group["rows"]}
        self.assertEqual(offense["EPA / play"]["rank"], 1)

    def test_a_barely_started_season_falls_back_to_the_prior_one(self):
        started = [{**row, "games": 1} for row in self.pool]
        self.pools = {2026: started, 2025: self.pool}
        self.assertEqual(team_panels.panel_season(_Repository(self.pools), 2026), (2025, True))
        self.pools = {2026: self.pool, 2025: self.pool}
        self.assertEqual(team_panels.panel_season(_Repository(self.pools), 2026), (2026, False))

    def test_matchup_card_leans_toward_the_better_rank_with_a_five_rank_gap(self):
        wide = [_pool_row(f"T{index}", epa_per_play=.5 - index * .01, def_epa_per_play=-.3 + index * .01) for index in range(20)]
        self.pools = {2026: wide}
        cards = team_panels.matchup_cards(_Repository(self.pools), 2026, "T0", "T19")["cards"]
        first = cards[0]["sections"][0]["rows"][0]            # T0's offense (rank 1) vs T19's defense (rank 20)
        self.assertEqual((first["offense_rank"], first["defense_rank"], first["lean"]), (1, 20, "T0"))
        reverse = cards[1]["sections"][0]["rows"][0]          # T19's offense (rank 20) vs T0's defense (rank 1)
        self.assertEqual(reverse["lean"], "T0")


class RealDatabaseSmokeTests(unittest.TestCase):
    @unittest.skipUnless(Path("instance/cfb.sqlite3").exists(), "needs the local college football database")
    def test_pool_covers_fbs_and_panels_build(self):
        repository = CFBRepository("instance/cfb.sqlite3")
        pool = team_panels.season_pool(repository, 2025)
        self.assertGreater(len(pool), 100)
        some = pool[0]["team"]
        panels = team_panels.team_panels(repository, 2025, some)
        self.assertTrue(panels["offense"]["has_data"] and panels["defense"]["has_data"])


if __name__ == "__main__":
    unittest.main()
