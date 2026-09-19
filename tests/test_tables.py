import os
import tempfile
import unittest

from app import create_app
from sports_aggregator.cfb import views
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.statlines import leader_table, player_stat_tables
from sports_aggregator.tables import Column, Table, format_value


def stat(season, category, stat_type, value, player="Alex Example",
         team="Michigan", position="QB"):
    return {"season": season, "player_id": "p1", "player": player, "team": team,
            "conference": "Big Ten", "position": position, "category": category,
            "stat_type": stat_type, "stat_value": str(value), "numeric_value": value}


class FormatTests(unittest.TestCase):
    def test_missing_values_render_as_an_em_dash_not_a_zero(self):
        for value in (None, ""):
            self.assertEqual(format_value(value, "int"), "—")
            self.assertEqual(format_value(value, "f1"), "—")

    def test_each_format_uses_its_own_scale(self):
        self.assertEqual(format_value(0.686, "rate"), "68.6%")
        self.assertEqual(format_value(68.6, "pct"), "68.6%")
        self.assertEqual(format_value(3120, "int"), "3120")
        self.assertEqual(format_value(83122, "big"), "83,122")
        self.assertEqual(format_value(0.4521, "f3"), "0.452")
        self.assertEqual(format_value(9.0, "num"), "9")

    def test_non_numeric_values_survive_a_numeric_format(self):
        self.assertEqual(format_value("TBD", "f1"), "TBD")

    def test_numeric_columns_default_to_right_alignment(self):
        self.assertEqual(Column("yards", "YDS", format="int").align, "right")
        self.assertEqual(Column("team", "Team").align, "left")

    def test_table_is_falsey_when_empty_so_templates_can_branch(self):
        self.assertFalse(Table(columns=[Column("a", "A")]))
        self.assertTrue(Table(columns=[Column("a", "A")], rows=[{"a": 1}]))

    def test_compact_tables_remain_mobile_data_tables(self):
        app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})
        template = app.jinja_env.from_string(
            '{% from "_tables.html" import data_table %}{{ data_table(table) }}')
        with app.test_request_context():
            html = template.render(table=Table(
                columns=[Column("team", "Team"), Column("record", "Record")],
                rows=[{"team": "Michigan", "record": "10-2"}],
            ))
        self.assertIn("mobile-compact", html)
        # A real table with real headers, rather than cards built from per-cell
        # label attributes: the headers are what carry the column names.
        self.assertIn("<thead>", html)
        self.assertIn(">Team</th>", html)
        self.assertIn(">Record</th>", html)
        self.assertIn("Michigan", html)

    def test_wide_stat_tables_keep_a_local_swipe_region(self):
        app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})
        template = app.jinja_env.from_string(
            '{% from "_tables.html" import data_table %}{{ data_table(table) }}')
        columns = [Column(f"stat_{index}", f"S{index}") for index in range(10)]
        with app.test_request_context():
            html = template.render(table=Table(
                columns=columns,
                rows=[{column.key: index for index, column in enumerate(columns)}],
            ))
        self.assertIn("mobile-scroll", html)
        self.assertIn("Swipe to see all columns", html)

    def test_table_tabs_cancel_navigation_and_restore_the_selection(self):
        app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})
        template = app.jinja_env.from_string(
            '{% from "_tables.html" import tabs_script %}{{ tabs_script() }}')
        with app.test_request_context("/college-football/teams/1/"):
            html = template.render()
        self.assertIn("event.preventDefault()", html)
        self.assertIn("window.sessionStorage.getItem(storageKey)", html)
        self.assertIn("window.sessionStorage.setItem(storageKey", html)

    def test_comparison_edge_cells_explain_the_visual_marker(self):
        app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})
        template = app.jinja_env.from_string(
            '{% from "_tables.html" import data_table %}{{ data_table(table) }}')
        with app.test_request_context():
            html = template.render(table=Table(
                columns=[Column("value", "Value", format="f1")],
                rows=[{"value": 42.0, "value_class": "advantage"}],
            ))
        self.assertIn("advantage", html)
        self.assertIn('title="Comparison edge"', html)
        # `value` is not a column the stylesheet targets, so no key class.
        self.assertNotIn("col-key-value", html)

    def test_comparison_scale_is_compact_and_accessibly_labeled(self):
        app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})
        template = app.jinja_env.from_string(
            '{% from "_tables.html" import data_table %}{{ data_table(table) }}')
        row = {
            "edge": "Michigan", "edge_scale_position": "72.0",
            "edge_scale_left": "50.0", "edge_scale_width": "22.0",
            "edge_scale_side": "home",
            "edge_scale_label": "Wisconsin left, Michigan right; Michigan holds the edge",
        }
        with app.test_request_context():
            html = template.render(table=Table(
                columns=[Column("edge", "Edge")], rows=[row]))
        self.assertIn('class="comparison-scale scale-home"', html)
        self.assertIn('--edge-position:72.0%', html)
        self.assertIn('aria-label="Wisconsin left, Michigan right; Michigan holds the edge"', html)


