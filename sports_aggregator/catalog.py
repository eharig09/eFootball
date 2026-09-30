"""Declarative league catalog.

Adding a league starts here. Web routes and API discovery read this catalog, while
provider implementations remain independent of Flask.
"""

from __future__ import annotations

from types import MappingProxyType

from sports_aggregator.models import FeedConfig, LeagueConfig


_LEAGUES = MappingProxyType(
    {
        "college-football": LeagueConfig(
            slug="college-football",
            name="College Football",
            sport="Football",
            abbreviation="CFB",
            description=(
                "National college-football headlines through a normalized, "
                "provider-independent feed. Team and conference views can build on this base."
            ),
            accent_color="#1473e6",
            feeds=(
                FeedConfig(
                    name="ESPN",
                    url="https://www.espn.com/espn/rss/ncf/news",
                    max_articles=40,
                    source_type="national_reporting",
                    reliability=4,
                    source_entity_key="organization:espn",
                    source_endpoint_key="rss:https://www.espn.com/espn/rss/ncf/news",
                ),
                FeedConfig(
                    name="NCAA.com FBS",
                    url="https://www.ncaa.com/news/football/fbs/rss.xml",
                    max_articles=40,
                    source_type="official_reporting",
                    reliability=5,
                    source_entity_key="organization:ncaa",
                    source_endpoint_key="rss:https://www.ncaa.com/news/football/fbs/rss.xml",
                ),
                FeedConfig(
                    name="Yahoo Sports CFB",
                    url="https://sports.yahoo.com/college-football/rss/",
                    max_articles=40,
                    source_type="national_reporting",
                    reliability=4,
                    source_entity_key="organization:yahoo-sports",
                    source_endpoint_key="rss:https://sports.yahoo.com/college-football/rss/",
                ),
                FeedConfig(
                    name="CFBNumbers",
                    url="https://cfbnumbers.substack.com/feed",
                    max_articles=20,
                    source_type="analysis",
                    reliability=4,
                    source_entity_key="publication:cfbnumbers",
                    source_endpoint_key="rss:https://cfbnumbers.substack.com/feed",
                ),
                FeedConfig(
                    name="CFB Graphs",
                    url="https://cfbgraphs.substack.com/feed",
                    max_articles=20,
                    source_type="analysis",
                    reliability=4,
                    source_entity_key="publication:cfbgraphs",
                    source_endpoint_key="rss:https://cfbgraphs.substack.com/feed",
                ),
                FeedConfig(
                    name="The Mintner Method",
                    url="https://themintnermethod.substack.com/feed",
                    max_articles=20,
                    source_type="analysis",
                    reliability=3,
                    source_entity_key="publication:mintner-method",
                    source_endpoint_key="rss:https://themintnermethod.substack.com/feed",
                ),
                FeedConfig(
                    name="The Spade",
                    url="https://thespade.substack.com/feed",
                    max_articles=20,
                    source_type="analysis",
                    reliability=4,
                    source_entity_key="publication:the-spade",
                    source_endpoint_key="rss:https://thespade.substack.com/feed",
                    topic_terms=('college football', 'cfb', 'ncaa', 'transfer portal', 'cfp', 'fbs'),
                ),
                FeedConfig(
                    name="Bless Your Chart",
                    url="https://blessyourchart.substack.com/feed",
                    max_articles=20,
                    source_type="analysis",
                    reliability=4,
                    source_entity_key="publication:blessyourchart",
                    source_endpoint_key="rss:https://blessyourchart.substack.com/feed",
                    topic_terms=('college football', 'cfb', 'ncaa', 'fbs'),
                ),
                FeedConfig(
                    name="Bets and Reps",
                    url="https://betsandreps.substack.com/feed",
                    max_articles=20,
                    source_type="analysis",
                    reliability=3,
                    source_entity_key="publication:betsandreps",
                    source_endpoint_key="rss:https://betsandreps.substack.com/feed",
                ),
                FeedConfig(
                    name="Neil Paine",
                    url="https://neilpaine.substack.com/feed",
                    max_articles=20,
                    source_type="analysis",
                    reliability=4,
                    source_entity_key="publication:neilpaine",
                    source_endpoint_key="rss:https://neilpaine.substack.com/feed",
                    topic_terms=('college football', 'cfb'),
                ),
            ),
        ),
        "nfl": LeagueConfig(
            slug="nfl",
            name="National Football League",
            sport="Football",
            abbreviation="NFL",
            description=(
                "League-wide NFL reporting, with schedules, rosters, player statistics, "
                "and analytics being added on top of the shared news platform."
            ),
            accent_color="#d50a0a",
            feeds=(
                FeedConfig(
                    name="ESPN",
                    url="https://www.espn.com/espn/rss/nfl/news",
                    max_articles=40,
                    source_type="national_reporting",
                    reliability=4,
                    source_entity_key="organization:espn",
                    source_endpoint_key="rss:https://www.espn.com/espn/rss/nfl/news",
                ),
            ),
        ),
    }
)


def list_leagues() -> tuple[LeagueConfig, ...]:
    return tuple(_LEAGUES.values())


def get_league(slug: str) -> LeagueConfig | None:
    return _LEAGUES.get(slug)
