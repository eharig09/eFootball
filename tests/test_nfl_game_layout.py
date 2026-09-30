"""Structure guards for the matchup template: tab assignment, ordering, balanced markup."""

import pathlib
import re
import unittest

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "templates" / "nfl_game.html"


class GameLayoutTests(unittest.TestCase):
    def setUp(self):
        self.html = TEMPLATE.read_text(encoding="utf-8")

    def panel(self, section_id):
        match = re.search(r'id="%s" data-nfl-panel="([a-z-]+)"' % re.escape(section_id), self.html)
        self.assertIsNotNone(match, section_id)
        return match.group(1)

    def test_tables_and_stats_live_on_the_stats_tab(self):
        for section_id in ("production", "position-groups", "shape", "market"):
            self.assertEqual(self.panel(section_id), "stats", section_id)

    def test_charts_and_unit_matchups_live_on_the_matchups_tab(self):
        for section_id in ("player-watches", "matchups", "trenches", "pass-defense",
                           "rush-defense", "high-volume-interactions", "run-interactions"):
            self.assertEqual(self.panel(section_id), "matchups", section_id)

    def test_players_to_watch_come_first_on_the_matchups_tab(self):
        order = re.findall(r'id="([a-z-]+)" data-nfl-panel="matchups"', self.html)
        self.assertEqual(order[0], "player-watches")
        self.assertEqual(order[1], "matchups")

    def test_the_redundant_unit_by_unit_table_is_gone(self):
        self.assertNotIn("expanded-matchup", self.html)
        self.assertNotIn("unit-matchup-card", self.html)

    def test_matchups_shows_two_rank_gap_groupings_in_order(self):
        traditional = self.html.index("('Traditional', 'PFF')")
        efficiency = self.html.index("('Efficiency', 'Next Gen Stats')")
        self.assertLess(traditional, efficiency)

    def test_review_tab_carries_the_play_story(self):
        self.assertEqual(self.panel("plays"), "review")
        for macro in ("wp_chart(", "swing_table(", "drive_chart(", "play_table("):
            self.assertIn(macro, self.html)

    def test_card_stacks_were_replaced_by_tables(self):
        for legacy in ("availability-grid", "situation-grid", "history-panel", "game-shape-grid", "pace-strip",
                       "market-strip", "coach-ats-grid", "leader-team-grid", "matchup-usage-grid",
                       "postgame-edge-grid", "postgame-leader-grid", "game-efficiency-grid", "player-watch-grid",
                       "alignment-card-grid", "run-matchup-grid", "trench-grid", "football-lab-score-grid",
                       "football-lab-delta-strip", "matchup-unit-continuity", "recent-form-grid"):
            self.assertNotIn(legacy, self.html, legacy)

    def test_container_tags_are_balanced(self):
        for tag in ("section", "details", "article", "header", "nav", "table", "tbody"):
            opened = len(re.findall(r"<%s\b" % tag, self.html))
            closed = self.html.count("</%s>" % tag)
            self.assertEqual(opened, closed, tag)


if __name__ == "__main__":
    unittest.main()
