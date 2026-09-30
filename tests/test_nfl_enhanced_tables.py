import json
import unittest

from jinja2 import Environment, FileSystemLoader

from sports_aggregator.nfl import enhanced_tables as et
from sports_aggregator.nfl import route_analytics as ra
from sports_aggregator.tables import format_value
from test_nfl_route_analytics import Base


class LowThresholds:
    """Shrink the coverage and ratio floors so the six-play scenario counts as real data."""

    def setUp(self):
        super().setUp()
        self._saved = (ra.MIN_COVERAGE, ra.MIN_RATIO_SAMPLE)
        ra.MIN_COVERAGE, ra.MIN_RATIO_SAMPLE = 3, 1
        ra.clear_cache()

    def tearDown(self):
        ra.MIN_COVERAGE, ra.MIN_RATIO_SAMPLE = self._saved
        ra.clear_cache()
        super().tearDown()


class RouteTableTests(LowThresholds, Base):
    def test_receiver_table_is_banded_and_totalled(self):
        tree = ra.route_tree(self.repository, 2025, "receiver", "R1")
        table = et.route_table(tree, role="receiver")
        self.assertEqual([label for label, _ in table.bands], ["Route", "Volume", "Results", "Efficiency"])
        self.assertEqual(table.total_row["label"], "All charted routes")
        self.assertEqual(table.total_row["targets"], 4)
        self.assertEqual({row["label"] for row in table.rows}, {"Hitch / Curl", "Go"})
        labels = [column.label for column in table.columns]
        self.assertIn("Catch %", labels)
        self.assertIn("YAC/R", labels)

    def test_passer_table_uses_passing_vocabulary(self):
        tree = ra.route_tree(self.repository, 2025, "passer", "QB1")
        labels = [column.label for column in et.route_table(tree, role="passer").columns]
        self.assertIn("Att", labels)
        self.assertIn("Cmp %", labels)
        self.assertIn("INT", labels)
        self.assertIn("CPOE", labels)
        self.assertNotIn("Catch %", labels)

    def test_depth_mix_shares_sum_to_one(self):
        mix = et.route_depth_mix(ra.route_tree(self.repository, 2025, "receiver", "R1"))
        self.assertAlmostEqual(sum(item["share"] for item in mix), 1.0)
        self.assertEqual({item["key"] for item in mix}, {"short", "deep"})

    def test_empty_tree_yields_an_empty_table(self):
        table = et.route_table({"has_data": False}, role="receiver")
        self.assertFalse(table)
        self.assertEqual(et.route_depth_mix({"has_data": False}), [])


class PairAndPackageTableTests(LowThresholds, Base):
    def test_pair_table_links_partners_and_lists_top_routes(self):
        result = ra.pairs(self.repository, None, 2025, "passer", "QB1")
        table = et.pair_table(result, role="passer", season=2025)
        first = table.rows[0]
        self.assertEqual(first["name_url"], "/nfl/players/R1/?season=2025")
        self.assertTrue(first["routes"].startswith("Hitch / Curl 2"))
        self.assertIn("PFF route", [column.label for column in table.columns])      # grades band only for passers
        receiver_table = et.pair_table(ra.pairs(self.repository, None, 2025, "receiver", "R1"),
                                       role="receiver", season=2025)
        self.assertNotIn("PFF route", [column.label for column in receiver_table.columns])

    def test_package_table_has_zone_heat_and_a_total_row(self):
        usage = ra.player_packages(self.repository, 2025, "R1")
        table = et.package_table(usage)
        self.assertEqual(table.bands[-1][0], "On-field rate by field zone")
        eleven = next(row for row in table.rows if row["label"] == "11P")
        self.assertAlmostEqual(eleven["on_field_rate"], 0.8)
        self.assertEqual(eleven["on_field_rate_class"], "heat-5")
        neutral = ra.ZONES.index("Neutral")
        self.assertAlmostEqual(eleven[f"zone_{neutral}"], 3 / 4)
        self.assertEqual(eleven[f"zone_{neutral}_sub"], "3/4")
        self.assertEqual(table.total_row["label"], "All packages")
        self.assertEqual(table.total_row["snaps"], 5)

    def test_defender_packages_are_not_labelled_as_personnel_groups(self):
        table = et.package_table(ra.player_packages(self.repository, 2025, "D5"))
        self.assertEqual(table.rows[0]["label"], "Nickel")

    def test_team_package_table_names_the_most_used_players(self):
        table = et.team_package_table(ra.team_packages(self.repository, 2025, "DAL"))
        eleven = next(row for row in table.rows if row["label"] == "11P")
        self.assertIn("100%", eleven["who_WR"])
        self.assertEqual(eleven["plays"], 5)


