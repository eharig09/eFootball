from datetime import datetime, timezone
import os
import tempfile
import unittest
from unittest.mock import patch

from app import _legacy_dashboards_default, create_app
from sports_aggregator.catalog import get_league
from sports_aggregator.models import Article
from sports_aggregator.nfl.models import Game, Player, Team
from sports_aggregator.service import AggregationResult


class StubService:
    def __init__(self, result):
        self.result = result

    def aggregate(self, league):
        return AggregationResult(
            league=league,
            articles=self.result.articles,
            errors=self.result.errors,
            fetched_at=self.result.fetched_at,
        )


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        league = get_league("college-football")
        assert league is not None
        result = AggregationResult(
            league=league,
            articles=(Article(
                title="Opening weekend preview", url="https://example.com/preview",
                source="Example Sports",
                published_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
            ),),
            errors=(),
            fetched_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )
        self.app = create_app({
            "TESTING": True,
            "REGISTER_LEGACY_DASHBOARDS": False,
            "LEAGUE_AGGREGATION_SERVICE": StubService(result),
            "CFB_DATABASE_PATH": os.path.join(self.temp_dir.name, "cfb.sqlite3"),
            "NFL_DATABASE_PATH": os.path.join(self.temp_dir.name, "nfl.sqlite3"),
            "CFB_REFRESH_TOKEN": "test-refresh-token",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_home_and_college_football_page(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"College Football", home.data)

        page = self.client.get("/leagues/college-football/", follow_redirects=True)
        self.assertEqual(page.status_code, 200)
        # Assert on the page's structure rather than its copy, which is edited often.
        self.assertIn(b"College Football Today", page.data)
        self.assertIn(b"Source Streams", page.data)

    def test_render_defaults_to_lightweight_cfb_runtime(self):
        with patch.dict(os.environ, {"RENDER": "true"}, clear=True):
            self.assertFalse(_legacy_dashboards_default())
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(_legacy_dashboards_default())

    def test_api_discovery_payload_and_limit(self):
        discovery = self.client.get("/api/v1/leagues")
        self.assertEqual(discovery.status_code, 200)
        self.assertEqual(discovery.get_json()["leagues"][0]["slug"], "college-football")
        self.assertIn("nfl", {item["slug"] for item in discovery.get_json()["leagues"]})

        response = self.client.get("/api/v1/leagues/college-football/articles?limit=1")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["articles"][0]["source"], "Example Sports")

    def test_nfl_has_stable_html_and_api_routes(self):
        generic = self.client.get("/leagues/nfl/")
        self.assertEqual(generic.status_code, 302)
        self.assertTrue(generic.headers["Location"].endswith("/nfl/"))

        page = self.client.get("/nfl/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"NFL news and analytics", page.data)

        dashboard = self.client.get("/api/v1/nfl")
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.get_json()["counts"]["teams"], 0)
        self.assertEqual(dashboard.get_json()["identity_coverage"], [])
        discovery = self.client.get("/api/v1/leagues").get_json()
        nfl = next(item for item in discovery["leagues"] if item["slug"] == "nfl")
        self.assertEqual(nfl["url"], "/nfl/")
        self.assertEqual(self.client.get("/nfl/teams/ZZZ/").status_code, 404)

        response = self.client.get("/api/v1/nfl/articles?limit=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["league"]["slug"], "nfl")
        self.assertEqual(response.get_json()["count"], 1)

    def test_nfl_data_status_page_and_nav_pill_render(self):
        page = self.client.get("/nfl/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'class="data-pill', page.data)
        self.assertIn(b'href="/nfl/data-status/', page.data)

        status = self.client.get("/nfl/data-status/")
        self.assertEqual(status.status_code, 200)
        self.assertIn(b"NFL data status", status.data)
        self.assertIn(b"Production seed", status.data)
        self.assertIn(b"Ingestion runs", status.data)
        # Regression check: `content_counts.items` in a Jinja template calls
        # dict.items() (a real attribute) instead of the "items" key --
        # bracket access is required. An empty database with zero stored
        # items must render "0", not a Python method repr.
        self.assertIn(b"<b>0</b><small>Stored content items</small>", status.data)

        api = self.client.get("/api/v1/nfl/data-status")
        self.assertEqual(api.status_code, 200)
        payload = api.get_json()
        self.assertIn("counts", payload)
        self.assertIn("runs_table", payload)
        self.assertIsInstance(payload["runs_table"]["rows"], list)

    def test_nfl_surface_loads_explicit_dark_theme(self):
        page = self.client.get("/nfl/")
        self.assertIn(b"/static/nfl.css", page.data)
        self.assertIn(b"/static/nfl_components.css", page.data)
        stylesheet = self.client.get("/static/nfl.css")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(b"color-scheme:dark", stylesheet.data)
        self.assertIn(b"html{background:#0d141b", stylesheet.data)
        self.assertIn(b"input,select,button,textarea", stylesheet.data)

    def test_nfl_game_and_player_pages_follow_canonical_links(self):
        repository = self.app.extensions["nfl_repository"]
        repository.initialize()
        repository.replace_teams((
            Team("GNB", "Green Bay Packers", "Packers", "NFC", "NFC North", "#203731", None, None),
            Team("CHI", "Chicago Bears", "Bears", "NFC", "NFC North", "#0B162A", None, None),
        ))
        repository.replace_games(2025, (Game(
            "2025_01_GNB_CHI", 2025, "REG", 1, "2025-09-07", "13:00",
            "GNB", "CHI", 24, 17, False, True, "Soldier Field", "outdoors",
            "grass", 72, 8, 1.5, 44.5,
        ),))
        repository.replace_players(2025, (Player(
            2025, "00-001", "GNB", "Sample Player", "Sample", "Player", "QB", "QB",
            10, "ACT", None, 74, 220, "Example State", 2, None, None, None, None,
        ),))
        repository.replace_weekly_stats(2025, ({
            "season": 2025, "week": 1, "season_type": "REG",
            "game_id": "2025_01_GNB_CHI", "player_id": "00-001",
            "player_display_name": "Sample Player", "team": "GNB",
            "opponent_team": "CHI", "position": "QB", "attempts": 30,
            "passing_yards": 250, "passing_tds": 2,
        },))
        repository.replace_game_efficiency(2025, (
            {"game_id": "2025_01_GNB_CHI", "week": 1, "posteam": "GNB",
             "defteam": "CHI", "epa": 0.4, "qb_epa": 0.4, "success": 1,
             "pass": 1, "rush": 0, "down": 1, "yards_gained": 12},
            {"game_id": "2025_01_GNB_CHI", "week": 1, "posteam": "CHI",
             "defteam": "GNB", "epa": -0.1, "qb_epa": -0.1, "success": 0,
             "pass": 1, "rush": 0, "down": 2, "yards_gained": 4},
        ))

        game = self.client.get("/nfl/games/2025_01_GNB_CHI/")
        self.assertEqual(game.status_code, 200)
        self.assertIn(b"Player box score", game.data)
        self.assertIn(b"Game efficiency", game.data)
        self.assertIn(b"Unit matchups", game.data)
        self.assertIn(b"Production comparison", game.data)
        self.assertIn(b"Situation", game.data)
        self.assertIn(b"Prior meetings", game.data)
        self.assertIn(b"Player box score \xe2\x80\x94 Passing", game.data)
        self.assertIn(b"Sample Player", game.data)
        game_payload = self.client.get("/api/v1/nfl/games/2025_01_GNB_CHI").get_json()
        self.assertEqual(game_payload["game"]["away_score"], 24)
        self.assertEqual(game_payload["unit_cards"][0]["offense"], "GNB")
        self.assertEqual(game_payload["stat_groups"][0]["label"], "Passing")
        self.assertEqual(game_payload["records"]["GNB"]["record"], "0-0")
        self.assertEqual(game_payload["history"]["meetings"], 0)
        self.assertEqual(len(game_payload["situation"]), 5)

        player = self.client.get("/nfl/players/00-001/?season=2025")
        self.assertEqual(player.status_code, 200)
        self.assertIn(b"Season production", player.data)
        payload = self.client.get("/api/v1/nfl/players/00-001?season=2025").get_json()
        self.assertEqual(payload["player"]["full_name"], "Sample Player")
        self.assertEqual(payload["game_log"]["rows"][0]["passing_yards"], 250)
        self.assertEqual(payload["game_log_groups"][0]["label"], "Passing")
        self.assertIn(b"Passing game log", player.data)
        team = self.client.get("/nfl/teams/GNB/?season=2025")
        self.assertIn(b"EPA / play", team.data)
        self.assertIn(b"Arrivals and departures", team.data)
        self.assertIn(b"news and social", team.data)
        self.assertIn(b"--team-primary:#203731", team.data)
        self.assertIn(b"1-0", team.data)
        dashboard = self.client.get("/api/v1/nfl?season=2025").get_json()
        self.assertEqual(dashboard["matchups"][0]["sides"][0]["record"], "1-0")
        north = next(group for group in dashboard["standings"] if group["division"] == "NFC North")
        green_bay = next(row for row in north["table"]["rows"] if row["team"] == "GNB")
        self.assertEqual(green_bay["record"], "1-0")
        self.assertEqual(self.client.get("/nfl/games/not-real/").status_code, 404)
        self.assertEqual(self.client.get("/nfl/players/not-real/?season=2025").status_code, 404)

    def test_unknown_league_returns_404(self):
        self.assertEqual(self.client.get("/leagues/not-a-league/").status_code, 404)
        self.assertEqual(
            self.client.get("/api/v1/leagues/not-a-league/articles").status_code, 404,
        )

    def test_source_graph_routes(self):
        response = self.client.get("/api/v1/cfb/source-entities")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["entity_count"], 0)
        self.assertEqual(self.client.get("/college-football/admin/source-graph/").status_code, 200)
        content = self.client.get("/api/v1/cfb/content")
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.get_json()["count"], 0)

    @patch("app.subprocess.Popen")
    def test_render_refresh_hook_requires_token_and_starts_background_job(self, popen):
        self.assertEqual(self.client.post("/internal/cfb-refresh").status_code, 401)
        response = self.client.post(
            "/internal/cfb-refresh",
            headers={"Authorization": "Bearer test-refresh-token"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "accepted")
        command = popen.call_args.args[0]
        # The hook spawns the tracked driver, which calls
        # `run_scheduled_refresh` itself; what matters here is that the request
        # started a real refresh in the background rather than doing it inline.
        self.assertIn("sports_aggregator.tracked_refresh", command)
        self.assertIn("--profile", command)


if __name__ == "__main__":
    unittest.main()
