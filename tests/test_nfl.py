from datetime import date, datetime, timezone
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sports_aggregator.catalog import get_league
from sports_aggregator.models import Article, FeedConfig
from sports_aggregator.nfl.content import NFLContentRepository
from sports_aggregator.providers.base import ProviderFetchError
from sports_aggregator.nfl.models import Game, Player, Team
from sports_aggregator.nfl.data_sources import free_sources, load_data_sources
from sports_aggregator.nfl.espn import injury_rows, staff_rows
from sports_aggregator.nfl.naming import canon_position, canon_team, normalize_name
from sports_aggregator.nfl.alignments import alignment_matchups, rushing_matchups
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.rss_directory import national_feeds, team_feeds
from sports_aggregator.nfl.source_directory import import_directory, load_directory
from sports_aggregator.nfl.sync import (
    NFLDataSync, NGS_PASSING_RATE_METRICS, NGS_RUSHING_RATE_METRICS, NGS_RUSHING_SUM_METRICS,
)
from sports_aggregator.nfl.teams import unit_continuity
from sports_aggregator.nfl.nflverse import NflverseClient, NflverseError, current_season
from sports_aggregator.nfl.charts import player_charts, team_charts
from sports_aggregator.nfl.explorer import scatter_plot
from sports_aggregator.nfl.passing import pass_matchup_packet, pass_zone_packet
from sports_aggregator.nfl.rushing import run_direction_packet, run_matchup_packet
from sports_aggregator.nfl.personnel import _score
from sports_aggregator.nfl.postgame import postgame_packet
from sports_aggregator.nfl.production_seed import LOCK_NAME, STATE_NAME, maybe_launch
from sports_aggregator.nfl.ranking import rank_lookup, rank_within
from sports_aggregator.nfl.search import search_entities as search_nfl_entities
from sports_aggregator.nfl.views import (
    current_games, ingestion_runs_table, schedule_table, zone_matchup_table,
)
from sports_aggregator.nfl.web import _headline_stats_with_rank
from sports_aggregator.social.models import SourceProfile
from sports_aggregator.social.registry import SourceRegistry


class NFLNamingTests(unittest.TestCase):
    def test_folds_cross_source_identifiers_without_dropping_unknowns(self):
        self.assertEqual(canon_team("GB"), "GNB")
        self.assertEqual(canon_team("JAX"), "JAC")
        self.assertEqual(canon_team("NEW"), "NEW")
        self.assertEqual(canon_position("HB"), "RB")
        self.assertEqual(canon_position("EDGE"), "EDGE")
        self.assertEqual(normalize_name("Odell Beckham Jr."), "odell beckham")
        self.assertEqual(normalize_name("Amon-Ra St. Brown"), "amon ra st brown")

    def test_pass_chart_runs_from_deep_to_behind_line(self):
        packet = pass_zone_packet({"zones": [], "total": {}, "season": 2026})
        self.assertEqual([row["depth"] for row in packet["rows"]],
                         ["deep", "intermediate", "short", "behind"])

    def test_pass_chart_color_represents_result_not_volume(self):
        profile = {"zones": [
            {"depth_bucket": "deep", "pass_location": "left", "attempts": 1,
             "epa_per_attempt": .7},
            {"depth_bucket": "short", "pass_location": "right", "attempts": 20,
             "epa_per_attempt": -.5},
        ], "total": {"attempts": 21}, "season": 2026}
        offense = pass_zone_packet(profile)
        defense = pass_zone_packet(profile, lower_is_better=True)
        self.assertEqual(offense["rows"][0]["cells"][0]["tone"], "good")
        self.assertEqual(offense["rows"][2]["cells"][2]["tone"], "bad")
        self.assertEqual(defense["rows"][0]["cells"][0]["tone"], "bad")
        self.assertEqual(defense["rows"][2]["cells"][2]["tone"], "good")

    def test_pass_matchup_joins_offense_and_defense_into_one_edge(self):
        offense = pass_zone_packet({"zones": [
            {"depth_bucket": "deep", "pass_location": "left", "attempts": 4,
             "epa_per_attempt": .3},
        ], "total": {"attempts": 10}, "season": 2026})
        defense = pass_zone_packet({"zones": [
            {"depth_bucket": "deep", "pass_location": "left", "attempts": 9,
             "epa_per_attempt": -.2},
        ], "total": {"attempts": 36}, "season": 2026}, lower_is_better=True)
        cell = pass_matchup_packet(offense, defense)["rows"][0]["cells"][0]
        # Offense EPA/att (+.3) and defense EPA/att allowed (-.2, stingy) share the
        # same offense-relative sign convention, so a stingy defense should shrink
        # the offense's edge, not add to it: .3 + (-.2) = .1, not .3 - (-.2) = .5.
        self.assertAlmostEqual(cell["edge"], .1)
        self.assertEqual(cell["lean"], "offense")
        self.assertAlmostEqual(cell["interaction_share"], (0.4 * 0.25) ** .5)

    def test_pass_matchup_favors_offense_against_a_leaky_defense(self):
        offense = pass_zone_packet({"zones": [
            {"depth_bucket": "short", "pass_location": "middle", "attempts": 4,
             "epa_per_attempt": -.3},
        ], "total": {"attempts": 10}, "season": 2026})
        defense = pass_zone_packet({"zones": [
            {"depth_bucket": "short", "pass_location": "middle", "attempts": 9,
             "epa_per_attempt": .8},
        ], "total": {"attempts": 36}, "season": 2026}, lower_is_better=True)
        cell = pass_matchup_packet(offense, defense)["rows"][2]["cells"][1]
        # A below-average offense (-.3) facing a defense that has allowed a lot of
        # value in this zone (+.8, leaky) should still lean offense overall, since
        # both terms describe the same offense-relative outcome: -.3 + .8 = .5.
        self.assertAlmostEqual(cell["edge"], .5)
        self.assertEqual(cell["lean"], "offense")

    def test_team_charts_include_defense_allowed_metrics(self):
        rows = [{"week": 1, "season": 2026, "game_id": "g1", "opponent": "SEA",
                 "point_margin": 7, "points": 24, "points_allowed": 17,
                 "epa_per_play": .1, "success_rate": .45, "pass_epa_per_play": .2,
                 "rush_epa_per_play": -.05, "explosive_rate": .1,
                 "defensive_epa_allowed": -.08, "defensive_success_allowed": .38,
                 "defensive_pass_epa_allowed": -.1, "defensive_rush_epa_allowed": .02,
                 "defensive_explosive_allowed": .07}]
        charts = {chart["key"]: chart for chart in team_charts(rows)}
        self.assertIn("defensive_epa_allowed", charts)
        self.assertAlmostEqual(charts["defensive_epa_allowed"]["values"][0]["value"], -.08)
        self.assertIn("defensive_explosive_allowed", charts)

    def test_player_charts_expanded_per_position(self):
        qb_row = {"week": 1, "season": 2026, "game_id": "g1", "opponent_team": "SEA",
                  "passing_yards": 280, "passing_epa": 5.2, "passing_air_yards": 190,
                  "attempts": 34, "completions": 22, "passing_tds": 2,
                  "passing_interceptions": 1, "rushing_yards": 12, "rushing_epa": .3}
        qb_charts = {chart["key"] for chart in player_charts([qb_row], "QB")}
        self.assertIn("passing_tds", qb_charts)
        self.assertIn("passing_interceptions", qb_charts)
        self.assertIn("rushing_yards", qb_charts)

        dl_row = {"week": 1, "season": 2026, "game_id": "g1", "opponent_team": "SEA",
                  "def_tackles_solo": 4, "def_qb_hits": 2, "def_sacks": 1.5,
                  "def_tackles_for_loss": 1, "def_interceptions": 0,
                  "def_pass_defended": 1, "def_fumbles_forced": 1, "def_tackle_assists": 2}
        dl_charts = {chart["key"] for chart in player_charts([dl_row], "EDGE")}
        self.assertIn("def_pass_defended", dl_charts)
        self.assertIn("def_fumbles_forced", dl_charts)

    def test_scatter_plot_emits_chartjs_points_not_pixel_geometry(self):
        rows = [{"player_id": "1", "player_name": "A", "team": "SEA", "position": "QB",
                 "passing_epa": 5.0, "passing_yards": 300}]
        scatter = scatter_plot(rows, "passing_epa", "passing_yards")
        self.assertEqual(scatter["points"][0]["x"], 5.0)
        self.assertEqual(scatter["points"][0]["y"], 300)
        self.assertNotIn("cx", scatter["points"][0])
        self.assertNotIn("x_ticks", scatter)

    def test_current_slate_retains_finals_from_the_active_week(self):
        games = [
            {"game_id": "final", "week": 1, "game_date": "2026-09-13", "completed": 1},
            {"game_id": "next", "week": 1, "game_date": "2026-09-14", "completed": 0},
            {"game_id": "later", "week": 2, "game_date": "2026-09-20", "completed": 0},
        ]
        self.assertEqual(
            [game["game_id"] for game in current_games(games, today=date(2026, 9, 14))],
            ["final", "next"],
        )

    def test_team_schedule_exposes_team_perspective_final_score(self):
        table = schedule_table([{
            "game_id": "final", "week": 1, "game_date": "2026-09-13",
            "game_time": "13:00", "away_team": "CIN", "home_team": "CLE",
            "away_score": 31, "home_score": 20, "completed": 1,
        }], team="CIN")
        self.assertEqual(table.rows[0]["result"], "W")
        self.assertEqual(table.rows[0]["score"], "31–20")
        self.assertIn("score", [column.key for column in table.columns])

    def test_draft_capital_stops_driving_veteran_movement_rank(self):
        rookie = _score({}, None, [], {"draft_year": 2025, "draft_round": 1,
                                       "draft_pick": 10}, 2026)
        veteran = _score({}, None, [], {"draft_year": 2018, "draft_round": 1,
                                        "draft_pick": 10}, 2026)
        self.assertEqual(rookie["draft_weight"], 1.0)
        self.assertEqual(veteran["draft_weight"], 0.0)
        self.assertGreater(rookie["significance_score"], veteran["significance_score"])


