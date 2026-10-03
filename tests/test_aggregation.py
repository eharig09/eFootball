from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from sports_aggregator.catalog import get_league
from sports_aggregator.models import Article, FeedConfig
from sports_aggregator.providers.rss import RSSNewsProvider
from sports_aggregator.service import AggregationService


class FakeProvider:
    def __init__(self, name, articles=None, error=None):
        self.name = name
        self.articles = articles or []
        self.error = error
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.articles


class AggregationServiceTests(unittest.TestCase):
    def setUp(self):
        self.league = get_league("college-football")
        assert self.league is not None

    def test_max_workers_bounds_how_many_feeds_are_fetched_at_once(self):
        import threading
        import time
        lock, state = threading.Lock(), {"now": 0, "peak": 0}

        class Slow(FakeProvider):
            def fetch(self):
                with lock:
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                time.sleep(0.05)
                with lock:
                    state["now"] -= 1
                return []

        for workers, expected_peak in ((2, 2), (1, 1)):
            state.update(now=0, peak=0)
            service = AggregationService({self.league.slug: tuple(Slow(str(i)) for i in range(6))}, max_workers=workers)
            service.aggregate(self.league, force_refresh=True)
            self.assertLessEqual(state["peak"], expected_peak)

    def test_deduplicates_sorts_and_isolates_provider_failures(self):
        older = Article(
            title="Older story", url="https://example.com/older", source="One",
            published_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        newer = Article(
            title="Newer story", url="https://example.com/newer#comments", source="One",
            published_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
        duplicate = Article(
            title="Duplicate headline", url="https://example.com/newer", source="Two",
            published_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        service = AggregationService(
            {self.league.slug: (
                FakeProvider("One", [older, newer]),
                FakeProvider("Broken", error=RuntimeError("source unavailable")),
                FakeProvider("Two", [duplicate]),
            )}
        )

        result = service.aggregate(self.league)

        self.assertEqual([article.title for article in result.articles], ["Newer story", "Older story"])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].source, "Broken")

    def test_caches_a_league_result(self):
        clock_value = [100.0]
        provider = FakeProvider(
            "One", [Article(title="Story", url="https://example.com/story", source="One")]
        )
        service = AggregationService(
            {self.league.slug: (provider,)}, cache_ttl_seconds=10,
            clock=lambda: clock_value[0],
        )

        first = service.aggregate(self.league)
        second = service.aggregate(self.league)
        self.assertIs(first, second)
        self.assertEqual(provider.calls, 1)

        clock_value[0] = 111.0
        third = service.aggregate(self.league)
        self.assertIsNot(first, third)
        self.assertEqual(provider.calls, 2)


class RSSProviderTests(unittest.TestCase):
    def test_normalizes_feed_entries_and_skips_invalid_urls(self):
        parsed_feed = SimpleNamespace(
            bozo=False,
            entries=[
                {
                    "title": "A &amp; B", "link": "https://example.com/story",
                    "author": "Reporter", "summary": "<p>Summary text</p>",
                    "published_parsed": (2026, 8, 23, 12, 30, 0, 0, 0, 0),
                },
                {"title": "Bad URL", "link": "javascript:alert(1)"},
            ],
        )
        provider = RSSNewsProvider(
            FeedConfig(name="Example", url="https://example.com/feed"),
            parser=lambda _: parsed_feed,
        )

        articles = provider.fetch()

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "A & B")
        self.assertEqual(articles[0].summary, "Summary text")
        self.assertEqual(articles[0].published_at.tzinfo, timezone.utc)
        self.assertEqual(articles[0].publisher, "Example")
        self.assertEqual(articles[0].discovered_via, "RSS")


if __name__ == "__main__":
    unittest.main()
