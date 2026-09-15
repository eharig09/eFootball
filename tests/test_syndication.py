"""Sharing cards, structured data, outbound feeds, and crawler surfaces.

These pages assemble material that exists nowhere else. None of it travels
unless a pasted link renders as something, so the metadata is treated as part of
the product rather than as decoration.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

from app import create_app
from sports_aggregator.cfb.meta import (
    conference_meta, game_meta, person_ld, player_meta, sports_event_ld,
    sports_team_ld, team_meta, today_meta,
)
from sports_aggregator.cfb.models import Game, Player, Team
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.syndication import (
    robots, rss_feed, sitemap, story_items,
)
from sports_aggregator.social.content import ContentRepository


class DescriptionTests(unittest.TestCase):
    """Descriptions come from stored data, and never over-promise."""

    def test_a_game_card_names_the_matchup_and_the_window(self):
        meta = game_meta(
            {"game_id": 7, "away_team": "Boise State", "home_team": "San Diego State",
             "start_label": "Saturday 3:30 PM ET", "venue": "Snapdragon Stadium"},
            {"school": "Boise State"}, {"school": "San Diego State"})
        self.assertEqual(meta["title"], "Boise State at San Diego State | Game Preview")
        self.assertIn("Boise State at San Diego State", meta["description"])
        self.assertIn("Saturday 3:30 PM ET", meta["description"])
        self.assertIn("Snapdragon Stadium", meta["description"])
        self.assertEqual(meta["path"], "/college-football/games/7/")

    def test_a_game_card_claims_only_the_layers_that_populated(self):
        game = {"game_id": 7, "away_team": "A", "home_team": "B"}
        bare = game_meta(game, {}, {})
        self.assertNotIn("kickoff forecast", bare["description"])
        # An absent forecast arrives as a populated dict, not as None, so a
        # plain truthiness check would claim a forecast on every game.
        unavailable = game_meta(game, {}, {},
                                weather={"available": False, "snapshots": 0, "flags": []})
        self.assertNotIn("kickoff forecast", unavailable["description"])
        self.assertNotIn("attributed reporting", bare["description"])

        full = game_meta(game, {}, {},
                         weather={"available": True, "temperature": 54}, story_count=3)
        self.assertIn("kickoff forecast", full["description"])
        self.assertIn("attributed reporting", full["description"])

    def test_a_neutral_site_game_is_not_described_as_a_home_game(self):
        meta = game_meta(
            {"game_id": 9, "away_team": "A", "home_team": "B", "neutral_site": True},
            {}, {})
        self.assertIn("A vs B", meta["title"])
        self.assertNotIn("A at B", meta["title"])

    def test_a_team_card_carries_record_conference_and_next_opponent(self):
        meta = team_meta(
            {"team_id": 68, "school": "Boise State", "conference": "Mountain West"},
            {"school": "Boise State", "logo": "https://example.test/bsu.png"}, 2026,
            record={"wins": 9, "losses": 2},
            next_game={"home_team_id": 68, "away_team": "San Diego State"})
        self.assertIn("9-2", meta["description"])
        self.assertIn("Mountain West", meta["description"])
        self.assertIn("Next: vs San Diego State", meta["description"])
        self.assertEqual(meta["image"], "https://example.test/bsu.png")

    def test_a_road_game_is_described_as_away(self):
        meta = team_meta(
            {"team_id": 68, "school": "Boise State"}, {}, 2026,
            next_game={"home_team_id": 21, "home_team": "San Diego State"})
        self.assertIn("Next: at San Diego State", meta["description"])

    def test_a_team_with_no_record_still_gets_a_usable_description(self):
        meta = team_meta({"team_id": 68, "school": "Boise State"}, {}, 2026)
        self.assertIn("Boise State 2026", meta["description"])
        self.assertNotIn("None", meta["description"])

    def test_a_player_card_leads_with_position_school_and_class(self):
        meta = player_meta(
            {"player_id": "p1", "name": "Test Player", "position": "QB", "year": 3},
            {"school": "Boise State"})
        self.assertIn("Test Player", meta["description"])
        self.assertIn("QB", meta["description"])
        self.assertIn("Boise State", meta["description"])

    def test_descriptions_are_trimmed_at_a_word_boundary(self):
        meta = conference_meta({"conference": "M" * 400, "slug": "m"}, 2026)
        self.assertLessEqual(len(meta["description"]), 201)
        self.assertTrue(meta["description"].endswith("…"))
        self.assertNotIn("  ", meta["description"])

    def test_the_dashboard_card_reports_real_counts_only(self):
        empty = today_meta(2026)
        self.assertNotIn("0 games", empty["description"])
        populated = today_meta(2026, game_count=12, story_count=30)
        self.assertIn("12 games", populated["description"])
        self.assertIn("30 clustered stories", populated["description"])

    def test_counts_agree_with_their_nouns(self):
        one = today_meta(2026, game_count=1, story_count=1)
        self.assertIn("1 game worth watching", one["description"])
        self.assertIn("1 clustered story", one["description"])
        self.assertNotIn("1 games", one["description"])
        self.assertIn("1 upcoming game.",
                      conference_meta({"conference": "MW", "slug": "mw"}, 2026,
                                      game_count=1)["description"])


class StructuredDataTests(unittest.TestCase):
    """Only fields the repository actually owns are emitted."""

    def test_a_sports_event_carries_teams_start_and_venue(self):
        payload = sports_event_ld(
            {"game_id": 7, "away_team": "Boise State", "home_team": "San Diego State",
             "start_date": "2026-09-12T19:30:00+00:00", "venue": "Snapdragon Stadium",
             "venue_city": "San Diego", "venue_state": "CA"},
            {"school": "Boise State"}, {"school": "San Diego State"})
        self.assertEqual(payload["@type"], "SportsEvent")
        self.assertEqual(payload["awayTeam"]["name"], "Boise State")
        self.assertEqual(payload["homeTeam"]["name"], "San Diego State")
        self.assertEqual(payload["startDate"], "2026-09-12T19:30:00+00:00")
        self.assertEqual(payload["location"]["address"]["addressLocality"], "San Diego")

    def test_absent_fields_are_omitted_rather_than_emitted_as_null(self):
        payload = sports_event_ld({"away_team": "A", "home_team": "B"}, {}, {})
        self.assertNotIn("startDate", payload)
        self.assertNotIn("location", payload)

        team = sports_team_ld({"school": "A"}, {})
        self.assertNotIn("logo", team)
        self.assertNotIn("memberOf", team)

        person = person_ld({"name": "Test Player"}, {})
        self.assertNotIn("jobTitle", person)
        self.assertNotIn("memberOf", person)


class FeedTests(unittest.TestCase):
    """Outbound feeds must credit the publisher and link to the original."""

    def _story(self, **overrides):
        story = {"title": "Starter returns to practice",
                 "url": "https://publisher.test/story",
                 "published_at": "2026-08-20T14:00:00+00:00",
                 "source_name": "Idaho Statesman",
                 "sources": [{"author_name": "A Reporter"}, {}]}
        story.update(overrides)
        return story

    def test_a_feed_item_links_to_the_publisher_not_to_this_site(self):
        items = story_items([self._story()])
        self.assertEqual(items[0]["link"], "https://publisher.test/story")
        self.assertEqual(items[0]["source"], "Idaho Statesman")

    def test_a_story_with_no_original_url_is_dropped_rather_than_relinked(self):
        self.assertEqual(story_items([self._story(url=None)]), [])

    def test_the_description_credits_the_source_and_counts_corroboration(self):
        items = story_items([self._story()])
        self.assertIn("Reported by Idaho Statesman.", items[0]["description"])
        self.assertIn("1 corroborating source.", items[0]["description"])

    def test_the_feed_parses_as_rss_with_a_self_link(self):
        body = rss_feed(
            title="College Football Reporting", description="Test feed",
            link="https://example.test/college-football/",
            self_url="https://example.test/college-football/feed.xml",
            items=story_items([self._story()]))
        root = ElementTree.fromstring(body)
        self.assertEqual(root.tag, "rss")
        channel = root.find("channel")
        self.assertEqual(channel.find("title").text, "College Football Reporting")
        self.assertIsNotNone(
            channel.find("{http://www.w3.org/2005/Atom}link"))
        item = channel.find("item")
        self.assertEqual(item.find("link").text, "https://publisher.test/story")
        # Stored timestamps are ISO-8601; RSS requires RFC-822.
        self.assertEqual(item.find("pubDate").text,
                         "Thu, 20 Aug 2026 14:00:00 +0000")

    def test_titles_containing_markup_characters_do_not_break_the_document(self):
        body = rss_feed(
            title="Feed", description="d", link="https://example.test/",
            self_url="https://example.test/feed.xml",
            items=story_items([self._story(title="Coach: <QB> & the \"plan\"")]))
        root = ElementTree.fromstring(body)
        self.assertIn("<QB> &", root.find("channel/item/title").text)

    def test_an_unparseable_timestamp_omits_the_date_rather_than_failing(self):
        body = rss_feed(
            title="Feed", description="d", link="https://example.test/",
            self_url="https://example.test/feed.xml",
            items=story_items([self._story(published_at="not-a-date")]))
        root = ElementTree.fromstring(body)
        self.assertIsNone(root.find("channel/item/pubDate"))


class SitemapTests(unittest.TestCase):

    def test_the_sitemap_parses_and_carries_locations(self):
        body = sitemap([
            {"loc": "https://example.test/college-football/", "priority": 1.0},
            {"loc": "https://example.test/college-football/games/7/",
             "lastmod": "2026-09-12T19:30:00+00:00"},
        ])
        root = ElementTree.fromstring(body)
        namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        locations = [node.text for node in root.iter(f"{namespace}loc")]
        self.assertIn("https://example.test/college-football/games/7/", locations)
        self.assertEqual([node.text for node in root.iter(f"{namespace}lastmod")],
                         ["2026-09-12"])

    def test_an_entry_without_a_location_is_skipped(self):
        root = ElementTree.fromstring(sitemap([{"lastmod": "2026-01-01"}]))
        self.assertEqual(len(list(root)), 0)

    def test_robots_keeps_crawlers_out_of_admin_and_api(self):
        body = robots("https://example.test/sitemap.xml")
        self.assertIn("Disallow: /college-football/admin/", body)
        self.assertIn("Disallow: /api/", body)
        self.assertIn("Sitemap: https://example.test/sitemap.xml", body)


class ServedMetadataTests(unittest.TestCase):
    """The rendered pages and endpoints, end to end."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams([Team.from_cfbd(payload) for payload in (
            {"id": 68, "school": "Boise State", "mascot": "Broncos", "abbreviation": "BSU",
             "alternateNames": [], "conference": "Mountain West", "classification": "fbs",
             "color": "#0033A0", "logos": ["https://example.test/bsu.png"]},
            {"id": 21, "school": "San Diego State", "mascot": "Aztecs", "abbreviation": "SDSU",
             "alternateNames": [], "conference": "Mountain West", "classification": "fbs",
             "color": "#A6192E", "logos": []},
        )])
        self.repository.replace_games(2026, [Game.from_cfbd({
            "id": 401, "season": 2026, "week": 3, "seasonType": "regular",
            "startDate": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(), "startTimeTBD": False,
            "completed": False, "neutralSite": False, "conferenceGame": True,
            "venue": "Snapdragon Stadium", "venueId": 1,
            "homeId": 21, "homeTeam": "San Diego State", "homeConference": "Mountain West",
            "homePoints": None, "awayId": 68, "awayTeam": "Boise State",
            "awayConference": "Mountain West", "awayPoints": None,
        })])
        self.repository.replace_players(2026, (
            Player("p1", 2026, "Test", "Player", "Boise State", "QB", 1, 74, 200, 3),
        ))
        ContentRepository(self.path).initialize()
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        os.unlink(self.path)

    def _head(self, path: str) -> str:
        body = self.client.get(path).get_data(as_text=True)
        return body.split("</head>", 1)[0]

    def test_every_page_kind_carries_a_description_and_a_canonical_url(self):
        for path in ("/college-football/",
                     "/college-football/teams/68/",
                     "/college-football/players/p1/",
                     "/college-football/games/401/",
                     "/college-football/conferences/mountain-west/"):
            with self.subTest(path=path):
                head = self._head(path)
                self.assertIn('<meta name="description"', head, path)
                self.assertIn('rel="canonical"', head, path)
                self.assertIn('property="og:title"', head, path)
                self.assertIn('name="twitter:card"', head, path)
                # A card that fell back to the generic blurb on a detail page
                # means the packet never reached the template.
                self.assertNotIn('content=""', head, path)

    def test_a_game_page_emits_parseable_sports_event_json_ld(self):
        head = self._head("/college-football/games/401/")
        match = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', head, re.S)
        self.assertIsNotNone(match, "game page emitted no structured data")
        payload = json.loads(match.group(1))
        self.assertEqual(payload["@type"], "SportsEvent")
        self.assertEqual(payload["homeTeam"]["name"], "San Diego State")

    def test_the_canonical_url_drops_query_parameters(self):
        head = self._head("/college-football/teams/68/?stats_mode=total")
        canonical = re.search(r'rel="canonical" href="([^"]+)"', head).group(1)
        self.assertTrue(canonical.endswith("/college-football/teams/68/"), canonical)

    def test_the_team_card_uses_the_team_logo(self):
        head = self._head("/college-football/teams/68/")
        self.assertIn('property="og:image" content="https://example.test/bsu.png"', head)

    def test_the_national_feed_is_served_as_rss(self):
        response = self.client.get("/college-football/feed.xml")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/rss+xml", response.headers["Content-Type"])
        ElementTree.fromstring(response.get_data(as_text=True))

    def test_a_team_feed_is_served_and_discoverable_from_the_page(self):
        response = self.client.get("/college-football/teams/68/feed.xml")
        self.assertEqual(response.status_code, 200)
        ElementTree.fromstring(response.get_data(as_text=True))
        self.assertEqual(
            self.client.get("/college-football/teams/999999/feed.xml").status_code, 404)

    def test_the_sitemap_lists_teams_and_scheduled_games(self):
        body = self.client.get("/sitemap.xml").get_data(as_text=True)
        root = ElementTree.fromstring(body)
        namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        locations = [node.text for node in root.iter(f"{namespace}loc")]
        self.assertTrue(any(loc.endswith("/college-football/teams/68/")
                            for loc in locations), locations)
        self.assertTrue(any(loc.endswith("/college-football/games/401/")
                            for loc in locations), locations)
        # Admin and API surfaces are not advertised.
        self.assertFalse(any("/admin/" in loc or "/api/" in loc for loc in locations))

    def test_robots_is_served_and_points_at_the_sitemap(self):
        body = self.client.get("/robots.txt").get_data(as_text=True)
        self.assertIn("Sitemap:", body)
        self.assertIn("/sitemap.xml", body)

    def test_pages_advertise_the_national_feed(self):
        head = self._head("/college-football/")
        self.assertIn('type="application/rss+xml"', head)


