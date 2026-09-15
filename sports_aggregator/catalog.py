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
