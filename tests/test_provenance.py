"""Provenance shown beside the reporting, and the page furniture around it.

The clustering, role, and relevance pipelines already recorded why each story is
grouped, who filed it, and in what capacity. Until now that evidence lived only
in the JSON APIs and on one dashboard panel, which meant the most defensible
thing about the product was also the least visible part of it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from app import create_app
from sports_aggregator.models import Article
from sports_aggregator.cfb.models import Team
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.social.content import ContentRepository
from sports_aggregator.social.stories import StoryRepository


class StoryProvenanceTests(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams([Team.from_cfbd({
            "id": 68, "school": "Boise State", "mascot": "Broncos",
            "abbreviation": "BSU", "alternateNames": [],
            "conference": "Mountain West", "classification": "fbs",
            "color": "#0033A0", "logos": []})])
        self.content = ContentRepository(self.path)
        self.content.initialize()
        StoryRepository(self.path).initialize()
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        os.unlink(self.path)

    @staticmethod
    def _article(title, publisher, url, *, when=None):
        when = when or datetime.now(timezone.utc).isoformat()
        return Article(
            title=title, url=url, source=publisher, publisher=publisher,
            original_url=url, summary="Boise State practice report.",
            published_at=datetime.fromisoformat(when),
            source_entity_key=f"publisher:{publisher.lower().replace(' ', '-')}",
            source_endpoint_key=f"rss:{publisher.lower().replace(' ', '-')}",
            team_ids=(68,),
        )

    def _seed_story(self):
        """One ingested article, clustered by the real pipeline."""
        self.content.store_article(
            self._article("Starter returns to practice", "Idaho Statesman",
                          "https://publisher.test/starter-returns"), 2026)
        return StoryRepository(self.path).rebuild()

    def test_a_story_list_discloses_how_it_was_grouped_and_who_filed_it(self):
        self._seed_story()
        body = self.client.get("/college-football/teams/68/").get_data(as_text=True)
        self.assertIn("Why this is here", body)
        self.assertIn("single item", body)
        self.assertIn("Idaho Statesman", body)

    def test_the_disclosure_names_the_role_rather_than_the_stored_code(self):
        self._seed_story()
        body = self.client.get("/college-football/teams/68/").get_data(as_text=True)
        self.assertNotIn("CORROBORATION_CANDIDATE", body)
        self.assertIn("corroboration candidate", body)
        self.assertIn("(first)", body)

    def test_the_disclosure_costs_nothing_until_it_is_opened(self):
        """It is a <details>, so it must not ship expanded."""
        self._seed_story()
        body = self.client.get("/college-football/teams/68/").get_data(as_text=True)
        self.assertIn('<details class="provenance">', body)
        self.assertNotIn('<details class="provenance" open>', body)

    def test_a_story_that_never_moved_does_not_claim_it_was_updated(self):
        self._seed_story()
        body = self.client.get("/college-football/teams/68/").get_data(as_text=True)
        provenance = body[body.index("Why this is here"):]
        self.assertIn("first reported", provenance[:900])
        self.assertNotIn("last updated", provenance[:900])
        self.assertIn("1 source", body)
        self.assertNotIn("2 sources", body)


class DisclosureRenderingTests(unittest.TestCase):
    """The macro itself, against stories the clustering pipeline can produce.

    A genuine multi-source cluster needs two platform posts resolving to one
    external article plus the full tagging pipeline, which is more machinery
    than this presentation change warrants. The macro is exercised directly
    instead, with the packet shape `list_stories` returns.
    """

    def setUp(self):
        self.app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})

    def _render(self, story):
        template = self.app.jinja_env.from_string(
            "{% from '_tables.html' import story_provenance %}"
            "{{ story_provenance(story) }}")
        with self.app.test_request_context("/college-football/"):
            return template.render(story=story)

    @staticmethod
    def _story(**overrides):
        story = {
            "cluster_basis": "SHARED_ARTICLE",
            "confidence": 0.9,
            "first_reported_at": "2026-08-20T14:00:00+00:00",
            "last_updated_at": "2026-08-21T09:00:00+00:00",
            "sources": [
                {"source_display_name": "Idaho Statesman",
                 "source_role": "ORIGINAL_REPORT", "is_primary": 1},
                {"source_display_name": "Bronco Nation News",
                 "source_role": "CORROBORATION_CANDIDATE", "is_primary": 0},
            ],
        }
        story.update(overrides)
        return story

    def test_every_contributing_source_is_named_with_its_role(self):
        html = self._render(self._story())
        self.assertIn("Idaho Statesman", html)
        self.assertIn("Bronco Nation News", html)
        self.assertIn("shared article", html)
        self.assertIn("0.90 confidence", html)

    def test_the_source_that_filed_first_is_marked(self):
        html = self._render(self._story())
        self.assertIn("(first)", html)
        self.assertEqual(html.count("(first)"), 1)

    def test_a_story_that_moved_reports_both_timestamps(self):
        html = self._render(self._story())
        self.assertIn("first reported", html)
        self.assertIn("last updated", html)

    def test_a_long_source_list_is_summarised_rather_than_dumped(self):
        sources = [{"source_display_name": f"Outlet {index}",
                    "source_role": "CORROBORATION_CANDIDATE", "is_primary": 0}
                   for index in range(10)]
        html = self._render(self._story(sources=sources))
        self.assertIn("and 4 more", html)
        self.assertNotIn("Outlet 9", html)

    def test_an_unattributed_source_is_labelled_rather_than_blank(self):
        html = self._render(self._story(sources=[
            {"source_display_name": "", "publisher_name": "",
             "source_role": "UNCLASSIFIED", "is_primary": 0}]))
        self.assertIn("Unattributed", html)

    def test_a_story_with_no_sources_still_reports_how_it_was_grouped(self):
        html = self._render(self._story(sources=[]))
        self.assertIn("shared article", html)
        self.assertNotIn("Who reported it", html)


class PageFurnitureTests(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        repository = CFBRepository(self.path)
        repository.replace_teams([Team.from_cfbd({
            "id": 68, "school": "Boise State", "mascot": "Broncos",
            "abbreviation": "BSU", "alternateNames": [],
            "conference": "Mountain West", "classification": "fbs",
            "color": "#0033A0", "logos": []})])
        ContentRepository(self.path).initialize()
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": repository, "CFB_DEFAULT_SEASON": 2026,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        os.unlink(self.path)

    def test_the_manifest_is_served_and_is_valid_json(self):
        response = self.client.get("/static/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        manifest = json.loads(response.get_data(as_text=True))
        self.assertEqual(manifest["start_url"], "/college-football/")
        self.assertTrue(manifest["icons"])

    def test_the_icon_referenced_by_the_manifest_exists(self):
        response = self.client.get("/static/manifest.webmanifest")
        manifest = json.loads(response.get_data(as_text=True))
        for icon in manifest["icons"]:
            with self.subTest(icon=icon["src"]):
                self.assertEqual(self.client.get(icon["src"]).status_code, 200)

    def test_pages_link_the_manifest_and_an_icon(self):
        head = self.client.get("/college-football/").get_data(as_text=True)
        self.assertIn('rel="manifest"', head)
        self.assertIn('rel="icon"', head)

    def test_table_tablists_are_labelled(self):
        """An unlabelled tablist is announced only as "tab list"."""
        body = self.client.get("/college-football/teams/68/").get_data(as_text=True)
        if 'role="tablist"' in body:
            for fragment in body.split('role="tablist"')[1:]:
                self.assertIn("aria-label", fragment[:120])

    def test_table_tabs_support_arrow_key_navigation(self):
        """Roving tabindex without arrow keys traps a keyboard reader."""
        body = self.client.get("/college-football/teams/68/").get_data(as_text=True)
        self.assertIn("ArrowRight", body)
        self.assertIn("ArrowLeft", body)
        self.assertIn("'Home'", body)
        self.assertIn("'End'", body)


if __name__ == "__main__":
    unittest.main()