if __name__ == "__main__":
    unittest.main()


class EndpointHealthTests(unittest.TestCase):
    """Every registered GET route must respond, and every API must serialize.

    A packet that renders fine in a template can still be unserializable — a
    dict keyed by tuples is the case that actually shipped, and it took down
    /api/v1/cfb/games/<id>/preview while every HTML page stayed green.
    """

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams([Team.from_cfbd(payload) for payload in (
            {"id": 68, "school": "Boise State", "mascot": "Broncos", "abbreviation": "BSU",
             "alternateNames": [], "conference": "Mountain West", "classification": "fbs",
             "color": "#0033A0", "logos": ["https://example.test/bsu.png"]},
            {"id": 21, "school": "San Diego State", "mascot": "Aztecs", "abbreviation": "SDSU",
             "alternateNames": [], "conference": "Mountain West", "classification": "fbs",
             "color": "#A6192E", "logos": []},
        )])
        self.repository.replace_games(2026, [Game.from_cfbd({
            "id": 401, "season": 2026, "week": 3, "seasonType": "regular",
            "startDate": "2026-09-12T19:30:00.000Z", "startTimeTBD": False,
            "completed": False, "neutralSite": False, "conferenceGame": True,
            "venue": "Snapdragon Stadium", "venueId": 1,
            "homeId": 21, "homeTeam": "San Diego State", "homeConference": "Mountain West",
            "homePoints": None, "awayId": 68, "awayTeam": "Boise State",
            "awayConference": "Mountain West", "awayPoints": None,
        })])
        self.repository.replace_players(2026, (
            Player("p1", 2026, "Test", "Player", "Boise State", "QB", 1, 74, 200, 3),
        ))
        # A completed prior meeting with a box score, so the preview endpoint
        # actually builds its prior-player-games packet. Without stored box
        # rows that packet is empty and the sweep proves nothing about it.
        self.repository.replace_games(2025, [Game.from_cfbd({
            "id": 390, "season": 2025, "week": 5, "seasonType": "regular",
            "startDate": "2025-10-04T19:30:00.000Z", "startTimeTBD": False,
            "completed": True, "neutralSite": False, "conferenceGame": True,
            "venue": "Albertsons Stadium", "venueId": 2,
            "homeId": 68, "homeTeam": "Boise State", "homeConference": "Mountain West",
            "homePoints": 31, "awayId": 21, "awayTeam": "San Diego State",
            "awayConference": "Mountain West", "awayPoints": 17,
        })])
        self.repository.store_game_player_box_scores(({
            "id": 390, "teams": [{"team": "Boise State", "conference": "Mountain West",
                "homeAway": "home", "points": 31, "categories": [{
                    "name": "passing", "types": [{"name": "YDS", "athletes": [
                        {"id": "p1", "name": "Test Player", "stat": "268"}]}]}]}],
        },))
        ContentRepository(self.path).initialize()
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        os.unlink(self.path)

    #: Endpoints that legitimately refuse without configuration.
    EXPECTED_NON_200 = {"/internal/cfb-refresh-status",
                        "/college-football/data-status/log-tail"}

    def _routes(self):
        samples = {"<int:game_id>": "401", "<int:team_id>": "68",
                   "<player_id>": "p1", "<slug>": "mountain-west",
                   "<path:filename>": "cfb.css", "<league_slug>": "college-football"}
        for rule in sorted(self.app.url_map.iter_rules(), key=str):
            if "GET" not in rule.methods:
                continue
            path = str(rule)
            for token, value in samples.items():
                path = path.replace(token, value)
            if "<" in path:
                continue
            yield path

    def test_every_get_route_responds_without_a_server_error(self):
        for path in self._routes():
            if path in self.EXPECTED_NON_200:
                continue
            with self.subTest(path=path):
                self.assertLess(self.client.get(path).status_code, 500)

    def test_every_json_api_serializes(self):
        for path in self._routes():
            if not path.startswith("/api/"):
                continue
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertLess(response.status_code, 500)
                if response.status_code == 200:
                    json.loads(response.get_data(as_text=True))
