import re
import unittest

from app import create_app
from sports_aggregator.cfb import views
from sports_aggregator.cfb.views import BOX_SCORE_ROWS_SHOWN


def defensive_rows(team, count):
    """One box-score row per player, each credited with a tackle."""
    return [{"team": team, "category": "defensive", "stat_type": "TOT",
             "player_id": f"{team}-{index}", "player": f"Player {index}",
             "numeric_value": count - index, "stat_value": str(count - index)}
            for index in range(count)]


class SiteNavigationTests(unittest.TestCase):
    """A reader must be able to leave the page they landed on."""

    def setUp(self):
        self.client = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False}).test_client()

    def nav(self, path):
        body = self.client.get(path).get_data(as_text=True)
        found = re.search(r'<nav class="site-nav".*?</nav>', body, re.S)
        return found.group(0) if found else ""

    def test_every_page_offers_the_whole_site(self):
        for path in ("/college-football/", "/college-football/scoreboard/",
                     "/college-football/picks/", "/college-football/elo/", "/college-football/draft/",
                     "/college-football/search/"):
            nav = self.nav(path)
            for destination in ("/college-football/", "/college-football/scoreboard/",
                                "/college-football/picks/", "/college-football/elo/",
                                "/college-football/draft/"):
                self.assertIn(f'href="{destination}"', nav, f"{path} cannot reach {destination}")

    def test_the_current_page_is_marked_once(self):
        nav = self.nav("/college-football/scoreboard/")
        self.assertEqual(nav.count('aria-current="page"'), 1)
        current = re.search(r'aria-current="page">([^<]+)<', nav)
        self.assertEqual(current.group(1), "Scoreboard")

    def test_a_page_outside_the_nav_marks_nothing(self):
        self.assertNotIn('aria-current', self.nav("/college-football/search/"))


class BoxScorePanelTests(unittest.TestCase):
    """Panels start collapsed so the page does not resize once the script runs."""

    def setUp(self):
        self.app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})

    def render(self, groups):
        template = self.app.jinja_env.from_string(
            '{% from "_tables.html" import tabbed_tables %}{{ tabbed_tables(groups, "g") }}')
        with self.app.test_request_context():
            return template.render(groups=groups)

    def groups(self, count):
        return views.player_box_score_groups(
            [row for index in range(count)
             for row in defensive_rows(f"Team{index}", 2)])["Team0"]

    def test_only_the_first_panel_is_open(self):
        rows = []
        for category in ("passing", "rushing", "receiving"):
            rows.append({"team": "Michigan", "category": category, "stat_type": "YDS",
                         "player_id": f"p-{category}", "player": "Alex",
                         "numeric_value": 1, "stat_value": "1"})
        html = self.render(views.player_box_score_groups(rows)["Michigan"])
        panels = re.findall(r'<div class="tabpanel"[^>]*>', html)
        self.assertEqual(len(panels), 3)
        self.assertNotIn("hidden", panels[0])
        self.assertIn("hidden", panels[1])
        self.assertIn("hidden", panels[2])

    def test_a_reader_without_javascript_still_sees_every_panel(self):
        body = self.app.test_client().get("/college-football/").get_data(as_text=True)
        self.assertIn("<noscript>", body)
        noscript = re.search(r"<noscript>(.*?)</noscript>", body, re.S).group(1)
        self.assertIn(".tabpanel[hidden]{display:block}", noscript)
        self.assertIn(".tabs{display:none}", noscript)


class LongCategoryTests(unittest.TestCase):
    """A defensive category lists everyone with a tackle; most did not do much."""

    def group(self, count):
        return views.player_box_score_groups(defensive_rows("Michigan", count))["Michigan"][0]

    def test_a_long_category_shows_the_leaders_and_keeps_the_rest(self):
        group = self.group(41)
        self.assertEqual(len(group["table"].rows), BOX_SCORE_ROWS_SHOWN)
        self.assertEqual(len(group["overflow"].rows), 41 - BOX_SCORE_ROWS_SHOWN)
        self.assertIn("26", group["overflow_label"])

    def test_nobody_is_dropped(self):
        group = self.group(41)
        shown = [row["player"] for row in group["table"].rows]
        rest = [row["player"] for row in group["overflow"].rows]
        self.assertEqual(len(set(shown + rest)), 41)

    def test_the_leaders_are_the_ones_shown(self):
        group = self.group(41)
        self.assertEqual(group["table"].rows[0]["TOT"], 41)
        self.assertGreater(min(row["TOT"] for row in group["table"].rows),
                           max(row["TOT"] for row in group["overflow"].rows))

    def test_a_short_category_is_left_whole(self):
        group = self.group(9)
        self.assertEqual(len(group["table"].rows), 9)
        self.assertNotIn("overflow", group)

    def test_a_category_barely_over_the_line_is_not_split(self):
        """Hiding two rows to save two rows of height is just another click."""
        group = self.group(BOX_SCORE_ROWS_SHOWN + 2)
        self.assertNotIn("overflow", group)

    def test_both_halves_carry_the_same_columns(self):
        group = self.group(41)
        self.assertEqual([column.key for column in group["table"].columns],
                         [column.key for column in group["overflow"].columns])


if __name__ == "__main__":
    unittest.main()