class NFLProductionSeedTests(unittest.TestCase):
    def test_empty_database_launches_only_one_seed_process(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Path(directory) / "nfl.sqlite3"
            with patch("sports_aggregator.nfl.production_seed.subprocess.Popen") as popen:
                self.assertTrue(maybe_launch(database_path=database, season=2026))
                self.assertFalse(maybe_launch(database_path=database, season=2026))
            self.assertEqual(popen.call_count, 1)
            self.assertTrue((database.parent / LOCK_NAME).exists())
            state = (database.parent / STATE_NAME).read_text(encoding="utf-8")
            self.assertIn('"status": "launching"', state)

    def test_espn_injuries_keep_designations_and_drop_active_news_rows(self):
        payload = {"timestamp": "2026-09-14T20:56:24Z", "injuries": [{
            "injuries": [
                {"id": "i1", "status": "Questionable", "type": {"abbreviation": "Q"},
                 "athlete": {"id": "10", "displayName": "Player One",
                             "position": {"abbreviation": "WR"},
                             "team": {"abbreviation": "GB"}},
                 "details": {"type": "Hamstring", "location": "Leg",
                             "returnDate": "2026-09-20"},
                 "shortComment": "Limited in practice.", "date": "2026-09-14"},
                {"id": "i2", "status": "Active", "type": {"abbreviation": "A"},
                 "athlete": {"id": "11", "displayName": "Player Two",
                             "team": {"abbreviation": "GB"}}},
            ]}]}
        rows = injury_rows(payload, 2026)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["team"], rows[0]["designation"], rows[0]["injury_type"]),
                         ("GNB", "Q", "Hamstring"))

    def test_staff_seed_and_injury_repository_are_team_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            staff = [row for row in staff_rows() if row["season"] == 2026 and row["team"] == "CIN"]
            self.assertEqual(repository.replace_team_staff(2026, staff), 4)
            self.assertEqual(repository.team_staff(2026, "CIN")[0]["coach_name"], "Zac Taylor")
            repository.replace_injuries(2026, [{
                "team": "CIN", "injury_id": "one", "espn_id": "123",
                "player_name": "Test Player", "position": "WR", "designation": "Q",
                "status": "Questionable", "injury_type": "Hamstring",
                "fetched_at": "2026-09-14T20:56:24Z", "source_url": "https://example.test",
            }])
            self.assertEqual(repository.team_injuries(2026, "CIN")[0]["designation"], "Q")


class NflverseClientTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_nfl_season_rolls_over_in_march(self):
        self.assertEqual(current_season(date(2026, 2, 8)), 2025)
        self.assertEqual(current_season(date(2026, 3, 1)), 2026)

    def test_completed_season_cache_is_immutable_but_live_assets_expire(self):
        client = NflverseClient(
            self.cache, ttl_seconds=100, clock=lambda: 10_000,
            today=date(2026, 9, 1),
        )
        completed = self.cache / "rosters_2025.parquet"
        completed.write_bytes(b"cached")
        os.utime(completed, (1, 1))
        self.assertTrue(client._is_fresh(completed, "rosters", 2025))

        current = self.cache / "rosters_2026.parquet"
        current.write_bytes(b"cached")
        os.utime(current, (1, 1))
        self.assertFalse(client._is_fresh(current, "rosters", 2026))

        schedules = self.cache / "schedules.parquet"
        schedules.write_bytes(b"cached")
        os.utime(schedules, (1, 1))
        self.assertFalse(client._is_fresh(schedules, "schedules", None))

    def test_rejects_unknown_or_unscoped_assets_before_network_access(self):
        client = NflverseClient(self.cache)
        with self.assertRaises(NflverseError):
            client.frame("not-real")
        with self.assertRaisesRegex(NflverseError, "pass one"):
            client.frame("rosters")


class NFLCatalogTests(unittest.TestCase):
    def test_nfl_feed_is_registered_with_verified_espn_endpoint(self):
        league = get_league("nfl")
        self.assertIsNotNone(league)
        self.assertEqual(league.abbreviation, "NFL")
        self.assertEqual(
            [feed.url for feed in league.feeds],
            ["https://www.espn.com/espn/rss/nfl/news"],
        )


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows
        self.columns = tuple(rows[0]) if rows else ()

    def to_dict(self, orientation):
        assert orientation == "records"
        return [dict(row) for row in self.rows]


class FakeNflverseClient:
    def load_teams(self, **_kwargs):
        return FakeFrame([{
            "team_abbr": "GB", "team_name": "Green Bay Packers", "team_nick": "Packers",
            "team_conf": "NFC", "team_division": "NFC North", "team_color": "#203731",
            "team_color2": "#FFB612", "team_logo_espn": "https://example.com/gb.png",
        }])

    def load_schedules(self, _seasons, **_kwargs):
        return FakeFrame([{
            "game_id": "2025_01_GB_CHI", "season": 2025, "game_type": "REG", "week": 1,
            "gameday": "2025-09-07", "gametime": "13:00", "away_team": "GB",
            "home_team": "CHI", "away_score": 24, "home_score": 17, "overtime": 0,
            "div_game": 1, "stadium": "Soldier Field", "spread_line": 1.5, "total_line": 44.5,
        }])

    def load_rosters(self, _seasons, **_kwargs):
        base = {
            "season": 2025, "team": "GB", "position": "QB", "depth_chart_position": "QB",
            "jersey_number": 10, "full_name": "Sample Player Jr.", "first_name": "Sample",
            "last_name": "Player", "gsis_id": "00-001", "years_exp": 2,
        }
        departed = {
            **base, "gsis_id": "00-002", "full_name": "Former Player",
            "first_name": "Former", "week": 3, "status": "CUT",
        }
        traded = {
            **base, "gsis_id": "00-003", "full_name": "Traded Player",
            "first_name": "Traded", "week": 2, "status": "TRD",
        }
        return FakeFrame([
            {**base, "week": 1, "status": "ACT"},
            {**base, "week": 2, "status": "RES"},
            departed,
            traded,
        ])

    def load_weekly(self, _seasons, **_kwargs):
        return FakeFrame([{
            "player_id": "00-001", "player_name": "S.Player", "player_display_name": "Sample Player",
            "position": "QB", "position_group": "QB", "headshot_url": "", "season": 2025,
            "week": 1, "season_type": "REG", "game_id": "2025_01_GB_CHI", "team": "GB",
            "opponent_team": "CHI", "attempts": 30, "passing_yards": 250,
            "targets": 0, "carries": 2,
            "fg_made_list": "40;52",
        }])

    def load_ngs_passing(self, _seasons, **_kwargs):
        return FakeFrame([{
            "season": 2025, "week": 1, "season_type": "REG", "player_gsis_id": "00-001",
            "player_display_name": "Sample Player", "player_position": "QB", "team_abbr": "GB",
            "attempts": 30, "completions": 20, "avg_time_to_throw": 2.7, "aggressiveness": 15.0,
            "avg_intended_air_yards": 7.5, "avg_air_yards_to_sticks": -1.2,
            "completion_percentage_above_expectation": 3.5, "avg_completed_air_yards": 5.1,
        }])

    def load_ngs_rushing(self, _seasons, **_kwargs):
        return FakeFrame([{
            "season": 2025, "week": 1, "season_type": "REG", "player_gsis_id": "00-001",
            "player_display_name": "Sample Player", "player_position": "QB", "team_abbr": "GB",
            "rush_attempts": 2, "efficiency": 4.2, "avg_time_to_los": 2.9,
            "percent_attempts_gte_eight_defenders": 10.0, "rush_yards_over_expected": 1.5,
        }])

    def load_ngs_receiving(self, _seasons, **_kwargs):
        # No matching load_weekly row for this player -- exercises the
        # "no game context to anchor to" skip path in sync.py's _ngs_rows.
        return FakeFrame([{
            "season": 2025, "week": 1, "season_type": "REG", "player_gsis_id": "00-999",
            "player_display_name": "Unrostered Player", "player_position": "WR", "team_abbr": "GB",
            "targets": 5, "receptions": 3, "avg_separation": 2.8, "avg_cushion": 5.5,
            "percent_share_of_intended_air_yards": 20.0, "avg_yac": 4.0, "avg_expected_yac": 3.0,
            "avg_yac_above_expectation": 1.0,
        }])

    def load_snap_counts(self, _seasons, **_kwargs):
        return FakeFrame([{
            "season": 2025, "week": 1, "game_id": "2025_01_GB_CHI",
            "pfr_game_id": "202509070chi", "player": "Sample Player",
            "pfr_player_id": "SampPl01", "position": "QB", "team": "GB",
            "opponent": "CHI", "offense_snaps": 62, "offense_pct": 1.0,
            "defense_snaps": 0, "defense_pct": 0.0, "st_snaps": 0, "st_pct": 0.0,
        }])

    def load_depth_charts(self, _seasons, **_kwargs):
        return FakeFrame([{
            "dt": "2025-09-06T07:00:00Z", "team": "GB", "player_name": "Sample Player",
            "espn_id": "123", "gsis_id": "00-001", "pos_grp_id": "1",
            "pos_grp": "Offense", "pos_id": "1", "pos_name": "Quarterback",
            "pos_abb": "QB", "pos_slot": 1, "pos_rank": 1,
        }])

    def load_player_master(self, **_kwargs):
        return FakeFrame([{
            "gsis_id": "00-001", "display_name": "Sample Player", "first_name": "Sample",
            "last_name": "Player", "pfr_id": "SampPl01", "pff_id": "99",
            "espn_id": "123", "position": "QB", "position_group": "QB",
            "height": 74, "weight": 220, "college_name": "Example State",
            "latest_team": "GB", "status": "ACT", "years_of_experience": 2,
            "draft_year": 2023, "draft_round": 2, "draft_pick": 40, "draft_team": "GB",
        }])

    def load_player_ids(self, **_kwargs):
        return FakeFrame([{
            "gsis_id": "00-001", "name": "Sample Player", "pfr_id": "SampPl01",
            "sleeper_id": 1234,
        }])

    def load_team_weekly(self, _seasons, **_kwargs):
        return FakeFrame([{
            "season": 2025, "week": 1, "season_type": "REG", "team": "GB",
            "opponent_team": "CHI", "passing_yards": 250, "rushing_yards": 110,
        }])

    def load_pbp(self, _seasons, **_kwargs):
        return FakeFrame([
            {"game_id": "2025_01_GB_CHI", "season": 2025, "week": 1,
             "posteam": "GB", "defteam": "CHI", "epa": 0.5, "qb_epa": 0.5,
             "success": 1, "pass": 1, "rush": 0, "down": 1, "yards_gained": 12},
            {"game_id": "2025_01_GB_CHI", "season": 2025, "week": 1,
             "posteam": "CHI", "defteam": "GB", "epa": -0.2, "qb_epa": -0.2,
             "success": 0, "pass": 1, "rush": 0, "down": 2, "yards_gained": 3},
            {"game_id": "2025_01_GB_CHI", "season": 2025, "week": 1,
             "posteam": "GB", "defteam": "CHI", "epa": 5.0, "qb_epa": None,
             "success": 1, "pass": 0, "rush": 0, "down": None, "yards_gained": 80},
        ])


