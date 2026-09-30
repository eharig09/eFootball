import unittest

from jinja2 import Environment, FileSystemLoader

from sports_aggregator.tables import format_value


def _env():
    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.filters["cell"] = format_value
    # _nfl_ui.html imports the shared table macros, which reference these app-level filters and globals.
    for name in ("role", "height", "logo_pair"):
        env.filters[name] = lambda value, *args: value
    env.globals["url_for"] = lambda *args, **kwargs: "/x"
    return env


class RankGapTests(unittest.TestCase):
    def _render(self, **row):
        base = {"label": "EPA / play", "format": "signed2", "offense_value": 0.1,
                "defense_value": 0.0, "offense_rank": 1, "offense_of": 32,
                "defense_rank": 17, "defense_of": 32, "lean": "NE", "separation": 16}
        base.update(row)
        card = {"offense": "NE", "defense": "SEA",
                "sections": [{"label": "Efficiency", "source": "x", "rows": [base]}]}
        template = _env().from_string(
            "{% from '_nfl_ui.html' import rank_gap_panel %}"
            "{{ rank_gap_panel(card, {'color': '#123456'}, {'color': '#654321'}) }}")
        return template.render(card=card)

    def test_positions_scale_rank_to_percent_of_track(self):
        html = self._render()
        self.assertIn("--x:0.0", html)     # #1 sits at the left edge
        self.assertIn("--x:51.6", html)    # #17 of 32 is 16/31 along the track
        self.assertIn("--span:51.6", html)
        self.assertIn('data-lean="attack"', html)

    def test_hover_detail_carries_the_actual_numbers(self):
        html = self._render(offense_value=0.123, defense_value=-0.045)
        self.assertIn("ui-gap-tip", html)
        self.assertIn("+0.12", html)
        self.assertIn("-0.04", html)
        self.assertIn("NE advantage", html)

    def test_groups_are_filtered_and_labelled(self):
        card = {"offense": "NE", "defense": "SEA", "sections": [
            {"label": "Traditional", "source": "box", "rows": [
                {"label": "Scoring", "format": "f1", "offense_value": 27.0, "defense_value": 12.0,
                 "offense_rank": 3, "offense_of": 32, "defense_rank": 1, "defense_of": 32,
                 "lean": "SEA", "separation": 2}]}]}
        tpl = _env().from_string(
            "{% from '_nfl_ui.html' import rank_gap_panel %}"
            "{{ rank_gap_panel(card, {}, {}, ('Efficiency',)) }}|"
            "{{ rank_gap_panel(card, {}, {}, ('Traditional',)) }}")
        empty, shown = tpl.render(card=card).split("|")
        self.assertNotIn("ui-gap", empty)
        self.assertIn("Traditional", shown)

    def test_rows_without_both_ranks_are_skipped(self):
        self.assertNotIn("ui-gap-row", self._render(defense_rank=None))


if __name__ == "__main__":
    unittest.main()