class StatLineTests(unittest.TestCase):
    def test_long_form_rows_collapse_into_one_row_per_season(self):
        rows = [
            stat(2025, "passing", "YDS", 3120),
            stat(2025, "passing", "ATT", 400),
            stat(2025, "passing", "COMPLETIONS", 270),
            stat(2025, "passing", "TD", 27),
            stat(2024, "passing", "YDS", 2100),
            stat(2024, "passing", "ATT", 300),
        ]
        tables = player_stat_tables(rows)
        self.assertEqual([group["category"] for group in tables], ["passing"])
        table = tables[0]["table"]
        self.assertEqual(len(table.rows), 2)
        self.assertEqual(table.rows[0]["season"], 2025)
        self.assertEqual(table.rows[0]["YDS"], 3120)
        self.assertEqual(table.rows[0]["COMPLETIONS"], 270)

    def test_columns_follow_box_score_order_not_alphabetical_order(self):
        tables = player_stat_tables([stat(2025, "passing", "YDS", 3120)])
        labels = [column.label for column in tables[0]["table"].columns]
        self.assertEqual(labels, ["Season", "Team", "CMP", "ATT", "PCT", "YDS", "YPA", "TD", "INT"])

    def test_categories_are_ordered_by_reading_convention(self):
        rows = [
            stat(2025, "kicking", "PTS", 90),
            stat(2025, "passing", "YDS", 3120),
            stat(2025, "receiving", "YDS", 400),
        ]
        self.assertEqual(
            [group["category"] for group in player_stat_tables(rows)],
            ["passing", "receiving", "kicking"],
        )

    def test_unknown_category_still_renders_rather_than_disappearing(self):
        tables = player_stat_tables([stat(2025, "brandNewCategory", "XYZ", 5)])
        self.assertEqual(len(tables), 1)
        self.assertIn("XYZ", [column.label for column in tables[0]["table"].columns])

    def test_leaderboard_carries_the_full_stat_line(self):
        table = leader_table("rushing", [
            {"player": "Runner", "player_id": "p2", "position": "RB", "team": "Wisconsin",
             "stats": {"YDS": 1400, "CAR": 240, "TD": 12}},
        ])
        self.assertEqual(table.rows[0]["rank"], 1)
        self.assertEqual(table.rows[0]["CAR"], 240)
        self.assertIn("CAR", [column.key for column in table.columns])


class ViewTableTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
        })

    def tearDown(self):
        os.unlink(self.path)

    def test_height_inches_render_as_feet_and_inches(self):
        self.assertEqual(views.height_label(74), "6-2")
        self.assertEqual(views.height_label(72), "6-0")
        self.assertIsNone(views.height_label(None))
        self.assertIsNone(views.height_label(0))

    def test_current_impact_is_annotated_inline_on_existing_player_rows(self):
        impact = views.impact_player_index([{
            "player_id": "p1", "player": "Alex Example", "roles": ["Ground engine"],
            "impact_level": "notable", "impact_label": "Notable impact",
        }])
        with self.app.test_request_context():
            returning = views.pff_players_table([{
                "cfbd_player_id": "p1", "player_name": "Alex Example",
                "position": "RB", "cfbd_team": "Michigan",
                "roster_status": "RETURNING", "interest_score": 81.2,
            }], 2026, impact=impact)
            arrival = views.arrivals_table([{
                # Name fallback covers feeds whose player IDs do not align.
                "player_id": "different-id", "name": "Alex Example", "position": "RB",
                "movement_type": "TRANSFER_IN", "origin": "Wisconsin",
            }], 2026, impact=impact)

        self.assertEqual(returning.rows[0]["player_name_class"], "impact-notable")
        self.assertEqual(returning.rows[0]["player_name_sub"],
                         "Notable impact · Ground engine")
        self.assertEqual(arrival.rows[0]["name_class"],
                         "state-arrived impact-notable")
        self.assertEqual(arrival.rows[0]["name_sub"],
                         "Notable impact · Ground engine")

    def test_schedule_marks_wins_and_losses_from_the_team_perspective(self):
        schedule = [
            {"game_id": 1, "week": 1, "home_team_id": 5, "away_team_id": 9,
             "home_team": "Michigan", "away_team": "Wisconsin", "home_points": 30,
             "away_points": 17, "completed": True, "television": "FOX",
             "date_label": "Sat, Sep 05", "time_label": "12:00 PM"},
            {"game_id": 2, "week": 2, "home_team_id": 9, "away_team_id": 5,
             "home_team": "Wisconsin", "away_team": "Michigan", "home_points": 21,
             "away_points": 14, "completed": True, "television": None,
             "date_label": "Sat, Sep 12", "time_label": "3:30 PM"},
            {"game_id": 3, "week": 3, "home_team_id": 5, "away_team_id": 9,
             "home_team": "Michigan", "away_team": "Wisconsin", "home_points": None,
             "away_points": None, "completed": False, "television": None,
             "date_label": "Sat, Sep 19", "time_label": "TBD"},
        ]
        with self.app.test_request_context():
            table = views.schedule_table(schedule, 5, 2026)
        self.assertEqual(table.rows[0]["result"], "W 30-17")
        self.assertEqual(table.rows[0]["result_class"], "win")
        self.assertEqual(table.rows[0]["site"], "vs")
        self.assertEqual(table.rows[0]["detail"], "Box score")
        self.assertTrue(table.rows[0]["detail_url"].endswith("/box-score/"))
        self.assertEqual(table.rows[1]["result"], "L 14-21")
        self.assertEqual(table.rows[1]["site"], "at")
        self.assertEqual(table.rows[2]["result_class"], "pending")
        self.assertEqual(table.rows[2]["detail"], "Preview")
        self.assertFalse(table.rows[2]["detail_url"].endswith("/box-score/"))

    def test_standings_render_records_as_records_not_separate_columns(self):
        standings = [{
            "team_id": 5, "school": "Michigan", "rank": 3, "games": 12,
            "wins": 10, "losses": 2, "ties": 0, "conference_wins": 7,
            "conference_losses": 2, "conference_ties": 0, "expected_wins": 9.4,
        }]
        with self.app.test_request_context():
            table = views.standings_table(standings, 2026)
        self.assertEqual(table.rows[0]["overall_record"], "10-2")
        self.assertEqual(table.rows[0]["conference_record"], "7-2")

    def test_advanced_metrics_keep_a_per_row_scale(self):
        game = {
            "season": 2026, "home_team": "Michigan", "away_team": "Wisconsin",
            "advanced_metrics": {
                "Michigan": {"offense_success_rate": .452, "offense_ppa": .231},
                "Wisconsin": {"offense_success_rate": .401, "offense_ppa": .118},
            },
        }
        table = views.matchup_metrics_table(game, {
            "Michigan": {
                "offense_success_rate": {"rank": 12, "of": 134, "percentile": 92},
                "offense_ppa": {"rank": 8, "of": 134, "percentile": 95},
            },
            "Wisconsin": {
                "offense_success_rate": {"rank": 55, "of": 134, "percentile": 59},
            },
        })
        by_metric = {row["metric"]: row for row in table.rows}
        self.assertEqual(by_metric["Success rate"]["home_offense"], "45.2%")
        self.assertEqual(by_metric["Success rate"]["home_offense_sub"],
                         "#12 of 134 · P92")
        self.assertEqual(by_metric["PPA per play"]["home_offense"], "0.231")
        self.assertEqual(by_metric["Havoc"]["home_offense"], "—")

    def test_camel_case_team_stat_names_become_readable_headers(self):
        metrics = {"stats": [{"stat_name": "thirdDownConversions", "stat_value": 71}]}
        table = views.team_stats_table(metrics, 2026)
        self.assertEqual(table.rows[0]["stat_name"], "Third Down Conversions")

    def test_team_summary_switches_between_totals_and_per_game(self):
        metrics = {
            "score": {"games": 2, "points_for": 70, "points_against": 40},
            "stats": [
                {"stat_name": "games", "stat_value": 2},
                {"stat_name": "totalYards", "stat_value": 800},
                {"stat_name": "totalYardsOpponent", "stat_value": 600},
            ],
        }
        total = views.team_summary_table(metrics, 2026, "total")
        average = views.team_summary_table(metrics, 2026, "per_game")
        self.assertEqual(total.rows[0]["offense"], "70")
        self.assertEqual(average.rows[0]["offense"], "35.0")
        self.assertEqual(average.rows[1]["defense"], "300.0")

    def test_matchup_summary_highlights_better_offense_and_defense(self):
        game = {"away_team": "Wisconsin", "home_team": "Michigan"}
        away = {"score": {"games": 2, "points_for": 50, "points_against": 30}}
        home = {"score": {"games": 2, "points_for": 60, "points_against": 40}}
        table = views.matchup_summary_table(game, away, home, 2026, "per_game")
        points = table.rows[0]
        self.assertEqual(points["home_offense_class"], "advantage")
        self.assertEqual(points["away_defense_class"], "advantage")
        self.assertEqual(points["edge"], "Even")
        self.assertIn("% better", points["home_offense_sub"])

    def test_box_score_groups_follow_offense_defense_special_teams_order(self):
        rows = []
        for category, stat_type in (("kicking", "PTS"), ("defensive", "TOT"),
                                    ("receiving", "REC"), ("passing", "YDS")):
            rows.append({"team": "Michigan", "category": category,
                         "stat_type": stat_type, "player_id": "p1",
                         "player": "Alex Example", "numeric_value": 1,
                         "stat_value": "1"})
        groups = views.player_box_score_groups(rows)["Michigan"]
        self.assertEqual([group["label"] for group in groups],
                         ["Passing", "Receiving", "Defense", "Kicking"])


