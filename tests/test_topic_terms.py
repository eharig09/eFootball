import unittest

from sports_aggregator.models import FeedConfig
from sports_aggregator.providers.rss import RSSNewsProvider
from sports_aggregator.catalog import get_league


def _entry(title):
    return {"title": title, "link": f"https://example.com/{title[:5]}", "summary": ""}


class TopicTermsTests(unittest.TestCase):
    def test_multi_sport_feed_keeps_only_on_topic_entries(self):
        feed = {"entries": [_entry("Why the NFL EPA model changed"), _entry("NBA finals recap")]}
        config = FeedConfig(name="Paine", url="https://example.com/f", topic_terms=("nfl",))
        articles = RSSNewsProvider(config, parser=lambda _u: feed).fetch()
        self.assertEqual([a.title for a in articles], ["Why the NFL EPA model changed"])

    def test_no_terms_keeps_everything(self):
        feed = {"entries": [_entry("NBA finals recap")]}
        config = FeedConfig(name="X", url="https://example.com/f")
        self.assertEqual(len(RSSNewsProvider(config, parser=lambda _u: feed).fetch()), 1)

    def test_cfb_catalog_registers_analysis_substacks(self):
        feeds = {f.name: f for f in get_league("college-football").feeds}
        self.assertEqual(feeds["CFBNumbers"].source_type, "analysis")
        self.assertTrue(feeds["The Spade"].topic_terms)


if __name__ == "__main__":
    unittest.main()
