import unittest

from jinja2 import Environment, FileSystemLoader

from sports_aggregator.nfl.season_tables import player_season_table
from sports_aggregator.nfl.team_panels import share_rows
from sports_aggregator.nfl.week_chart import epa_week_chart
from sports_aggregator.tables import Column, Table, format_value


def _weekly(season, game, **stats):
    base = {"season": season, "season_type": "REG", "game_id": game, "team": "BLT"}
    base.update(stats)
    return base


class BandedTableTests(unittest.TestCase):
    def test_adjacent_columns_share_one_band(self):
        table = Table([Column("a", "A", group="Record"), Column("b", "B", group="Record"),
                       Column("c", "C", group="Efficiency"), Column("d", "D", group="Record")], [{}])
        self.assertEqual(table.bands, [("Record", 2), ("Efficiency", 1), ("Record", 1)])

    def test_ungrouped_tables_have_no_band_row(self):
        self.assertEqual(Table([Column("a", "A")], [{}]).bands, [])

    def test_macro_renders_band_row_and_selected_class(self):
        env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
        env.filters["cell"] = format_value
        for name in ("role", "height", "logo_pair"):
            env.filters[name] = lambda value, *args: value
        env.globals["url_for"] = lambda *a, **k: "/x"
        table = Table([Column("s", "S", group="Record"), Column("v", "V", "f1", group="Record")],
                      [{"s": "2025", "v": 1.0, "_row_class": "is-selected"}])
        html = env.from_string("{% from '_tables.html' import data_table %}{{ data_table(t) }}").render(t=table)
        self.assertIn('colspan="2"', html)
        self.assertIn("band-row", html)
        self.assertIn('class="is-selected"', html)


class PlayerSeasonTableTests(unittest.TestCase):
    def rows(self):
        return [
            _weekly(2024, "g1", attempts=30, completions=20, passing_yards=250, passing_tds=2,
                    passing_interceptions=1, sacks_suffered=2, passing_epa=5.0, carries=5, rushing_yards=40,
                    fantasy_points_ppr=20.0),
            _weekly(2024, "g2", attempts=20, completions=10, passing_yards=100, carries=3, rushing_yards=10,
                    fantasy_points_ppr=8.0),
            _weekly(2025, "g3", attempts=10, completions=5, passing_yards=60, carries=2, rushing_yards=9,
                    fantasy_points_ppr=6.0),
            {**_weekly(2025, "p1", attempts=99, completions=99, passing_yards=999), "season_type": "POST"},
        ]

    def test_one_row_per_season_newest_first_with_bands_and_career_total(self):
        table = player_season_table(self.rows(), selected=2025)
        self.assertEqual([row["season"] for row in table.rows], ["2025", "2024"])
        self.assertEqual(table.rows[1]["games"], 2)
        self.assertEqual(table.rows[1]["pass_yds"], 350)
        self.assertAlmostEqual(table.rows[1]["cmp_pct"], 0.6)
        self.assertEqual(table.total_row["pass_yds"], 410)          # postseason excluded
        self.assertEqual(table.total_row["games"], 3)
        labels = [label for label, _ in table.bands]
        self.assertIn("Passing", labels)
        self.assertIn("Rushing", labels)
        self.assertNotIn("Receiving", labels)                       # no receiving volume, so no band

    def test_selected_season_is_flagged(self):
        table = player_season_table(self.rows(), selected=2024)
        flagged = [row["season"] for row in table.rows if row.get("_row_class") == "is-selected"]
        self.assertEqual(flagged, ["2024"])

    def test_no_rows_is_an_empty_table(self):
        self.assertFalse(player_season_table([], selected=2025))


class WeekChartTests(unittest.TestCase):
    def rows(self):
        return [
            {"week": 1, "game_id": "2025_01_A_B", "opponent": "B", "epa_per_play": 0.10,
             "defensive_epa_allowed": -0.05, "point_margin": 7},
            {"week": 3, "game_id": "2025_03_A_C", "opponent": "C", "epa_per_play": -0.20,
             "defensive_epa_allowed": 0.15, "point_margin": -10},
        ]

    def test_geometry_places_weeks_by_number_and_scales_margin_bars(self):
        chart = epa_week_chart(self.rows())
        self.assertTrue(chart["has_data"])
        xs = [point["x"] for point in chart["offense_points"]]
        self.assertLess(xs[0], xs[1])
        bars = {bar["week"]: bar for bar in chart["bars"]}
        self.assertTrue(bars[1]["positive"])
        self.assertFalse(bars[3]["positive"])
        self.assertGreater(bars[3]["h"], bars[1]["h"])              # bigger margin, taller bar
        self.assertEqual(chart["ticks"][2]["label"], "0.00")
        # higher EPA is higher on the chart (smaller y)
        self.assertLess(chart["offense_points"][0]["y"], chart["offense_points"][1]["y"])

    def test_missing_efficiency_means_no_chart(self):
        self.assertFalse(epa_week_chart([{"week": 1, "epa_per_play": None,
                                          "defensive_epa_allowed": None}])["has_data"])


class ShareRowsTests(unittest.TestCase):
    def test_bar_is_relative_to_the_leader(self):
        rows = share_rows([{"player_name": "A", "share": 0.30}, {"player_name": "B", "share": 0.15}],
                          "share", lambda p: "x")
        self.assertEqual(rows[0]["bar"], 1.0)
        self.assertEqual(rows[1]["bar"], 0.5)
        self.assertEqual(rows[0]["value"], "30.0%")


if __name__ == "__main__":
    unittest.main()