def team_box_rows(away, home, away_team="Howard", home_team="Maryland",
                  away_points=0, home_points=79):
    """Rows as `game_box_score` supplies them: away first, one per category."""
    rows = []
    for team, points, stats in ((away_team, away_points, away),
                                (home_team, home_points, home)):
        for category, value in stats.items():
            rows.append({"team": team, "points": points, "category": category,
                         "stat_value": str(value),
                         "numeric_value": value if isinstance(value, (int, float)) else None})
    return rows


class TeamBoxScoreTests(unittest.TestCase):
    """The team box score is a comparison, so it reads down rather than across."""

    def table(self, away, home, **kwargs):
        return views.team_box_score_table(team_box_rows(away, home, **kwargs))

    def rows_by_stat(self, table):
        return {row["stat"]: row for row in table.rows}

    def test_it_puts_the_teams_in_columns_and_the_statistics_in_rows(self):
        table = self.table({"totalYards": 68}, {"totalYards": 623})
        self.assertEqual([column.label for column in table.columns],
                         ["Statistic", "Howard", "Maryland"])
        self.assertIn("Total yards", self.rows_by_stat(table))

    def test_the_away_team_keeps_the_first_column(self):
        """The cover, the scoreboard and the URL all read away-then-home."""
        table = self.table({"totalYards": 68}, {"totalYards": 623})
        self.assertEqual(table.columns[1].label, "Howard")

    def test_it_offers_no_sort_because_the_rows_share_no_scale(self):
        table = self.table({"totalYards": 68}, {"totalYards": 623})
        self.assertFalse(table.sortable)

    def test_a_group_heading_precedes_the_statistics_it_labels(self):
        table = self.table({"totalYards": 68, "sacks": 0},
                           {"totalYards": 623, "sacks": 3})
        stats = [row["stat"] for row in table.rows]
        self.assertLess(stats.index("Offense"), stats.index("Total yards"))
        self.assertLess(stats.index("Defense"), stats.index("Sacks"))
        heading = self.rows_by_stat(table)["Offense"]
        self.assertEqual(heading["stat_class"], "box-group")

    def test_a_heading_leaves_no_em_dash_under_it(self):
        """An empty value renders as "no data"; a heading has no data to miss."""
        table = self.table({"totalYards": 68}, {"totalYards": 623})
        heading = self.rows_by_stat(table)["Offense"]
        self.assertNotIn("—", [heading[column.key] for column in table.columns[1:]])

    def test_a_group_with_nothing_reported_does_not_appear(self):
        table = self.table({"totalYards": 68}, {"totalYards": 623})
        self.assertNotIn("Special teams", [row["stat"] for row in table.rows])

    def test_the_better_value_is_marked_on_one_side_only(self):
        table = self.table({"totalYards": 68}, {"totalYards": 623})
        yards = self.rows_by_stat(table)["Total yards"]
        away, home = table.columns[1].key, table.columns[2].key
        self.assertEqual(yards[f"{home}_class"], "advantage")
        self.assertNotIn(f"{away}_class", yards)

    def test_fewer_is_better_where_fewer_is_better(self):
        table = self.table({"turnovers": 3}, {"turnovers": 0})
        turnovers = self.rows_by_stat(table)["Turnovers"]
        self.assertEqual(turnovers[f"{table.columns[2].key}_class"], "advantage")

    def test_a_tie_gives_neither_side_an_edge(self):
        table = self.table({"totalYards": 300}, {"totalYards": 300})
        yards = self.rows_by_stat(table)["Total yards"]
        self.assertEqual([key for key in yards if key.endswith("_class")], [])

    def test_volume_defence_is_not_scored_as_an_edge(self):
        """Tackles and return yards climb because the other team kept scoring.

        Marking them would hand the losing side an "edge" in a 79-0 game.
        """
        table = self.table({"tackles": 45, "kickReturnYards": 145, "puntReturnYards": 60},
                           {"tackles": 38, "kickReturnYards": 44, "puntReturnYards": 12})
        for label in ("Tackles", "Kick return yards", "Punt return yards"):
            row = self.rows_by_stat(table)[label]
            self.assertEqual([key for key in row if key.endswith("_class")], [],
                             f"{label} should carry no edge")

    def test_a_statistic_that_is_not_a_contest_carries_no_edge(self):
        table = self.table({"possessionTime": "28:04", "thirdDownEff": "2-17"},
                           {"possessionTime": "31:56", "thirdDownEff": "6-13"})
        for label in ("Time of possession", "Third down"):
            row = self.rows_by_stat(table)[label]
            self.assertEqual([key for key in row if key.endswith("_class")], [])

    def test_a_value_only_one_team_reported_is_shown_without_an_edge(self):
        table = self.table({"totalYards": 68},
                           {"totalYards": 623, "kickingPoints": 11})
        points = self.rows_by_stat(table)["Kicking points"]
        self.assertEqual(points[table.columns[1].key], "—")
        self.assertEqual(points[table.columns[2].key], "11")
        self.assertEqual([key for key in points if key.endswith("_class")], [])

    def test_no_stored_box_score_leaves_an_empty_table_rather_than_a_broken_one(self):
        table = views.team_box_score_table([])
        self.assertEqual(table.rows, [])
        self.assertFalse(table)