class NFLCanonicalSyncTests(unittest.TestCase):
    def test_coach_ats_splits_roles_sites_and_replays_current_number(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            common = dict(season=2025, season_type="REG", game_time="13:00",
                          overtime=False, division_game=False, stadium=None, roof=None,
                          surface=None, temperature=None, wind=None, total_line=44.0)
            repository.replace_games(2025, (
                Game(game_id="past-away", week=1, game_date="2025-09-01",
                     away_team="ARI", home_team="SEA", away_score=24, home_score=20,
                     spread_line=-3.0, away_coach="Coach A", home_coach="Coach B", **common),
                Game(game_id="past-home-fav", week=2, game_date="2025-09-08",
                     away_team="SEA", home_team="ARI", away_score=20, home_score=21,
                     spread_line=2.5, away_coach="Coach B", home_coach="Coach A", **common),
                Game(game_id="past-home-dog", week=3, game_date="2025-09-15",
                     away_team="SEA", home_team="ARI", away_score=21, home_score=20,
                     spread_line=-3.0, away_coach="Coach B", home_coach="Coach A", **common),
                Game(game_id="current", week=4, game_date="2025-09-22",
                     away_team="ARI", home_team="SEA", away_score=None, home_score=None,
                     spread_line=2.0, away_coach="Coach A", home_coach="Coach B", **common),
            ))
            packet = repository.coach_against_numbers(
                "Coach A", before_game_id="current", current_spread=2.0,
                current_total=44.0,
            )
            self.assertEqual(packet["ats_record"], "2-1")
            self.assertEqual(packet["favorite"]["ats_record"], "1-1")
            self.assertEqual(packet["underdog"]["ats_record"], "1-0")
            self.assertEqual((packet["home"]["games"], packet["away"]["games"]), (2, 1))
            self.assertEqual(packet["versus"]["ats_record"], "3-0")
            self.assertEqual(packet["season"]["total_record"], "0-2-1")
            self.assertEqual(packet["versus_total"]["total_record"], "0-2-1")

    def test_historical_team_alias_cannot_replace_current_branding(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            current = Team("LV", "Las Vegas Raiders", "Raiders", "AFC", "AFC West", None, None, None)
            historical = Team("LV", "Oakland Raiders", "Raiders", "AFC", "AFC West", None, None, None)
            repository.replace_teams([current, historical])
            with closing(sqlite3.connect(repository.path)) as connection:
                self.assertEqual(connection.execute("SELECT name FROM teams").fetchone()[0], "Las Vegas Raiders")

    def test_sync_persists_each_dataset_and_keeps_current_roster_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            report = NFLDataSync(FakeNflverseClient(), repository).sync(2025)

            self.assertTrue(report.succeeded)
            self.assertEqual(repository.counts(2025), {
                # 4 from load_weekly's own row, plus 19 Next Gen Stats metrics:
                # 6 passing + 3 rushing rate metrics each store a natural and
                # a weighted-companion value (18), plus the one directly-
                # summable ngs_rush_yards_over_expected (1). The fake
                # receiving row uses a player_gsis_id load_weekly never
                # mentions, so it is skipped for lack of a game to anchor to.
                "teams": 1, "games": 1, "players": 1, "weekly_metrics": 23,
                "snap_counts": 1, "depth_snapshots": 1,
                "player_master": 1, "external_ids": 5,
                "team_metrics": 2, "game_efficiency": 2,
                "game_situational": 2, "elo_games": 1,
            })
            self.assertEqual(repository.latest_season(), 2025)
            self.assertEqual(repository.get_team("GNB")["name"], "Green Bay Packers")
            self.assertEqual(len(repository.schedule(2025, team="GNB")), 1)
            self.assertEqual(repository.player_leaders(2025, "passing_yards")[0]["value"], 250)
            self.assertEqual(
                {row["metric"] for row in repository.player_leaders_for_metrics(
                    2025, ("attempts", "passing_yards"), team="GNB",
                )},
                {"attempts", "passing_yards"},
            )
            self.assertEqual(repository.team_roster(2025, "GNB")[0]["status"], "RES")
            self.assertEqual(repository.get_game("2025_01_GB_CHI")["home_team"], "CHI")
            self.assertEqual(repository.game_player_stats("2025_01_GB_CHI")[0]["attempts"], 30)
            self.assertEqual(repository.get_player(2025, "00-001")["teams"], ["GNB"])
            self.assertEqual(repository.player_weekly(2025, "00-001")[0]["passing_yards"], 250)
            self.assertEqual(repository.player_totals(2025, "00-001")["passing_yards"], 250)
            usage = repository.team_player_usage(2025, "GNB")[0]
            self.assertEqual(usage["opportunities"], 2)
            self.assertEqual(usage["carry_share"], 1)
            self.assertEqual(repository.team_snap_leaders(2025, "GNB")[0]["offense_snaps"], 62)
            self.assertEqual(repository.current_depth_chart(2025, "GNB")[0]["gsis_id"], "00-001")
            player = repository.get_player(2025, "00-001")
            self.assertEqual(player["draft"]["draft_pick"], 40)
            self.assertEqual(player["external_ids"]["sleeper"], "1234")
            self.assertEqual(repository.identity_coverage()[0]["players"], 1)
            efficiency = repository.team_efficiency(2025, "GNB")
            self.assertAlmostEqual(efficiency["epa_per_play"], 0.5)
            self.assertAlmostEqual(efficiency["defensive_epa_allowed"], -0.2)
            self.assertAlmostEqual(efficiency["defensive_pass_epa_allowed"], -0.2)
            self.assertEqual(
                repository.player_leaders(2025, "passing_yards", team="GNB")[0]["player_name"],
                "Sample Player",
            )
            summary = repository.team_season_summary(2025, "GNB")
            self.assertEqual(summary["games"], 1)
            self.assertEqual(summary["passing_yards_per_game"], 250)
            self.assertEqual(summary["points_per_game"], 24)
            self.assertEqual(repository.game_efficiency("2025_01_GB_CHI")[0]["team"], "GNB")
            self.assertEqual(repository.game_team_stats("2025_01_GB_CHI")["GNB"]["passing_yards"], 250)
            self.assertEqual(repository.game_situational("2025_01_GB_CHI")[0]["plays"], 1)
            playcalling = repository.team_playcalling_profile(2025, "GNB")
            self.assertEqual(playcalling["plays"], 1)
            self.assertEqual(playcalling["pass_rate"], 1)
            # The three bulk "every team at once" variants exist purely for
            # leaguewide ranking (see ranking.py) and must agree with their
            # already-trusted single-team counterparts above.
            league_summary = {row["team"]: row for row in repository.league_team_summary(2025)}
            self.assertEqual(league_summary["GNB"]["passing_yards_per_game"],
                             summary["passing_yards_per_game"])
            self.assertEqual(league_summary["GNB"]["points_per_game"], summary["points_per_game"])
            league_playcalling = {row["team"]: row for row in repository.league_playcalling_profile(2025)}
            self.assertEqual(league_playcalling["GNB"]["pass_rate"], playcalling["pass_rate"])
            situational = repository.team_situational_profile(2025, "GNB")
            league_situational = {row["team"]: row for row in repository.league_situational_profile(2025)}
            self.assertEqual(league_situational["GNB"]["plays_per_game"], situational["plays_per_game"])
            postgame = postgame_packet(
                repository, repository.get_game("2025_01_GB_CHI"),
                repository.game_efficiency("2025_01_GB_CHI"),
                repository.game_player_stats("2025_01_GB_CHI"),
            )
            self.assertEqual((postgame["winner"], postgame["market"]["ats"]),
                             ("GNB", "GNB covered"))
            search = search_nfl_entities(
                repository, NFLContentRepository(repository), "Green Bay", season=2025,
            )
            self.assertEqual(search["teams"][0]["abbreviation"], "GNB")
            self.assertEqual(search["games"][0]["destination"], "review")
            with closing(sqlite3.connect(repository.path)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT abbreviation FROM teams"
                ).fetchone()[0], "GNB")
                self.assertEqual(connection.execute(
                    "SELECT status FROM players"
                ).fetchone()[0], "RES")
                self.assertEqual(connection.execute(
                    "SELECT completed FROM games"
                ).fetchone()[0], 1)
                ngs_metrics = set()
                for _column, metric_key, _weight in (*NGS_PASSING_RATE_METRICS, *NGS_RUSHING_RATE_METRICS):
                    ngs_metrics |= {metric_key, f"{metric_key}_wtd"}
                for _column, metric_key in NGS_RUSHING_SUM_METRICS:
                    ngs_metrics.add(metric_key)
                self.assertEqual(
                    {row[0] for row in connection.execute("SELECT metric FROM player_weekly_stats")},
                    {"attempts", "passing_yards", "targets", "carries"} | ngs_metrics,
                )

    def test_core_groups_partition_every_sync_job_with_no_overlap(self):
        """Regression test for the nfl-core split (bootstrap.py's four
        nfl-core-* steps): every job name sync() can produce must land in
        exactly one group, or a future added dataset would silently vanish
        from every split segment instead of running in any of them."""
        from sports_aggregator.nfl import sync as sync_module
        all_job_names = {
            "teams", "games", "elo", "players", "weekly_stats", "next_gen_stats",
            "snap_counts", "depth_charts", "player_master", "player_ids",
            "team_weekly", "pbp_efficiency",
        }
        groups = (sync_module.CORE_FOUNDATION, sync_module.CORE_STATS,
                 sync_module.CORE_DEPTH, sync_module.CORE_PBP)
        union = frozenset().union(*groups)
        self.assertEqual(union, all_job_names, "every job must belong to some group")
        for first, second in ((a, b) for i, a in enumerate(groups) for b in groups[i + 1:]):
            self.assertEqual(first & second, frozenset(), "groups must not overlap")

    def test_sync_only_parameter_scopes_to_one_group(self):
        from sports_aggregator.nfl.sync import NFLDataSync, CORE_FOUNDATION
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            report = NFLDataSync(FakeNflverseClient(), repository).sync(
                2025, only=CORE_FOUNDATION,
            )
            self.assertEqual({dataset.dataset for dataset in report.datasets}, CORE_FOUNDATION)
            self.assertTrue(report.succeeded)
            # None of the stats/depth-chart/PBP jobs ran, so weekly_metrics
            # stays at zero even though the fake client can supply them.
            self.assertEqual(repository.counts(2025)["weekly_metrics"], 0)

    def test_roster_movement_and_content_links_use_nfl_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            repository.replace_teams((
                Team("GNB", "Green Bay Packers", "Packers", "NFC", "NFC North", None, None, None),
                Team("CHI", "Chicago Bears", "Bears", "NFC", "NFC North", None, None, None),
            ))
            common = dict(first_name="Sample", last_name="Player", position="QB",
                          depth_position="QB", jersey_number=10, status="ACT", birth_date=None,
                          height=74, weight=220, college="Example State", years_experience=2,
                          headshot_url=None, pff_id=None, pfr_id=None, espn_id=None)
            repository.replace_players(2025, (Player(
                season=2025, player_id="00-001", team="GNB", full_name="Sample Player", **common,
            ),))
            repository.replace_players(2026, (Player(
                season=2026, player_id="00-001", team="CHI", full_name="Sample Player", **common,
            ),))
            repository.replace_games(2026, (Game(
                "2026_01_GNB_CHI", 2026, "REG", 1, "2026-09-13", "13:00",
                "GNB", "CHI", None, None, False, True, "Soldier Field", "outdoors",
                "grass", None, None, 1.5, 44.5,
            ),))

            self.assertEqual(
                repository.roster_movements(2026, "CHI")["arrivals"][0]["detail"],
                "From GNB",
            )
            self.assertEqual(
                repository.roster_movements(2026, "GNB")["departures"][0]["detail"],
                "Joined CHI",
            )
            chi_qbs = unit_continuity(repository, 2026, "CHI")[0]
            self.assertEqual((chi_qbs["returning"], chi_qbs["additions"]), (0, 1))
            gnb_qbs = unit_continuity(repository, 2026, "GNB")[0]
            self.assertEqual(gnb_qbs["retention"], 0)
            content = NFLContentRepository(repository)
            article = Article(
                title="Sample Player leads Chicago Bears vs Green Bay Packers preview",
                url="https://example.com/nfl-preview", source="Example NFL",
                published_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
            )
            self.assertEqual(content.ingest_articles((article,), 2026), 1)
            self.assertEqual(len(content.for_team("CHI")), 1)
            self.assertEqual(len(content.for_player(2026, "00-001")), 1)
            self.assertEqual(len(content.for_game("2026_01_GNB_CHI")), 1)
            self.assertEqual(content.counts(), {
                "items": 1, "team_links": 2, "player_links": 1, "game_links": 1,
            })
            repository.replace_players(2026, ())
            self.assertEqual(content.counts()["player_links"], 0)

    def test_schema_drift_fails_only_the_affected_dataset(self):
        class DriftClient(FakeNflverseClient):
            def load_schedules(self, _seasons, **_kwargs):
                return FakeFrame([{"season": 2025}])

        with tempfile.TemporaryDirectory() as directory:
            report = NFLDataSync(
                DriftClient(), NFLRepository(Path(directory) / "nfl.sqlite3")
            ).sync(2025)
            statuses = {item.dataset: item.status for item in report.datasets}
            self.assertEqual(statuses["games"], "failed")
            self.assertEqual(statuses["teams"], "success")
            self.assertEqual(statuses["players"], "success")
            self.assertEqual(statuses["weekly_stats"], "success")

    def test_content_source_audit_distinguishes_output_checks_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            content = NFLContentRepository(repository)
            content.store(
                platform="bluesky", external_id="at://one/post/1",
                url="https://bsky.app/profile/one.test/post/1", title="NFL update",
                body="League update", source_name="One", source_handle="one.test",
                published_at="2026-09-13T12:00:00+00:00", season=2026,
            )
            checked = datetime(2026, 9, 13, 13, tzinfo=timezone.utc)
            content.record_source_checks((
                {"platform": "bluesky", "source_key": "one.test", "display_name": "One",
                 "success": True, "seen": 1, "stored": 1},
                {"platform": "bluesky", "source_key": "two.test", "display_name": "Two",
                 "success": False, "seen": 0, "stored": 0, "error": "unavailable"},
            ), checked_at=checked)
            content.record_ingestion_run(
                "bluesky", 2026, checked, checked, 2, 1, 1, 1,
                [{"handle": "two.test", "error": "unavailable"}],
            )
            sources = [
                {"handle": "one.test", "display_name": "One", "team": "CHI", "tags": []},
                {"handle": "two.test", "display_name": "Two", "team": "GNB", "tags": []},
            ]
            audit = content.source_coverage(sources)
            self.assertEqual((audit["configured"], audit["producing"], audit["checked"], audit["errors"]),
                             (2, 1, 2, 1))
            self.assertEqual({row["handle"]: row["status"] for row in audit["rows"]},
                             {"one.test": "producing", "two.test": "error"})
            self.assertEqual(audit["latest_runs"][0]["attempted"], 2)


class NFLPassingProfileTests(unittest.TestCase):
    def test_targeted_passes_are_queryable_by_quarterback_and_defense(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            rows = [
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "pass_attempt": 1, "passer_player_id": "qb-1", "passer_player_name": "Q One",
                 "receiver_player_id": "wr-1", "receiver_player_name": "R One",
                 "air_yards": 22, "pass_location": "right", "complete_pass": 1,
                 "passing_yards": 31, "receiving_yards": 31, "epa": 1.2, "pass_touchdown": 1,
                 "interception": 0, "cpoe": 8.0},
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "pass_attempt": 1, "passer_player_id": "qb-1", "passer_player_name": "Q One",
                 "receiver_player_id": "wr-2", "receiver_player_name": "R Two",
                 "air_yards": 12, "pass_location": "middle", "complete_pass": 0,
                 "passing_yards": 0, "receiving_yards": 0, "epa": -0.4, "pass_touchdown": 0,
                 "interception": 1, "cpoe": -12.0,
                 "interception_player_id": "db-1", "interception_player_name": "D One"},
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "pass_attempt": 1, "passer_player_id": "qb-1", "air_yards": None,
                 "pass_location": None},
            ]
            self.assertEqual(repository.replace_qb_pass_profiles(2025, rows), 2)
            quarterback = repository.qb_pass_profile(2025, "qb-1")
            defense = repository.defense_pass_profile(2025, "NO")
            self.assertEqual(quarterback["total"]["attempts"], 2)
            self.assertEqual(defense["total"]["passing_yards"], 31)
            self.assertAlmostEqual(quarterback["total"]["epa_per_attempt"], 0.4)
            self.assertEqual(repository.replace_receiver_pass_profiles(2025, rows), 2)
            contributors = repository.pass_zone_contributors(2025, passer_player_id="qb-1")
            deep_right = next(row for row in contributors
                              if row["depth_bucket"] == "deep" and row["pass_location"] == "right")
            self.assertEqual((deep_right["receiver_name"], deep_right["targets"],
                              deep_right["receptions"], deep_right["receiving_yards"],
                              deep_right["touchdowns"]), ("R One", 1, 1, 31, 1))
            self.assertEqual(deep_right["adot"], 22)

            # The interception is an honest, already-attributed defensive
            # credit inside the same zone -- shown alongside the offense's
            # recipients, not instead of them.
            defenders = repository.pass_zone_defenders(2025, defense_team="NO")
            intermediate_middle = next(row for row in defenders
                                       if row["depth_bucket"] == "intermediate"
                                       and row["pass_location"] == "middle")
            self.assertEqual((intermediate_middle["defender_name"], intermediate_middle["event"],
                              intermediate_middle["count"]), ("D One", "interception", 1))


