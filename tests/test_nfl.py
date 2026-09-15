from datetime import date, datetime, timezone
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sports_aggregator.catalog import get_league
from sports_aggregator.models import Article
from sports_aggregator.nfl.content import NFLContentRepository
from sports_aggregator.nfl.models import Game, Player, Team
from sports_aggregator.nfl.data_sources import free_sources, load_data_sources
from sports_aggregator.nfl.espn import injury_rows, staff_rows
from sports_aggregator.nfl.naming import canon_position, canon_team, normalize_name
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.source_directory import import_directory, load_directory
from sports_aggregator.nfl.sync import NFLDataSync
from sports_aggregator.nfl.teams import unit_continuity
from sports_aggregator.nfl.nflverse import NflverseClient, NflverseError, current_season
from sports_aggregator.nfl.charts import player_charts, team_charts
from sports_aggregator.nfl.explorer import scatter_plot
from sports_aggregator.nfl.passing import pass_matchup_packet, pass_zone_packet
from sports_aggregator.nfl.personnel import _score
from sports_aggregator.nfl.postgame import postgame_packet
from sports_aggregator.nfl.production_seed import LOCK_NAME, STATE_NAME, maybe_launch
from sports_aggregator.nfl.search import search_entities as search_nfl_entities
from sports_aggregator.nfl.views import current_games, schedule_table
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
                "teams": 1, "games": 1, "players": 1, "weekly_metrics": 4,
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
                self.assertEqual(
                    {row[0] for row in connection.execute("SELECT metric FROM player_weekly_stats")},
                    {"attempts", "passing_yards", "targets", "carries"},
                )

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
                 "interception": 0, "cpoe": -12.0},
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


if __name__ == "__main__":
    unittest.main()