class RenderedTableTests(unittest.TestCase):
    """The pages must emit real tables, not divs styled to look like them."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        self.repository.replace_player_stats(2025, [
            {"season": 2025, "playerId": "p1", "player": "Alex Example",
             "team": "Michigan", "conference": "Big Ten", "position": "QB",
             "category": "passing", "statType": key, "stat": value}
            for key, value in (("YDS", "3120"), ("ATT", "400"),
                               ("COMPLETIONS", "270"), ("TD", "27"), ("PCT", "0.675"))
        ], "Big Ten")
        from sports_aggregator.cfb.models import Player
        self.repository.replace_players(2026, (
            Player("p1", 2026, "Alex", "Example", "Michigan", "QB", 7, 74, 215, 3),
        ))
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
        })

    def tearDown(self):
        os.unlink(self.path)

    def test_player_page_renders_a_pivoted_stat_line(self):
        body = self.app.test_client().get("/college-football/players/p1/").get_data(as_text=True)
        self.assertIn('<table class="data', body)
        for header in (">CMP<", ">ATT<", ">PCT<", ">YDS<", ">TD<"):
            self.assertIn(header, body)
        # Values belong to one row, not five key/value rows.
        self.assertIn("67.5%", body)
        self.assertNotIn("<th>Statistic</th>", body)
        self.assertIn('data-sortable="true"', body)
        self.assertIn("cfb_tables.js", body)
