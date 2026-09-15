"""Turn the curated NFL RSS + community workbook into fetchable feeds.

`NFL_RSS_and_Community_Directory.xlsx` bundles three ingestible sheets --
national/specialist RSS feeds, one subreddit+PFF+blog trio per team, and a
handful of cross-team subreddits -- alongside an "Overview" sheet that is
just narrative summary, not data. This module only shapes those three sheets
into `FeedConfig`s; `NFLContentRepository.ingest_rss_feeds()` (content.py) is
what actually fetches and stores the resulting articles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sports_aggregator.models import FeedConfig
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.source_directory import NFLSourceDirectoryError, _sheet_rows


DEFAULT_PATH = Path("data/nfl/NFL_RSS_and_Community_Directory.xlsx")


class NFLRSSDirectoryError(NFLSourceDirectoryError):
    pass


def _rows(path: str | Path, sheet: str, header_marker: str) -> Iterator[dict[str, str]]:
    """Yield each data row as a dict, keyed off the header row containing `header_marker`.

    Sheets in this workbook open with title/summary rows before the real
    header, so the header can't just be "the first non-empty row" -- it has
    to be identified by a column name unique to it.
    """
    rows = _sheet_rows(path, sheet=sheet)
    header_index = next((index for index, row in enumerate(rows) if header_marker in row), None)
    if header_index is None:
        raise NFLRSSDirectoryError(f"{sheet!r} sheet has no {header_marker!r} header")
    headers = rows[header_index]
    key_column = headers[0]
    for values in rows[header_index + 1:]:
        row = {header: values[index] if index < len(values) else ""
               for index, header in enumerate(headers) if header}
        if row.get(key_column, "").strip():
            yield row


def _feed(name: str, url: str, *, source_type: str, reliability: int, key_prefix: str) -> FeedConfig:
    if not url.startswith("https://"):
        raise NFLRSSDirectoryError(f"feed {name!r} has no HTTPS URL: {url!r}")
    return FeedConfig(
        name=name, url=url, max_articles=30, source_type=source_type, reliability=reliability,
        source_entity_key=f"{key_prefix}:{name}", source_endpoint_key=f"rss:{url}",
    )


def national_feeds(path: str | Path = DEFAULT_PATH) -> tuple[FeedConfig, ...]:
    """League-wide feeds: national/specialist RSS plus cross-team subreddits."""
    feeds: dict[str, FeedConfig] = {}
    for row in _rows(path, "National RSS", "RSS URL"):
        reliability = 4 if row["Priority"].strip().casefold() == "tier 1" else 3
        source_type = "reddit" if "reddit.com" in row["RSS URL"].casefold() else "news"
        feed = _feed(row["Feed"].strip(), row["RSS URL"].strip(), source_type=source_type,
                    reliability=reliability, key_prefix="nfl-national-rss")
        feeds.setdefault(feed.url, feed)
    for row in _rows(path, "Other Communities", "RSS URL"):
        feed = _feed(row["Community"].strip(), row["RSS URL"].strip(), source_type="reddit",
                    reliability=2, key_prefix="nfl-community")
        feeds.setdefault(feed.url, feed)
    return tuple(feeds.values())


def team_feeds(path: str | Path = DEFAULT_PATH) -> dict[str, tuple[FeedConfig, ...]]:
    """Per-team subreddit, PFF, and blog feeds, keyed by canonical team code."""
    grouped: dict[str, list[FeedConfig]] = {}
    for row in _rows(path, "Team Sources", "Subreddit RSS"):
        team = canon_team(row["Abbr"].strip())
        candidates = (
            (row["Subreddit"].strip(), row["Subreddit RSS"].strip(), "reddit", 2),
            (f"{row['Team'].strip()} (PFF)", row["PFF Team RSS"].strip(), "news", 3),
            (row["Team blog"].strip(), row["Blog RSS"].strip(), "news", 3),
        )
        feeds = [_feed(name, url, source_type=source_type, reliability=reliability,
                       key_prefix=f"nfl-team-rss:{team}")
                for name, url, source_type, reliability in candidates if url]
        grouped.setdefault(team, []).extend(feeds)
    return {team: tuple(feeds) for team, feeds in grouped.items()}