class NFLRushDirectionAndSituationalTests(unittest.TestCase):
    def test_run_direction_classifies_the_standard_seven_cell_chart(self):
        classify = NFLRepository._run_direction
        self.assertEqual(classify("left", "end"), "left end")
        self.assertEqual(classify("right", "tackle"), "right tackle")
        self.assertEqual(classify("middle", None), "middle")
        self.assertIsNone(classify(None, None))
        self.assertIsNone(classify("left", None))  # no gap on a non-middle run is unclassifiable

    def test_rush_direction_profiles_are_queryable_by_rusher_and_defense(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            rows = [
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "rush": 1, "rusher_player_id": "rb-1", "rusher_player_name": "R One",
                 "run_location": "left", "run_gap": "tackle", "yards_gained": 6, "epa": .4,
                 "rush_touchdown": 0, "solo_tackle_1_player_id": "lb-1",
                 "solo_tackle_1_player_name": "L One"},
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "rush": 1, "rusher_player_id": "rb-2", "rusher_player_name": "R Two",
                 "run_location": "left", "run_gap": "tackle", "yards_gained": 3, "epa": -.1,
                 "rush_touchdown": 0, "tackle_with_assist_1_player_id": "lb-1",
                 "tackle_with_assist_1_player_name": "L One"},
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "rush": 1, "rusher_player_id": "rb-1", "rusher_player_name": "R One",
                 "run_location": "middle", "run_gap": None, "yards_gained": 2, "epa": -.3,
                 "rush_touchdown": 0},
                # Unclassifiable (no gap on a non-middle run) -- excluded, not "unknown".
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "rush": 1, "rusher_player_id": "rb-1", "run_location": "right", "run_gap": None,
                 "yards_gained": 1, "epa": -.9},
            ]
            self.assertEqual(repository.replace_rush_direction_profiles(2025, rows), 3)
            runner = repository.rusher_direction_profile(2025, "rb-1")
            self.assertEqual(runner["total"]["attempts"], 2)
            self.assertEqual({d["direction"] for d in runner["directions"]}, {"left tackle", "middle"})
            defense = repository.defense_rush_direction_profile(2025, "NO")
            self.assertEqual(defense["total"]["attempts"], 3)
            left_tackle = next(d for d in defense["directions"] if d["direction"] == "left tackle")
            self.assertAlmostEqual(left_tackle["epa_per_attempt"], .15)  # (.4 + -.1) / 2

            # Full team run game -- both rushers, not just the lead back.
            team = repository.team_rush_direction_profile(2025, "ARI")
            self.assertEqual(team["total"]["attempts"], 3)
            contributors = repository.rush_direction_contributors(2025, offense_team="ARI")
            left_tackle_rushers = {row["rusher_name"] for row in contributors if row["direction"] == "left tackle"}
            self.assertEqual(left_tackle_rushers, {"R One", "R Two"})

            # Both a solo tackle and an assisted tackle credit the same
            # primary tackler (solo wins when both are recorded on separate
            # plays) -- an honest, already-attributed defensive side, not a
            # fabricated coverage assignment.
            defenders = repository.rush_direction_defenders(2025, defense_team="NO")
            tackler = next(row for row in defenders if row["direction"] == "left tackle")
            self.assertEqual((tackler["defender_name"], tackler["tackles"]), ("L One", 2))

    def test_situational_pass_profiles_split_red_zone_and_end_zone(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            rows = [
                # Inside the 20, target reaches the end zone -> counts for both.
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "pass_attempt": 1, "passer_player_id": "qb-1", "passer_player_name": "Q One",
                 "receiver_player_id": "wr-1", "receiver_player_name": "R One",
                 "yardline_100": 8, "air_yards": 8, "complete_pass": 1, "passing_yards": 8,
                 "receiving_yards": 8, "epa": 2.1, "pass_touchdown": 1, "interception": 0},
                # Inside the 20, target short of the end zone -> red zone only.
                # Broken up by a defender -- the honest defensive credit.
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "pass_attempt": 1, "passer_player_id": "qb-1", "passer_player_name": "Q One",
                 "receiver_player_id": "wr-1", "receiver_player_name": "R One",
                 "yardline_100": 15, "air_yards": 5, "complete_pass": 0, "passing_yards": 0,
                 "receiving_yards": 0, "epa": -.6, "pass_touchdown": 0, "interception": 0,
                 "pass_defense_1_player_id": "cb-1", "pass_defense_1_player_name": "C One"},
                # Outside the 20 entirely -> neither situation.
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "pass_attempt": 1, "passer_player_id": "qb-1", "passer_player_name": "Q One",
                 "receiver_player_id": "wr-1", "receiver_player_name": "R One",
                 "yardline_100": 55, "air_yards": 10, "complete_pass": 1, "passing_yards": 10,
                 "receiving_yards": 10, "epa": .3, "pass_touchdown": 0, "interception": 0},
            ]
            # 3 plays collapse into 2 (game, passer, situation) buckets: row 1
            # counts toward both red_zone and end_zone (same passer/game), so
            # those share one red_zone bucket with row 2; row 3 (outside the
            # 20) contributes to neither.
            self.assertEqual(repository.replace_situational_pass_profiles(2025, rows), 2)
            passer = repository.qb_situational_profile(2025, "qb-1")
            self.assertEqual(passer["red_zone"]["attempts"], 2)
            self.assertEqual(passer["end_zone"]["attempts"], 1)
            self.assertEqual(passer["end_zone"]["touchdowns"], 1)
            defense = repository.defense_situational_profile(2025, "NO")
            self.assertEqual(defense["red_zone"]["attempts"], 2)
            with closing(repository._connect()) as connection:
                receiver_rows = list(connection.execute(
                    "SELECT situation,targets FROM receiver_situational_profiles WHERE receiver_player_id='wr-1'"
                ))
            self.assertEqual({row["situation"]: row["targets"] for row in receiver_rows},
                             {"red_zone": 2, "end_zone": 1})

            contributors = repository.situational_pass_contributors(2025, passer_player_id="qb-1")
            self.assertEqual(contributors["red_zone"][0]["targets"], 2)
            defenders = repository.situational_pass_defenders(2025, defense_team="NO")
            self.assertEqual(defenders["red_zone"][0]["defender_name"], "C One")
            self.assertEqual(defenders["red_zone"][0]["event"], "pass_defended")

    def test_rush_situational_profiles_are_the_full_run_game_not_one_back(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            rows = [
                # Inside the 20 -- counts.
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "rush": 1, "rusher_player_id": "rb-1", "rusher_player_name": "R One",
                 "yardline_100": 12, "yards_gained": 5, "epa": .5, "rush_touchdown": 1,
                 "solo_tackle_1_player_id": "lb-1", "solo_tackle_1_player_name": "L One"},
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "rush": 1, "rusher_player_id": "rb-2", "rusher_player_name": "R Two",
                 "yardline_100": 5, "yards_gained": 2, "epa": -.2, "rush_touchdown": 0},
                # Outside the 20 -- excluded.
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "rush": 1, "rusher_player_id": "rb-1", "yardline_100": 60,
                 "yards_gained": 4, "epa": .1, "rush_touchdown": 0},
            ]
            self.assertEqual(repository.replace_rush_situational_profiles(2025, rows), 2)
            team = repository.team_rush_situational_profile(2025, "ARI")
            self.assertEqual(team["attempts"], 2)  # both backs, not just one
            defense = repository.defense_rush_situational_profile(2025, "NO")
            self.assertEqual(defense["attempts"], 2)
            contributors = repository.rush_situational_contributors(2025, offense_team="ARI")
            self.assertEqual({row["rusher_name"] for row in contributors}, {"R One", "R Two"})
            defenders = repository.rush_situational_defenders(2025, defense_team="NO")
            self.assertEqual((defenders[0]["defender_name"], defenders[0]["tackles"]), ("L One", 1))

    def test_defender_position_is_never_none_to_avoid_crashing_jinja_groupby(self):
        """Regression test: a mixed None/string `position` column crashes
        Jinja's `groupby` filter (it sorts by the key first, and Python
        cannot order None against a string) -- the query methods must
        normalize a missing position before a template ever sees it."""
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            rows = [
                {"game_id": "2025_01_ARI_NO", "week": 1, "posteam": "ARI", "defteam": "NO",
                 "rush": 1, "rusher_player_id": "rb-1", "rusher_player_name": "R One",
                 "run_location": "middle", "run_gap": None, "yards_gained": 3, "epa": .2,
                 "rush_touchdown": 0, "solo_tackle_1_player_id": "lb-unrostered",
                 "solo_tackle_1_player_name": "No Roster Match"},
            ]
            repository.replace_rush_direction_profiles(2025, rows)
            defenders = repository.rush_direction_defenders(2025, defense_team="NO")
            self.assertEqual(defenders[0]["position"], "UNK")


