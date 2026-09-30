"""Normalized models shared by every league and news provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Article:
    """A provider-neutral article returned to web and API clients."""

    title: str
    url: str
    source: str
    published_at: datetime | None = None
    author: str = ""
    summary: str = ""
    source_type: str = "news"
    reliability: int = 3
    publisher: str = ""
    original_url: str = ""
    discovered_via: str = ""
    content_kind: str = "REPORTING"
    source_entity_key: str = ""
    source_endpoint_key: str = ""
    discovery_endpoint_key: str = ""
    team_ids: tuple[int, ...] = field(default_factory=tuple)
    player_ids: tuple[str, ...] = field(default_factory=tuple)
    game_ids: tuple[int, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("article title cannot be empty")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"article URL must be HTTP(S): {self.url!r}")
        if not self.source.strip():
            raise ValueError("article source cannot be empty")
        if not 1 <= self.reliability <= 5:
            raise ValueError("article reliability must be between 1 and 5")
        object.__setattr__(self, "published_at", _utc(self.published_at))

    @property
    def identity(self) -> str:
        """Stable key used to remove the same story returned by multiple feeds."""
        parsed = urlsplit(self.original_url or self.url)
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
        )

    @property
    def published_label(self) -> str:
        if self.published_at is None:
            return "Date unavailable"
        return self.published_at.strftime("%b %d, %Y · %I:%M %p UTC")

    @property
    def source_icon(self) -> str:
        kind = self.source_type.casefold()
        if kind in {"youtube", "video"}:
            return "▶️"
        if kind in {"podcast", "audio"}:
            return "🎧"
        if kind in {"reddit", "community"}:
            return "💬"
        if kind in {"bluesky", "social"}:
            return "🦋"
        return "📰"

    @property
    def source_label(self) -> str:
        return {"youtube": "Video", "video": "Video", "podcast": "Podcast",
                "audio": "Podcast", "reddit": "Reddit", "community": "Community",
                "bluesky": "Bluesky", "social": "Social post"}.get(
                    self.source_type.casefold(), "Article")

    @property
    def makes_sound(self) -> bool:
        return self.source_type.casefold() in {"youtube", "video", "podcast", "audio"}

    @property
    def published_relative(self) -> str:
        if self.published_at is None:
            return "undated"
        hours = max(0, (datetime.now(timezone.utc) - self.published_at).total_seconds() / 3600)
        if hours < 1:
            return "just now"
        if hours < 24:
            return f"{int(hours)}h ago"
        if hours < 24 * 7:
            return f"{int(hours // 24)}d ago"
        return self.published_at.strftime("%b %d")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "author": self.author,
            "summary": self.summary,
            "source_type": self.source_type,
            "source_icon": self.source_icon,
            "source_label": self.source_label,
            "makes_sound": self.makes_sound,
            "published_label": self.published_label,
            "published_relative": self.published_relative,
            "reliability": self.reliability,
            "publisher": self.publisher or self.source,
            "original_url": self.original_url or self.url,
            "discovered_via": self.discovered_via,
            "content_kind": self.content_kind,
            "source_entity_key": self.source_entity_key,
            "source_endpoint_key": self.source_endpoint_key,
            "discovery_endpoint_key": self.discovery_endpoint_key,
            "team_ids": list(self.team_ids),
            "player_ids": list(self.player_ids),
            "game_ids": list(self.game_ids),
        }


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """Configuration for an RSS/Atom news source."""

    name: str
    url: str
    max_articles: int = 30
    source_type: str = "rss"
    reliability: int = 3
    source_entity_key: str = ""
    source_endpoint_key: str = ""
    # Multi-sport or multi-level publications: keep only entries whose title or
    # summary mentions one of these terms (case-insensitive, whole word).
    topic_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LeagueConfig:
    """Metadata and sources needed to add a league to the application."""

    slug: str
    name: str
    sport: str
    abbreviation: str
    description: str
    accent_color: str
    feeds: tuple[FeedConfig, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.slug or self.slug.strip("abcdefghijklmnopqrstuvwxyz0123456789-"):
            raise ValueError("league slug must contain lowercase letters, numbers, and hyphens")