class SideViewTests(LowThresholds, Base):
    def test_view_carries_league_comparison_and_a_full_grid(self):
        league = ra.league_hash_profile(self.repository, 2025)
        view = et.side_view(ra.hash_profile(self.repository, 2025, "passer", "QB1"), league)
        self.assertTrue(view["has_data"])
        passes = view["passes"]
        self.assertEqual(passes["ratio"], "0.33 : 1")
        self.assertIsNotNone(passes["league_ratio"])
        boundary = next(row for row in passes["rows"] if row["bucket"] == "Boundary")
        self.assertIsNotNone(boundary["league_share"])
        self.assertEqual([row["hash"] for row in passes["matrix"]], ["L", "M", "R"])
        self.assertEqual([cell["loc"] for cell in passes["matrix"][0]["cells"]], ["left", "middle", "right"])
        left_left = passes["matrix"][0]["cells"][0]
        self.assertEqual(left_left["n"], 2)

    def test_no_data_view(self):
        self.assertFalse(et.side_view({"has_data": False})["has_data"])


class OrchestrationTests(LowThresholds, Base):
    def test_receiver_page_packet(self):
        packet = et.player_enhanced(self.repository, None, 2025, "R1", "WR")
        self.assertTrue(packet["has_data"])
        self.assertEqual(packet["routes"]["role"], "receiver")
        self.assertIsNone(packet["routes"]["note"])
        self.assertEqual([item["role"] for item in packet["sides"]["items"]], ["receiver"])
        self.assertEqual(packet["packages"]["usage"]["team"], "DAL")

    def test_quarterback_packet_uses_passer_role_and_a_baseline_note(self):
        packet = et.player_enhanced(self.repository, None, 2026, "QB1", "QB")
        self.assertEqual(packet["routes"]["role"], "passer")
        self.assertEqual(packet["routes"]["note"], "2025 baseline (no 2026 data yet)")
        self.assertEqual(packet["pairs"]["role"], "passer")

    def test_lineman_gets_packages_only(self):
        packet = et.player_enhanced(self.repository, None, 2025, "T1", "T")
        self.assertNotIn("routes", packet)
        self.assertIn("packages", packet)

    def test_team_packet_and_json_round_trip(self):
        packet = et.team_enhanced(self.repository, None, 2025, "DAL")
        for key in ("offense_packages", "offense_sides", "offense_routes", "pairs"):
            self.assertIn(key, packet)
        self.assertNotIn("defense_packages", packet)          # DAL never played defense in the scenario
        defense = et.team_enhanced(self.repository, None, 2025, "PHI")
        self.assertIn("defense_packages", defense)
        self.assertIn("defense_routes", defense)
        self.assertIn("defense_sides", defense)
        text = json.dumps(et.to_json(packet))
        self.assertIn("All charted routes", text)
        json.dumps(et.to_json(et.player_enhanced(self.repository, None, 2025, "R1", "WR")))

    def test_without_coverage_nothing_is_reported(self):
        ra.MIN_COVERAGE = 10 ** 6
        ra.clear_cache()
        self.assertFalse(et.player_enhanced(self.repository, None, 2025, "R1", "WR")["has_data"])
        self.assertFalse(et.team_enhanced(self.repository, None, 2025, "DAL")["has_data"])


class MacroRenderTests(LowThresholds, Base):
    def env(self):
        env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
        env.filters["cell"] = format_value
        for name in ("role", "height", "logo_pair"):
            env.filters[name] = lambda value, *args: value
        env.globals["url_for"] = lambda *args, **kwargs: "/x"
        return env

    def test_side_panel_marks_the_league_tick_and_shows_the_grid(self):
        league = ra.league_hash_profile(self.repository, 2025)
        view = et.side_view(ra.hash_profile(self.repository, 2025, "passer", "QB1"), league)
        html = self.env().from_string(
            "{% from '_nfl_ui.html' import side_panel %}{{ side_panel('Throws', v.passes, true, 'n') }}").render(v=view)
        self.assertIn("ui-ratio", html)
        self.assertIn("0.33 : 1", html)
        self.assertIn("<u style=", html)
        self.assertIn("ui-matrix", html)
        self.assertIn("Boundary", html)

    def test_table_panel_renders_bands_mix_and_note_text_unescaped(self):
        tree = ra.route_tree(self.repository, 2025, "receiver", "R1")
        html = self.env().from_string(
            "{% from '_nfl_ui.html' import table_panel %}{{ table_panel('Routes', t, '2025 · vs league', mix, 'why') }}"
        ).render(t=et.route_table(tree, role="receiver"), mix=et.route_depth_mix(tree))
        self.assertIn("band-row", html)
        self.assertIn("ui-mix", html)
        self.assertIn("2025 · vs league", html)
        self.assertIn("Hitch / Curl", html)


if __name__ == "__main__":
    unittest.main()