class RunDirectionPacketTests(unittest.TestCase):
    def test_run_matchup_edge_combines_offense_and_defense_additively(self):
        offense = run_direction_packet({"directions": [
            {"direction": "left end", "attempts": 5, "rushing_yards": 20, "epa_per_attempt": .3},
        ], "total": {}})
        defense = run_direction_packet({"directions": [
            {"direction": "left end", "attempts": 9, "rushing_yards": 40, "epa_per_attempt": .5},
        ], "total": {}}, lower_is_better=True)
        matchup = run_matchup_packet(offense, defense)
        cell = next(c for c in matchup["cells"] if c["direction"] == "left end")
        # Same offense-relative sign convention as the pass matchup: a good
        # offense (+.3) against a defense that's leaky there (+.5 allowed)
        # combines to a strong offense edge, not a canceled-out one.
        self.assertAlmostEqual(cell["edge"], .8)
        self.assertEqual(cell["lean"], "offense")

    def test_run_direction_packet_covers_all_seven_cells_even_when_sparse(self):
        profile = run_direction_packet({"directions": [
            {"direction": "middle", "attempts": 3, "rushing_yards": 9, "epa_per_attempt": .1},
        ], "total": {}})
        self.assertEqual(len(profile["cells"]), 7)
        middle = next(c for c in profile["cells"] if c["direction"] == "middle")
        self.assertEqual(middle["tone"], "good")
        empty = next(c for c in profile["cells"] if c["direction"] == "left guard")
        self.assertEqual(empty["tone"], "neutral")


class NFLSourceDirectoryTests(unittest.TestCase):
    def test_real_data_source_workbook_separates_paid_candidates(self):
        sources = load_data_sources("data/nfl/NFL_Data_Sources_Directory.xlsx")
        available = free_sources("data/nfl/NFL_Data_Sources_Directory.xlsx")
        self.assertEqual(len(sources), 28)
        self.assertEqual(len(available), 25)
        self.assertEqual({source.provider for source in sources if source.paid},
                         {"Sportradar", "SportsDataIO"})
        self.assertIn("Snap counts", {source.dataset for source in available})

    def test_real_workbook_has_complete_team_and_section_coverage(self):
        profiles = load_directory("data/nfl/NFL_Bluesky_Directory.xlsx")
        self.assertEqual(len(profiles), 158)
        self.assertEqual(len({profile.source.handle for profile in profiles}), 158)
        self.assertEqual(len({profile.team for profile in profiles if profile.team}), 32)
        self.assertEqual(
            {tag[2] for profile in profiles for tag in profile.tags},
            {"overview", "wire", "analysis", "personnel", "players", "beats"},
        )

    def test_real_rss_directory_covers_every_team_and_national_feed(self):
        national = national_feeds()
        # 15 National RSS rows + 9 Other Communities rows, minus 2 URLs
        # (r/nfl, r/NFL_Draft) listed in both sheets.
        self.assertEqual(len(national), 22)
        self.assertEqual(len({feed.url for feed in national}), 22)
        self.assertIn("ESPN NFL Headlines", {feed.name for feed in national})

        teams = team_feeds()
        self.assertEqual(len(teams), 32)
        # Every team has a subreddit, a PFF feed, and a blog feed.
        self.assertTrue(all(len(feeds) == 3 for feeds in teams.values()))
        self.assertEqual({feed.source_type for feeds in teams.values() for feed in feeds
                          if "reddit.com" in feed.url}, {"reddit"})
        cin = {feed.name: feed.url for feed in teams["CIN"]}
        self.assertEqual(cin["r/bengals"], "https://www.reddit.com/r/bengals/.rss")
        # "TB" in the workbook canonicalizes to the same code the rest of the
        # app uses for Tampa Bay.
        self.assertIn("TAM", teams)

    def test_ingest_rss_feeds_stores_articles_and_biases_team_match(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            repository.replace_teams((
                Team("CHI", "Chicago Bears", "Bears", "NFC", "NFC North", None, None, None),
            ))
            content = NFLContentRepository(repository)
            good_feed = FeedConfig(name="Windy City Gridiron", url="https://example.com/chi.xml")
            bad_feed = FeedConfig(name="Broken Feed", url="https://example.com/broken.xml")

            def fake_fetch(feed, _timeout):
                if feed is bad_feed:
                    raise ProviderFetchError("boom")
                return [Article(title="Bears roster news", url="https://example.com/a1",
                                source="Windy City Gridiron",
                                published_at=datetime(2026, 9, 12, tzinfo=timezone.utc))]

            with patch.object(NFLContentRepository, "_fetch_feed", staticmethod(fake_fetch)):
                result = content.ingest_rss_feeds(
                    [(good_feed, "CHI"), (bad_feed, None)], 2026, workers=2,
                )
            self.assertEqual(result, {"feeds": 2, "succeeded": 1, "seen": 1, "stored": 1,
                                      "errors": [{"feed": "Broken Feed",
                                                  "url": "https://example.com/broken.xml",
                                                  "error": "boom"}]})
            self.assertEqual(len(content.for_team("CHI")), 1)

    def test_import_preserves_existing_cfb_metadata_and_routes_nfl_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SourceRegistry(Path(directory) / "shared.sqlite3")
            registry.seed((SourceProfile(
                handle="aaronschatz.com", display_name="Existing name", organization="Existing org",
                source_type="REPORTER", specialties=("college_football", "analysis"),
                teams=("Sample University",), priority=4,
            ),))
            self.assertEqual(import_directory(
                registry, "data/nfl/NFL_Bluesky_Directory.xlsx"
            ), 158)

            analysis = registry.list_league_sources("nfl", section="analysis")
            self.assertIn("aaronschatz.com", {row["handle"] for row in analysis})
            with closing(sqlite3.connect(registry.path)) as connection:
                source_id = connection.execute(
                    "SELECT source_id FROM sources WHERE handle='aaronschatz.com'"
                ).fetchone()[0]
                specialties = {row[0] for row in connection.execute(
                    "SELECT specialty FROM source_specialties WHERE source_id=?", (source_id,)
                )}
                teams = {row[0] for row in connection.execute(
                    "SELECT team FROM source_teams WHERE source_id=?", (source_id,)
                )}
                identity = connection.execute(
                    "SELECT display_name,organization FROM sources WHERE source_id=?", (source_id,)
                ).fetchone()
            self.assertEqual(specialties, {"college_football", "analysis"})
            self.assertEqual(teams, {"Sample University"})
            self.assertEqual(identity, ("Existing name", "Existing org"))


class _FakeAlignmentRepository:
    """Duck-typed stand-in for NFLRepository -- alignment_matchups and
    rushing_matchups only call these methods, so a real sqlite-backed
    repository (with PFF fixture families synced through it) isn't needed
    to exercise the pure logic."""

    def __init__(self, *, weeks_played, rosters, usage=None, receiver_profiles=None,
                season_stats=None):
        self._weeks_played = weeks_played
        self._rosters = rosters
        self._usage = usage or {}
        self._receiver_profiles = receiver_profiles or {}
        # {(season, team): [row, ...]}, each row shaped like a real
        # player_season_stats() result (player_id plus whatever metrics
        # were asked for).
        self._season_stats = season_stats or {}

    def latest_stat_week(self, season):
        return self._weeks_played[season]

    def team_roster(self, _season, team):
        return [{"player_id": player_id} for player_id in self._rosters.get(team, ())]

    def team_player_usage(self, _season, team):
        return self._usage.get(team, [])

    def receiver_pass_profile(self, _season, player_id):
        return self._receiver_profiles.get(player_id, {"zones": [], "total": {}})

    def player_season_stats(self, season, _metrics, *, team=None, **_kwargs):
        return self._season_stats.get((season, team), [])


class _FakeAlignmentPFF:
    def __init__(self, receivers=None, slot_defenders=None, runners=None, league_leaders=None):
        self._receivers = receivers or {}
        self._slot_defenders = slot_defenders or {}
        self._runners = runners or {}
        self._league_leaders = league_leaders or {}

    def family_profiles(self, _season, family, team, _fields):
        if family == "receiving_summary":
            return self._receivers.get(team, [])
        if family == "slot_coverage":
            return self._slot_defenders.get(team, [])
        if family == "rushing_summary":
            return self._runners.get(team, [])
        return []

    def leaders(self, _season, family, metric, *, team=None, limit=25, lower=False,
               minimum_metric=None, minimum_value=0):
        return self._league_leaders.get((family, metric), [])


class AlignmentMatchupTests(unittest.TestCase):
    def test_weak_zone_threshold_scales_off_the_defense_zones_own_season_not_pff_season(self):
        """Regression test for a real bug caught by live-server verification:
        defense zones come from the CURRENT live season (often just 1-2 weeks
        in), while pff_season/receiver data can lag a full season behind and
        so have a much higher week count. Scaling the zone-attempts floor off
        pff_season's week count against defense zones from a nearly-empty
        current season silently zeroed every weak-zone hit -- each zone had
        3 attempts against an unscaled 20-attempt floor."""
        game = {"away_team": "AAA", "home_team": "BBB", "season": 2026}
        repository = _FakeAlignmentRepository(
            weeks_played={2025: 22, 2026: 1},  # pff_season=2025 (lagging, full); live season=2026 (week 1)
            rosters={"AAA": ("r1", "r2"), "BBB": ()},
            usage={"AAA": [{"player_id": "r1", "targets": 20, "target_share": .3},
                           {"player_id": "r2", "targets": 15, "target_share": .2}]},
            receiver_profiles={
                "r1": {"zones": [
                    {"depth_bucket": "short", "pass_location": "left", "targets": 5,
                     "epa_per_target": .4, "receptions": 4, "receiving_yards": 40},
                    {"depth_bucket": "short", "pass_location": "middle", "targets": 3,
                     "epa_per_target": .1, "receptions": 2, "receiving_yards": 15},
                ], "total": {"targets": 8}},
                "r2": {"zones": [
                    {"depth_bucket": "short", "pass_location": "right", "targets": 4,
                     "epa_per_target": -.2, "receptions": 3, "receiving_yards": 20},
                ], "total": {"targets": 4}},
            },
        )
        pff = _FakeAlignmentPFF(receivers={"AAA": [
            # pff_season=2025 is a full 22-week season, so the 100-route
            # floor is unscaled -- both receivers need to clear it outright.
            {"gsis_id": "r1", "player_name": "Receiver One", "routes": 150,
             "grades_pass_route": 80.0, "yprr": 2.1, "slot_rate": 10, "wide_rate": 90},
            {"gsis_id": "r2", "player_name": "Receiver Two", "routes": 120,
             "grades_pass_route": 70.0, "yprr": 1.5, "slot_rate": 10, "wide_rate": 90},
        ]})
        # BBB's own defense zones this season: only 3 attempts each -- would
        # fail a hardcoded (or pff_season-scaled) 20-attempt floor entirely.
        defense_profiles = {"BBB": {"season": 2026, "zones": [
            {"depth_bucket": "short", "pass_location": "left", "attempts": 3,
             "epa_per_attempt": .5, "completion_rate": .8},
            {"depth_bucket": "short", "pass_location": "middle", "attempts": 3,
             "epa_per_attempt": 0.0, "completion_rate": .6},
            {"depth_bucket": "short", "pass_location": "right", "attempts": 3,
             "epa_per_attempt": -.5, "completion_rate": .4},
        ]}}

        cards = alignment_matchups(repository, pff, game, 2026, 2025, defense_profiles)
        by_player = {card["player_name"]: card for card in cards}

        r1 = by_player["Receiver One"]
        # short/left sits above BBB's own zone median (0.0) -> flagged weak;
        # short/middle sits exactly at the median -> not weak (ties don't count).
        self.assertEqual(r1["weak_zone_hits"], 1)
        weak_flags = {(zone["depth_bucket"], zone["pass_location"]): zone["is_weak_zone"]
                     for zone in r1["zone_matchups"]}
        self.assertEqual(weak_flags, {("short", "left"): True, ("short", "middle"): False})
        self.assertAlmostEqual(r1["vulnerable_zone"]["epa_per_attempt"], .5)

        r2 = by_player["Receiver Two"]
        self.assertEqual(r2["weak_zone_hits"], 0)

        # A receiver hitting a weak zone ranks ahead of one who doesn't, even
        # though Receiver Two isn't behind Receiver One on raw target volume.
        self.assertEqual([card["player_name"] for card in cards if card["offense"] == "AAA"],
                         ["Receiver One", "Receiver Two"])

    def test_widened_candidate_pool_reorders_by_matchup_fit_not_raw_volume(self):
        game = {"away_team": "AAA", "home_team": "BBB", "season": 2026}
        repository = _FakeAlignmentRepository(
            weeks_played={2026: 10},
            rosters={"AAA": ("r1", "r2", "r3"), "BBB": ()},
            usage={"AAA": [{"player_id": "r1", "targets": 40, "target_share": .35},
                           {"player_id": "r2", "targets": 30, "target_share": .25},
                           {"player_id": "r3", "targets": 10, "target_share": .1}]},
            receiver_profiles={
                # Highest-volume receiver: all his targets land in a zone
                # this defense is actually good in (negative EPA allowed).
                "r1": {"zones": [{"depth_bucket": "deep", "pass_location": "right", "targets": 10,
                                  "epa_per_target": .3, "receptions": 4, "receiving_yards": 90}],
                      "total": {"targets": 10}},
                # Lower-volume receiver: concentrated in the defense's worst zone.
                "r3": {"zones": [{"depth_bucket": "short", "pass_location": "left", "targets": 8,
                                  "epa_per_target": .2, "receptions": 6, "receiving_yards": 50}],
                      "total": {"targets": 8}},
            },
        )
        pff = _FakeAlignmentPFF(receivers={"AAA": [
            {"gsis_id": "r1", "player_name": "Volume Guy", "routes": 100,
             "grades_pass_route": 75.0, "yprr": 1.8, "slot_rate": 5, "wide_rate": 95},
            {"gsis_id": "r2", "player_name": "No Zone Data", "routes": 80,
             "grades_pass_route": 65.0, "yprr": 1.2, "slot_rate": 5, "wide_rate": 95},
            {"gsis_id": "r3", "player_name": "Weak Spot Exploiter", "routes": 60,
             "grades_pass_route": 72.0, "yprr": 2.4, "slot_rate": 5, "wide_rate": 95},
        ]})
        defense_profiles = {"BBB": {"season": 2026, "zones": [
            {"depth_bucket": "short", "pass_location": "left", "attempts": 15,
             "epa_per_attempt": .6, "completion_rate": .8},
            {"depth_bucket": "deep", "pass_location": "right", "attempts": 15,
             "epa_per_attempt": -.6, "completion_rate": .3},
            {"depth_bucket": "short", "pass_location": "right", "attempts": 15,
             "epa_per_attempt": 0.0, "completion_rate": .5},
        ]}}

        cards = alignment_matchups(repository, pff, game, 2026, 2026, defense_profiles)
        offense_cards = [card for card in cards if card["offense"] == "AAA"]
        # All three qualifying receivers are selected (pool widened past 2)...
        self.assertEqual({card["player_name"] for card in offense_cards},
                         {"Volume Guy", "No Zone Data", "Weak Spot Exploiter"})
        # ...but the receiver who actually exploits the defense's weak zone
        # is ranked first despite lower raw target volume.
        self.assertEqual(offense_cards[0]["player_name"], "Weak Spot Exploiter")
        self.assertEqual(offense_cards[0]["weak_zone_hits"], 1)
        self.assertEqual(offense_cards[1]["weak_zone_hits"], 0)

    def test_alignment_cards_carry_season_weighted_ngs_separation_and_cushion(self):
        """Next Gen Stats separation/cushion on each receiver card are a
        volume-weighted season average, not a raw per-week number -- same
        SUM(weighted)/SUM(weight) shape explorer.py uses for PACR/RACR,
        exercised here through alignment_matchups' own lookup."""
        game = {"away_team": "AAA", "home_team": "BBB", "season": 2026}
        repository = _FakeAlignmentRepository(
            weeks_played={2026: 10},
            rosters={"AAA": ("r1",), "BBB": ()},
            usage={"AAA": [{"player_id": "r1", "targets": 20, "target_share": .3}]},
            receiver_profiles={"r1": {"zones": [], "total": {"targets": 20}}},
            season_stats={(2026, "AAA"): [
                # 3.5 avg separation and 6.0 avg cushion weighted across 20 targets.
                {"player_id": "r1", "targets": 20,
                 "ngs_rec_separation_wtd": 70.0, "ngs_rec_cushion_wtd": 120.0},
            ]},
        )
        pff = _FakeAlignmentPFF(receivers={"AAA": [
            {"gsis_id": "r1", "player_name": "Receiver One", "routes": 100,
             "grades_pass_route": 75.0, "yprr": 1.8, "slot_rate": 5, "wide_rate": 95},
        ]})
        cards = alignment_matchups(repository, pff, game, 2026, 2026, {"BBB": {"season": 2026, "zones": []}})
        card = next(card for card in cards if card["player_name"] == "Receiver One")
        self.assertAlmostEqual(card["ngs_separation"], 3.5)
        self.assertAlmostEqual(card["ngs_cushion"], 6.0)

    def test_alignment_cards_carry_a_leaguewide_route_grade_rank(self):
        """Route grade rank comes from a leaguewide PFF pool via leaders(),
        not just this game's own candidates -- reuses the same shared
        ranking helper (rank_within) NGS separation/cushion already use."""
        game = {"away_team": "AAA", "home_team": "BBB", "season": 2026}
        repository = _FakeAlignmentRepository(
            weeks_played={2026: 10}, rosters={"AAA": ("r1",), "BBB": ()},
            usage={"AAA": [{"player_id": "r1", "targets": 20, "target_share": .3}]},
            receiver_profiles={"r1": {"zones": [], "total": {"targets": 20}}},
        )
        pff = _FakeAlignmentPFF(
            receivers={"AAA": [
                {"gsis_id": "r1", "player_name": "Receiver One", "routes": 100,
                 "grades_pass_route": 75.0, "yprr": 1.8, "slot_rate": 5, "wide_rate": 95},
            ]},
            league_leaders={("receiving_summary", "grades_pass_route"): [
                {"gsis_id": "other1", "value": 90.0},
                {"gsis_id": "r1", "value": 75.0},
                {"gsis_id": "other2", "value": 60.0},
            ]},
        )
        cards = alignment_matchups(repository, pff, game, 2026, 2026, {"BBB": {"season": 2026, "zones": []}})
        card = next(card for card in cards if card["player_name"] == "Receiver One")
        self.assertEqual(card["route_grade_rank"], {"rank": 2, "of": 3})

    def test_alignment_card_ngs_fields_are_none_without_a_season_stats_match(self):
        """A receiver with no matching Next Gen Stats row (not yet synced,
        or no qualifying targets) gets None rather than a KeyError or a
        misleading zero."""
        game = {"away_team": "AAA", "home_team": "BBB", "season": 2026}
        repository = _FakeAlignmentRepository(
            weeks_played={2026: 10}, rosters={"AAA": ("r1",), "BBB": ()},
            usage={"AAA": [{"player_id": "r1", "targets": 20, "target_share": .3}]},
            receiver_profiles={"r1": {"zones": [], "total": {"targets": 20}}},
        )
        pff = _FakeAlignmentPFF(receivers={"AAA": [
            {"gsis_id": "r1", "player_name": "Receiver One", "routes": 100,
             "grades_pass_route": 75.0, "yprr": 1.8, "slot_rate": 5, "wide_rate": 95},
        ]})
        cards = alignment_matchups(repository, pff, game, 2026, 2026, {"BBB": {"season": 2026, "zones": []}})
        card = next(card for card in cards if card["player_name"] == "Receiver One")
        self.assertIsNone(card["ngs_separation"])
        self.assertIsNone(card["ngs_cushion"])


class RushingMatchupNgsTests(unittest.TestCase):
    def test_run_game_cards_carry_season_weighted_ngs_efficiency_and_ryoe(self):
        game = {"away_team": "AAA", "home_team": "BBB", "season": 2026}
        repository = _FakeAlignmentRepository(
            weeks_played={2026: 10}, rosters={"AAA": ("b1",)},
            usage={"AAA": [{"player_id": "b1", "carries": 15, "carry_share": .6,
                            "rushing_yards": 80}]},
            season_stats={(2026, "AAA"): [
                # 4.2 avg efficiency weighted across 15 carries; RYOE already
                # a real yards total (12.0), so it sums with no weighting.
                {"player_id": "b1", "carries": 15, "ngs_rush_efficiency_wtd": 63.0,
                 "ngs_rush_stacked_box_pct_wtd": 150.0, "ngs_rush_yards_over_expected": 12.0},
            ]},
        )
        pff = _FakeAlignmentPFF(runners={"AAA": [
            {"gsis_id": "b1", "player_name": "Back One", "attempts": 15,
             "grades_run": 72.0, "elusive_rating": 60.0, "yco_attempt": 2.1,
             "avoided_tackles": 3},
        ]})
        cards = rushing_matchups(repository, pff, game, 2026, 2026, {"BBB": {}})
        card = next(card for card in cards if card["player_name"] == "Back One")
        self.assertAlmostEqual(card["ngs_efficiency"], 4.2)
        self.assertAlmostEqual(card["ngs_stacked_box_pct"], 10.0)
        self.assertAlmostEqual(card["ngs_yards_over_expected"], 12.0)

    def test_run_game_cards_carry_a_leaguewide_run_grade_rank(self):
        game = {"away_team": "AAA", "home_team": "BBB", "season": 2026}
        repository = _FakeAlignmentRepository(
            weeks_played={2026: 10}, rosters={"AAA": ("b1",)},
            usage={"AAA": [{"player_id": "b1", "carries": 15, "carry_share": .6,
                            "rushing_yards": 80}]},
        )
        pff = _FakeAlignmentPFF(
            runners={"AAA": [
                {"gsis_id": "b1", "player_name": "Back One", "attempts": 15,
                 "grades_run": 72.0, "elusive_rating": 60.0, "yco_attempt": 2.1,
                 "avoided_tackles": 3},
            ]},
            league_leaders={("rushing_summary", "grades_run"): [
                {"gsis_id": "other1", "value": 85.0},
                {"gsis_id": "b1", "value": 72.0},
            ]},
        )
        cards = rushing_matchups(repository, pff, game, 2026, 2026, {"BBB": {}})
        card = next(card for card in cards if card["player_name"] == "Back One")
        self.assertEqual(card["run_grade_rank"], {"rank": 2, "of": 2})


class ViewTableHelperTests(unittest.TestCase):
    def test_zone_matchup_table_columns_align_via_real_table_not_css_grid(self):
        item = {
            "offense": "CAR", "defense": "ATL",
            "zone_matchups": [
                {"depth_bucket": "short", "pass_location": "right", "targets": 14,
                 "receptions": 9, "receiving_yards": 56, "offense_epa": .14,
                 "target_share": .206, "defense_attempts": 6, "defense_epa": .52,
                 "is_weak_zone": True},
                {"depth_bucket": "intermediate", "pass_location": "middle", "targets": 7,
                 "receptions": 4, "receiving_yards": 68, "offense_epa": .89,
                 "target_share": .103, "defense_attempts": 1, "defense_epa": 2.20,
                 "is_weak_zone": False},
            ],
        }
        table = zone_matchup_table(item)
        self.assertEqual([column.label for column in table.columns],
                         ["Zone", "CAR EPA/tgt", "ATL allowed"])
        self.assertEqual(table.rows[0]["zone"], "Short right")
        self.assertEqual(table.rows[0]["zone_class"], "weak-zone")
        self.assertIn("9/14", table.rows[0]["zone_sub"])
        self.assertIsNone(table.rows[1]["zone_class"])

    def test_zone_matchup_table_empty_state_when_no_zones(self):
        table = zone_matchup_table({"offense": "CAR", "defense": "ATL", "zone_matchups": []})
        self.assertEqual(table.rows, [])
        self.assertIn("Not enough", table.empty)

    def test_ingestion_runs_table_counts_errors_per_run(self):
        table = ingestion_runs_table([
            {"platform": "rss_directory", "season": 2026, "finished_at": "2026-09-15T12:00:00",
             "attempted": 118, "succeeded": 117, "seen": 900, "stored": 850,
             "errors_json": '[{"feed": "Reddit r/nfl", "error": "429"}]'},
            {"platform": "bluesky", "season": 2026, "finished_at": "2026-09-15T12:05:00",
             "attempted": 158, "succeeded": 158, "seen": 400, "stored": 400,
             "errors_json": "[]"},
        ])
        self.assertEqual(table.rows[0]["errors"], 1)
        self.assertEqual(table.rows[0]["errors_class"], "error")
        self.assertEqual(table.rows[1]["errors"], 0)
        self.assertIsNone(table.rows[1]["errors_class"])


class NFLSituationContextTests(unittest.TestCase):
    """Weather, travel and schedule-trap-spot context, mirroring the CFB
    side's situational module -- see sports_aggregator/nfl/matchups.py and
    sports_aggregator/nfl/weather.py."""

    def test_team_venues_seed_automatically_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            venues = repository.team_venues()
            self.assertEqual(len(venues), 32)
            self.assertEqual(venues["SEA"]["venue_name"], "Lumen Field")
            self.assertFalse(venues["SEA"]["dome"])
            self.assertTrue(venues["MIN"]["dome"])

    def test_travel_detects_long_haul_timezone_and_altitude(self):
        from sports_aggregator.nfl.matchups import _travel
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            # Seattle (Pacific) at Miami (Eastern): a long, cross-country,
            # three-time-zone trip -- notable on distance and shift alone.
            game = {"away_team": "SEA", "home_team": "MIA",
                    "game_date": "2026-11-01", "game_time": "13:00"}
            travel = _travel(repository, game)
            self.assertGreater(travel["miles"], 2500)
            self.assertEqual(travel["timezone_shift"], 3)
            self.assertTrue(travel["notable"])
            self.assertIsNone(travel["altitude"])

            # Same distance class, but the home venue is Denver's altitude --
            # the away team is not from a lowland team here, so no altitude
            # flag should fire (the CFB-mirrored rule only fires sea-level
            # visitor vs. high-elevation host).
            denver_game = {"away_team": "MIA", "home_team": "DEN",
                           "game_date": "2026-11-01", "game_time": "13:00"}
            denver_travel = _travel(repository, denver_game)
            self.assertIsNotNone(denver_travel["altitude"])
            self.assertIn("feet", denver_travel["altitude"])

    def test_travel_is_not_notable_for_a_short_same_zone_trip(self):
        from sports_aggregator.nfl.matchups import _travel
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            game = {"away_team": "CLE", "home_team": "TAM",
                    "game_date": "2026-11-01", "game_time": "13:00"}
            travel = _travel(repository, game)
            self.assertEqual(travel["timezone_shift"], 0)
            self.assertFalse(travel["notable"])

    def test_trap_spots_detect_look_ahead_and_revenge(self):
        from sports_aggregator.nfl.matchups import _trap_spots
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            common = dict(season=2026, season_type="REG", game_time="13:00",
                         overtime=False, division_game=False, stadium=None, roof=None,
                         surface=None, temperature=None, wind=None,
                         spread_line=None, total_line=None)
            repository.replace_games(2026, (
                # This week: a big favorite (KAN) hosts a weak team (NYJ).
                Game(game_id="this-week", week=5, game_date="2026-10-04",
                     away_team="NYJ", home_team="KAN", away_score=None, home_score=None,
                     **common),
                # Next week KAN plays a much tougher opponent -- look-ahead.
                Game(game_id="next-week", week=6, game_date="2026-10-11",
                     away_team="KAN", home_team="BUF", away_score=None, home_score=None,
                     **common),
            ))
            # Last season NYJ beat KAN -- this year's rematch is revenge for KAN.
            last_season = {**common, "season": 2025}
            repository.replace_games(2025, (
                Game(game_id="last-season-meeting", week=5, game_date="2025-10-05",
                     away_team="NYJ", home_team="KAN", away_score=27, home_score=20,
                     **last_season),
            ))
            elo_ratings = {"KAN": 1700.0, "NYJ": 1300.0, "BUF": 1650.0}
            game = repository.get_game("this-week")
            signals = _trap_spots(repository, game, elo_ratings)
            types = {signal["type"] for signal in signals}
            self.assertIn("LOOK_AHEAD", types)
            self.assertIn("REVENGE", types)
            look_ahead = next(s for s in signals if s["type"] == "LOOK_AHEAD")
            self.assertIn("KAN plays BUF next", look_ahead["headline"])
            revenge = next(s for s in signals if s["type"] == "REVENGE")
            self.assertIn("KAN lost this matchup last season", revenge["headline"])

    def test_kickoff_utc_resolves_eastern_daylight_and_standard_time(self):
        from sports_aggregator.nfl.weather import kickoff_utc
        # Early September: Eastern Daylight Time, UTC-4.
        september = kickoff_utc({"game_date": "2026-09-13", "game_time": "13:00"})
        self.assertTrue(september.startswith("2026-09-13T17:00"))
        # Mid-January: Eastern Standard Time, UTC-5 -- the whole reason this
        # uses zoneinfo instead of a fixed offset guess.
        january = kickoff_utc({"game_date": "2026-01-13", "game_time": "13:00"})
        self.assertTrue(january.startswith("2026-01-13T18:00"))
        self.assertIsNone(kickoff_utc({"game_date": None, "game_time": "13:00"}))

    def test_weather_snapshots_accumulate_and_track_movement(self):
        from sports_aggregator.providers.weather import Forecast
        from sports_aggregator.nfl.weather import store_weather, weather_for_game
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            early = Forecast(kickoff="2026-10-11T17:00:00+00:00", forecast_hour="2026-10-11T17:00",
                             temperature=60.0, precipitation_probability=10.0, precipitation=0.0,
                             wind_speed=5.0, wind_gusts=8.0, humidity=50.0, visibility=10000.0,
                             weather_code=1)
            late = Forecast(kickoff="2026-10-11T17:00:00+00:00", forecast_hour="2026-10-11T17:00",
                            temperature=45.0, precipitation_probability=70.0, precipitation=0.3,
                            wind_speed=18.0, wind_gusts=28.0, humidity=80.0, visibility=6000.0,
                            weather_code=61)
            store_weather(repository, "2026_06_BUF_KAN", early, flags=[], venue="Arrowhead",
                         latitude=39.05, longitude=-94.48, indoor=False,
                         generated_at="2026-09-25T00:00:00+00:00")
            store_weather(repository, "2026_06_BUF_KAN", late, flags=[{"flag": "RAIN_RISK", "detail": "70% chance"}],
                         venue="Arrowhead", latitude=39.05, longitude=-94.48, indoor=False,
                         generated_at="2026-10-11T00:00:00+00:00")
            result = weather_for_game(repository, "2026_06_BUF_KAN")
            self.assertTrue(result["available"])
            self.assertEqual(result["snapshots"], 2)
            self.assertEqual(result["latest"]["temperature"], 45.0)
            self.assertAlmostEqual(result["movement"]["temperature"], -15.0)
            self.assertAlmostEqual(result["movement"]["sustained_wind"], 13.0)
            self.assertEqual(result["flags"][0]["flag"], "RAIN_RISK")

    def test_weather_for_game_reports_unavailable_with_no_snapshots(self):
        from sports_aggregator.nfl.weather import weather_for_game
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            result = weather_for_game(repository, "no-such-game")
            self.assertFalse(result["available"])
            self.assertEqual(result["snapshots"], 0)


class RankingTests(unittest.TestCase):
    def test_ties_share_a_rank_and_the_next_distinct_value_skips_ahead(self):
        rows = [
            {"id": "a", "yards": 100}, {"id": "b", "yards": 100},
            {"id": "c", "yards": 90}, {"id": "d", "yards": 80},
        ]
        ranks = rank_within(rows, id_key="id", value_key="yards")
        self.assertEqual(ranks["a"], {"rank": 1, "of": 4})
        self.assertEqual(ranks["b"], {"rank": 1, "of": 4})
        # Standard competition ranking: two people tied for 1st means the
        # next distinct value is 3rd, not 2nd.
        self.assertEqual(ranks["c"], {"rank": 3, "of": 4})
        self.assertEqual(ranks["d"], {"rank": 4, "of": 4})

    def test_lower_is_better_reverses_the_ordering(self):
        rows = [{"id": "a", "int": 5}, {"id": "b", "int": 1}, {"id": "c", "int": 3}]
        ranks = rank_within(rows, id_key="id", value_key="int", lower_is_better=True)
        self.assertEqual(ranks["b"]["rank"], 1)
        self.assertEqual(ranks["c"]["rank"], 2)
        self.assertEqual(ranks["a"]["rank"], 3)

    def test_rows_missing_the_metric_are_excluded_from_rank_and_count(self):
        rows = [{"id": "a", "yards": 100}, {"id": "b", "yards": None}, {"id": "c"}]
        ranks = rank_within(rows, id_key="id", value_key="yards")
        self.assertEqual(ranks, {"a": {"rank": 1, "of": 1}})

    def test_rank_lookup_computes_each_metric_against_its_own_ordering(self):
        rows = [
            {"id": "a", "yards": 100, "interceptions": 2},
            {"id": "b", "yards": 50, "interceptions": 0},
        ]
        result = rank_lookup(rows, id_key="id", metrics=("yards", "interceptions"),
                             lower_is_better=frozenset({"interceptions"}))
        self.assertEqual(result["yards"]["a"]["rank"], 1)
        # Fewer interceptions is better, so "b" (0 picks) ranks ahead of "a".
        self.assertEqual(result["interceptions"]["b"]["rank"], 1)
        self.assertEqual(result["interceptions"]["a"]["rank"], 2)


class _FakePlayerRankRepository:
    """Duck-typed stand-in exposing only what _headline_stats_with_rank calls."""

    def __init__(self, rows_by_position: dict[str, list[dict]]):
        self._rows_by_position = rows_by_position

    def player_season_stats(self, _season, _metrics, *, position=None, **_kwargs):
        return self._rows_by_position.get(position, [])


class HeadlinePlayerRankTests(unittest.TestCase):
    def test_headline_stats_carry_rank_among_the_same_position(self):
        repository = _FakePlayerRankRepository({"QB": [
            {"player_id": "qb1", "passing_yards": 4000, "passing_tds": 30},
            {"player_id": "qb2", "passing_yards": 3000, "passing_tds": 20},
        ]})
        totals = {"passing_yards": 4000, "passing_tds": 30, "completions": 0, "attempts": 0,
                  "passing_interceptions": 0, "rushing_yards": 0}
        stats = _headline_stats_with_rank(repository, 2026, "QB", "qb1", totals)
        by_label = {stat["label"]: stat for stat in stats}
        self.assertEqual(by_label["Pass yards"]["rank"], 1)
        self.assertEqual(by_label["Pass yards"]["of"], 2)

    def test_headline_stats_have_no_rank_fields_without_a_peer_match(self):
        repository = _FakePlayerRankRepository({})
        totals = {"passing_yards": 4000, "completions": 0, "attempts": 0,
                  "passing_tds": 0, "passing_interceptions": 0, "rushing_yards": 0}
        stats = _headline_stats_with_rank(repository, 2026, "QB", "qb1", totals)
        for stat in stats:
            self.assertNotIn("rank", stat)


class NFLRefreshCliTests(unittest.TestCase):
    """refresh_cli.py used to do `from app import create_app` for every
    segment, booting the entire combined app (every CFB blueprint, pandas,
    numpy) before any segment-specific work started. Live production
    testing traced nfl-core/nfl-content's "OpenBLAS error: Memory
    allocation still failed" directly to that import cost colliding with
    the refresh child's memory ceiling -- both failed in well under a
    second, at a resident-memory peak far below the ceiling. The rewrite's
    entire point is that importing this module stays light; that property
    is exactly what would silently regress if a future change reintroduced
    a module-level `from app import ...` or similar."""

    def test_module_import_does_not_load_the_full_app_or_pandas(self):
        root = Path(__file__).resolve().parents[1]
        script = (
            "import sys; "
            "import sports_aggregator.nfl.refresh_cli; "
            "print('app' in sys.modules); "
            "print('sports_aggregator.cfb.web' in sys.modules); "
            "print('sports_aggregator.nfl.web' in sys.modules); "
            "print('pandas' in sys.modules); "
            "print('numpy' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["False"] * 5)

    def test_module_import_forces_blas_thread_limit_env_vars(self):
        """Regression test for the real production cause: a live Render
        check found OPENBLAS_NUM_THREADS/OMP_NUM_THREADS/MKL_NUM_THREADS all
        unset despite render.yaml configuring them, letting OpenBLAS size
        its thread pool off the container's host-visible CPU count (16)
        instead of the plan's real share -- enough virtual address space
        to fail nfl-core instantly, well under its memory ceiling. This
        module must force them itself rather than trust propagation."""
        keys = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
        root = Path(__file__).resolve().parents[1]
        script = (
            "import os; "
            + "; ".join(f"os.environ.pop('{key}', None)" for key in keys) + "; "
            "import sports_aggregator.nfl.refresh_cli; "
            + "; ".join(f"print(os.environ.get('{key}'))" for key in keys)
        )
        env = {key: value for key, value in os.environ.items() if key not in keys}
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(root), env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["1"] * len(keys))

    def test_module_import_does_not_override_an_explicitly_configured_thread_count(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["OPENBLAS_NUM_THREADS"] = "4"
        result = subprocess.run(
            [sys.executable, "-c",
             "import os; import sports_aggregator.nfl.refresh_cli; print(os.environ['OPENBLAS_NUM_THREADS'])"],
            capture_output=True, text=True, cwd=str(root), env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4")

    def test_main_dispatches_each_segment_with_the_right_arguments(self):
        from sports_aggregator.nfl import refresh_cli, sync as sync_module
        calls = []

        def record_core(season, **kwargs):
            calls.append(("core", season, kwargs))
            return True

        with patch.object(refresh_cli, "_sync_rosters", lambda season, **k: calls.append(("rosters", season))), \
             patch.object(refresh_cli, "_sync_core", record_core), \
             patch.object(refresh_cli, "_sync_content", lambda season: calls.append(("content", season))), \
             patch.object(refresh_cli, "_sync_pff", lambda season, **k: calls.append(("pff", season))), \
             patch.object(refresh_cli, "_sync_history", lambda start, end, **k: calls.append(("history", start, end))), \
             patch.object(refresh_cli, "_sync_weather", lambda season, **k: calls.append(("weather", season))):
            for segment in ("rosters", "core-foundation", "core-stats", "core-depth", "core-pbp",
                           "content", "pff", "history", "weather"):
                self.assertEqual(refresh_cli.main([segment, "--season", "2026"]), 0)

        self.assertIn(("rosters", 2026), calls)
        self.assertIn(("content", 2026), calls)
        # pff and history both look back to the completed prior season.
        self.assertIn(("pff", 2025), calls)
        self.assertIn(("history", 2010, 2025), calls)
        self.assertIn(("weather", 2026), calls)
        core_calls = {call[0]: call[2] for call in calls if call[0] == "core"}
        # Only one "core" entry in `calls` since each segment overwrites the
        # key -- rebuild per-segment by re-running the dispatch logic below
        # instead, against the real group constants.
        core_kwargs_by_group = []
        with patch.object(refresh_cli, "_sync_core", lambda season, **k: (core_kwargs_by_group.append(k), True)[1]):
            for segment, expected_group, expected_pbp, expected_extras in (
                ("core-foundation", sync_module.CORE_FOUNDATION, False, True),
                ("core-stats", sync_module.CORE_STATS, False, False),
                ("core-depth", sync_module.CORE_DEPTH, False, False),
                ("core-pbp", sync_module.CORE_PBP, True, False),
            ):
                core_kwargs_by_group.clear()
                self.assertEqual(refresh_cli.main([segment, "--season", "2026"]), 0)
                kwargs = core_kwargs_by_group[0]
                self.assertEqual(kwargs["only"], expected_group)
                self.assertEqual(kwargs["include_pbp"], expected_pbp)
                self.assertEqual(kwargs["include_extras"], expected_extras)

    def test_main_returns_failure_exit_code_when_core_sync_reports_failure(self):
        from sports_aggregator.nfl import refresh_cli
        with patch.object(refresh_cli, "_sync_core", lambda season, **k: False):
            self.assertEqual(refresh_cli.main(["core-foundation", "--season", "2026"]), 1)

    def test_main_returns_failure_exit_code_on_unexpected_exception(self):
        from sports_aggregator.nfl import refresh_cli
        with patch.object(refresh_cli, "_sync_weather", side_effect=RuntimeError("boom")):
            self.assertEqual(refresh_cli.main(["weather", "--season", "2026"]), 1)


if __name__ == "__main__":
    unittest.main()
