"""Platform-neutral source entities, endpoints, expertise, and relationships."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from sports_aggregator.social.models import (
    EndpointResolution,
    SourceEndpointProfile,
    SourceEntityProfile,
)
from sports_aggregator.social.media_catalog import catalog_sources, proposed_classes


UNIFIED_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_entities (
 source_entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
 entity_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, organization TEXT,
 entity_type TEXT NOT NULL, reliability_score INTEGER NOT NULL,
 reporting_score INTEGER NOT NULL, team_access_score INTEGER NOT NULL,
 national_score INTEGER NOT NULL, analytics_score INTEGER NOT NULL,
 scheme_score INTEGER NOT NULL, recruiting_score INTEGER NOT NULL,
 transfer_score INTEGER NOT NULL, draft_score INTEGER NOT NULL,
 awards_score INTEGER NOT NULL, g5_score INTEGER NOT NULL,
 breaking_score INTEGER NOT NULL, official_score INTEGER NOT NULL,
 priority INTEGER NOT NULL, trust_status TEXT NOT NULL,
 active INTEGER NOT NULL, last_verified_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_entity_classes (
 source_entity_id INTEGER NOT NULL, source_class TEXT NOT NULL,
 PRIMARY KEY(source_entity_id,source_class),
 FOREIGN KEY(source_entity_id) REFERENCES source_entities(source_entity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_entity_specialties (
 source_entity_id INTEGER NOT NULL, specialty TEXT NOT NULL,
 PRIMARY KEY(source_entity_id,specialty),
 FOREIGN KEY(source_entity_id) REFERENCES source_entities(source_entity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_entity_teams (
 source_entity_id INTEGER NOT NULL, team TEXT NOT NULL, expertise REAL NOT NULL DEFAULT 1.0,
 PRIMARY KEY(source_entity_id,team),
 FOREIGN KEY(source_entity_id) REFERENCES source_entities(source_entity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_entity_conferences (
 source_entity_id INTEGER NOT NULL, conference TEXT NOT NULL, expertise REAL NOT NULL DEFAULT 1.0,
 PRIMARY KEY(source_entity_id,conference),
 FOREIGN KEY(source_entity_id) REFERENCES source_entities(source_entity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_endpoints (
 endpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_entity_id INTEGER NOT NULL, endpoint_key TEXT NOT NULL UNIQUE,
 platform TEXT NOT NULL, endpoint_type TEXT NOT NULL, handle TEXT,
 platform_id TEXT, url TEXT, active INTEGER NOT NULL,
 verification_status TEXT NOT NULL, verified_at TEXT, last_checked_at TEXT,
 last_success_at TEXT, last_error TEXT, display_name TEXT, description TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL,
 FOREIGN KEY(source_entity_id) REFERENCES source_entities(source_entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_endpoints_platform_id ON source_endpoints(platform,platform_id);
CREATE INDEX IF NOT EXISTS idx_source_endpoints_entity ON source_endpoints(source_entity_id);
CREATE TABLE IF NOT EXISTS reddit_communities (
 endpoint_id INTEGER PRIMARY KEY, community_type TEXT NOT NULL,
 quality_score INTEGER NOT NULL, activity_score REAL,
 reporting_authority INTEGER NOT NULL DEFAULT 1,
 original_source_extraction INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(endpoint_id) REFERENCES source_endpoints(endpoint_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_relationships (
 relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
 from_entity_id INTEGER NOT NULL, to_entity_id INTEGER NOT NULL,
 relationship_type TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
 verified_at TEXT, UNIQUE(from_entity_id,to_entity_id,relationship_type),
 FOREIGN KEY(from_entity_id) REFERENCES source_entities(source_entity_id) ON DELETE CASCADE,
 FOREIGN KEY(to_entity_id) REFERENCES source_entities(source_entity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_candidates (
 candidate_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
 proposed_entity_type TEXT NOT NULL, proposed_classes TEXT NOT NULL,
 discovery_method TEXT NOT NULL, validation_status TEXT NOT NULL DEFAULT 'SOURCE_CANDIDATE',
 validation_score REAL, validation_notes TEXT NOT NULL DEFAULT '',
 last_checked_at TEXT, UNIQUE(name,discovery_method)
);
CREATE TABLE IF NOT EXISTS media_source_profiles (
 candidate_id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
 team TEXT, conference TEXT, coverage TEXT,
 platform_json TEXT NOT NULL DEFAULT '[]', tags_json TEXT NOT NULL DEFAULT '[]',
 priority INTEGER NOT NULL DEFAULT 3, youtube_url TEXT, podcast_url TEXT,
 podcast_page_url TEXT, website TEXT, active_status TEXT,
 last_verified_active TEXT, subscriber_count INTEGER, episode_frequency TEXT,
 original_reporting INTEGER, program_access INTEGER, reporting_evidence TEXT,
 content_focus TEXT, notes TEXT, catalog_status TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 FOREIGN KEY(candidate_id) REFERENCES source_candidates(candidate_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS model_sources (
 model_source_id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_entity_id INTEGER, model_name TEXT NOT NULL, provider TEXT NOT NULL,
 metric_type TEXT NOT NULL, update_frequency TEXT,
 methodology_url TEXT, current INTEGER NOT NULL DEFAULT 1,
 UNIQUE(model_name,provider),
 FOREIGN KEY(source_entity_id) REFERENCES source_entities(source_entity_id) ON DELETE SET NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def entity_key(profile: SourceEntityProfile) -> str:
    return profile.entity_key or f"{profile.entity_type.casefold()}:{_key(profile.name)}"


def endpoint_key(profile: SourceEndpointProfile) -> str:
    if profile.endpoint_key:
        return profile.endpoint_key
    identity = profile.platform_id or profile.handle or profile.url
    if not identity:
        raise ValueError("source endpoint requires an endpoint key or stable identity")
    return f"{profile.platform.casefold()}:{identity.casefold()}"


class UnifiedSourceRegistry:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(UNIFIED_SCHEMA)

    def upsert_entity(self, profile: SourceEntityProfile) -> int:
        self.initialize(); now = _now(); key = entity_key(profile)
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO source_entities VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(entity_key) DO UPDATE SET name=excluded.name,
                   organization=excluded.organization,entity_type=excluded.entity_type,
                   reliability_score=excluded.reliability_score,reporting_score=excluded.reporting_score,
                   team_access_score=excluded.team_access_score,national_score=excluded.national_score,
                   analytics_score=excluded.analytics_score,scheme_score=excluded.scheme_score,
                   recruiting_score=excluded.recruiting_score,transfer_score=excluded.transfer_score,
                   draft_score=excluded.draft_score,awards_score=excluded.awards_score,
                   g5_score=excluded.g5_score,breaking_score=excluded.breaking_score,
                   official_score=excluded.official_score,priority=excluded.priority,
                   trust_status=excluded.trust_status,active=excluded.active,updated_at=excluded.updated_at""",
                (key, profile.name, profile.organization, profile.entity_type,
                 profile.reliability_score, profile.reporting_score, profile.team_access_score,
                 profile.national_score, profile.analytics_score, profile.scheme_score,
                 profile.recruiting_score, profile.transfer_score, profile.draft_score,
                 profile.awards_score, profile.g5_score, profile.breaking_score,
                 profile.official_score, profile.priority, profile.trust_status,
                 int(profile.active), None, now),
            )
            source_entity_id = connection.execute(
                "SELECT source_entity_id FROM source_entities WHERE entity_key=?", (key,)
            ).fetchone()[0]
            for table in ("source_entity_classes", "source_entity_specialties",
                          "source_entity_teams", "source_entity_conferences"):
                connection.execute(f"DELETE FROM {table} WHERE source_entity_id=?", (source_entity_id,))
            connection.executemany("INSERT INTO source_entity_classes VALUES(?,?)",
                                   [(source_entity_id, item) for item in profile.source_classes])
            connection.executemany("INSERT INTO source_entity_specialties VALUES(?,?)",
                                   [(source_entity_id, item) for item in profile.specialties])
            connection.executemany("INSERT INTO source_entity_teams VALUES(?,?,1.0)",
                                   [(source_entity_id, item) for item in profile.teams])
            connection.executemany("INSERT INTO source_entity_conferences VALUES(?,?,1.0)",
                                   [(source_entity_id, item) for item in profile.conferences])
            connection.commit()
        return source_entity_id

    def upsert_endpoint(self, source_entity_id: int, profile: SourceEndpointProfile) -> int:
        self.initialize(); now = _now(); key = endpoint_key(profile)
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO source_endpoints(source_entity_id,endpoint_key,platform,endpoint_type,
                   handle,platform_id,url,active,verification_status,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(endpoint_key) DO UPDATE SET
                   source_entity_id=excluded.source_entity_id,platform=excluded.platform,
                   endpoint_type=excluded.endpoint_type,handle=excluded.handle,
                   platform_id=COALESCE(excluded.platform_id,source_endpoints.platform_id),
                   url=COALESCE(excluded.url,source_endpoints.url),active=excluded.active,
                   verification_status=CASE WHEN source_endpoints.verification_status='verified'
                     AND excluded.verification_status='seeded_unverified'
                     THEN source_endpoints.verification_status ELSE excluded.verification_status END,
                   updated_at=excluded.updated_at""",
                (source_entity_id, key, profile.platform.casefold(), profile.endpoint_type,
                 profile.handle, profile.platform_id, profile.url, int(profile.active),
                 profile.verification_status, now),
            )
            endpoint_id = connection.execute(
                "SELECT endpoint_id FROM source_endpoints WHERE endpoint_key=?", (key,)
            ).fetchone()[0]
            connection.commit()
        return endpoint_id

    def store_endpoint_resolution(self, result: EndpointResolution) -> None:
        self.initialize(); now = _now()
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE source_endpoints SET platform_id=CASE WHEN ?='verified'
                   THEN COALESCE(?,platform_id) ELSE platform_id END,
                   url=CASE WHEN ?='verified' THEN COALESCE(?,url) ELSE url END,
                   verification_status=?,verified_at=CASE WHEN ?='verified' THEN ? ELSE verified_at END,
                   last_checked_at=?,last_success_at=CASE WHEN ?='verified' THEN ? ELSE last_success_at END,
                   last_error=CASE WHEN ?='verified' THEN NULL ELSE ? END,
                   display_name=CASE WHEN ?='verified' THEN COALESCE(?,display_name) ELSE display_name END,
                   description=CASE WHEN ?='verified' THEN ? ELSE description END,updated_at=?
                   WHERE endpoint_key=?""",
                (result.status, result.platform_id, result.status, result.resolved_url,
                 result.status, result.status, now, now, result.status, now,
                 result.status, result.description, result.status, result.display_name,
                 result.status, result.description, now, result.endpoint_key),
            )
            if result.activity_score is not None:
                connection.execute(
                    "UPDATE reddit_communities SET activity_score=? WHERE endpoint_id=(SELECT endpoint_id FROM source_endpoints WHERE endpoint_key=?)",
                    (result.activity_score, result.endpoint_key),
                )
            connection.commit()

    def seed_reddit_communities(self) -> int:
        seeds = (
            ("r/CFB", "GENERAL_CFB", ("college_football", "story_discovery", "community_reaction"), 4),
            ("r/CFBAnalysis", "ANALYTICS", ("analytics", "original_analysis", "statistics"), 4),
            ("r/NFL_Draft", "DRAFT", ("NFL_Draft", "community_scouting", "prospects"), 4),
        )
        for name, community_type, specialties, quality in seeds:
            subreddit = name.removeprefix("r/")
            entity_id = self.upsert_entity(SourceEntityProfile(
                name=name, organization="Reddit", entity_type="COMMUNITY",
                source_classes=("COMMUNITY",), specialties=specialties,
                reliability_score=2, reporting_score=1, analytics_score=quality,
                draft_score=quality if community_type == "DRAFT" else 1,
                priority=3, trust_status="TRUSTED_COMMUNITY",
            ))
            endpoint_id = self.upsert_endpoint(entity_id, SourceEndpointProfile(
                platform="reddit", endpoint_type="SUBREDDIT", handle=subreddit,
                platform_id=name, url=f"https://www.reddit.com/r/{subreddit}/",
                endpoint_key=f"reddit:subreddit:{subreddit.casefold()}",
            ))
            with closing(self._connect()) as connection:
                connection.execute(
                    """INSERT INTO reddit_communities(endpoint_id,community_type,quality_score,activity_score)
                       VALUES(?,?,?,?)
                       ON CONFLICT(endpoint_id) DO UPDATE SET community_type=excluded.community_type,
                       quality_score=excluded.quality_score""",
                    (endpoint_id, community_type, quality, None),
                )
                connection.commit()
        return len(seeds)

    def seed_team_reddit_communities(self, entries: Iterable[dict]) -> int:
        """Promote audited team communities into the graph ingestion uses.

        The original team registry lived in ``team_subreddits`` while content
        ingestion queried ``source_endpoints``. Keeping the two disconnected
        made 47 verified communities invisible despite a healthy poll plan.
        """
        self.initialize()
        with closing(self._connect()) as connection:
            teams = {row["team_id"]: dict(row) for row in connection.execute(
                "SELECT team_id,school,conference FROM teams")}
        promoted = 0
        for entry in entries:
            team = teams.get(int(entry["team_id"]))
            if not team:
                continue
            subreddit = str(entry["subreddit"]).removeprefix("r/").strip()
            if not subreddit:
                continue
            tier = str(entry.get("tier") or "SCHEDULED").upper()
            entity_id = self.upsert_entity(SourceEntityProfile(
                name=f"r/{subreddit}", organization="Reddit", entity_type="COMMUNITY",
                source_classes=("COMMUNITY",),
                specialties=("team_community", "community_reaction", tier.casefold()),
                conferences=(team["conference"],) if team.get("conference") else (),
                teams=(team["school"],), reliability_score=2, reporting_score=1,
                team_access_score=2, priority=2, trust_status="TRUSTED_COMMUNITY",
                entity_key=f"reddit-community:{subreddit.casefold()}",
            ))
            endpoint_id = self.upsert_endpoint(entity_id, SourceEndpointProfile(
                platform="reddit", endpoint_type="SUBREDDIT", handle=subreddit,
                platform_id=f"r/{subreddit}",
                url=f"https://www.reddit.com/r/{subreddit}/",
                endpoint_key=f"reddit:subreddit:{subreddit.casefold()}",
                verification_status=str(entry.get("verification_status") or
                                        "seeded_unverified"),
            ))
            with closing(self._connect()) as connection:
                connection.execute(
                    """INSERT INTO reddit_communities(
                         endpoint_id,community_type,quality_score,activity_score)
                       VALUES(?,'TEAM',3,NULL)
                       ON CONFLICT(endpoint_id) DO UPDATE SET
                         community_type='TEAM',quality_score=excluded.quality_score""",
                    (endpoint_id,),
                )
                connection.commit()
            promoted += 1
        return promoted

    def seed_media_candidates(self) -> int:
        """Idempotently import the versioned media research catalog.

        Candidate identity remains separate from endpoint identity.  A catalog
        record can supply an exact researched feed, but it is still resolved and
        title-checked before becoming an ingestible endpoint.
        """
        records = catalog_sources()
        self.initialize(); now = _now()
        with closing(self._connect()) as connection:
            for record in records:
                class_set = set(proposed_classes(record))
                if record["source"] == "College Football Nerds":
                    class_set.add("MODEL")
                if record["source"] == "Andy Staples":
                    class_set.add("NATIONAL_ANALYST")
                row = connection.execute(
                    """SELECT candidate_id,proposed_classes,proposed_entity_type
                       FROM source_candidates WHERE name=? ORDER BY candidate_id LIMIT 1""",
                    (record["source"],),
                ).fetchone()
                if row is None:
                    status = "SOURCE_CANDIDATE" if {"PODCAST", "YOUTUBE_SHOW"} & class_set \
                        else "CATALOG_ONLY"
                    entity_type = "PERSON" if record["source"] == "Andy Staples" else "SHOW"
                    classes = ",".join(sorted(class_set))
                    connection.execute(
                        """INSERT INTO source_candidates(name,proposed_entity_type,
                           proposed_classes,discovery_method,validation_status)
                           VALUES(?, ?, ?, 'USER_CURATED_MEDIA_CATALOG', ?)""",
                        (record["source"], entity_type, classes, status),
                    )
                    candidate_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                else:
                    candidate_id = row[0]
                    class_set.update(item for item in (row["proposed_classes"] or "").split(",")
                                     if item)
                    classes = ",".join(sorted(class_set))
                    connection.execute(
                        """UPDATE source_candidates SET proposed_classes=?,
                           discovery_method=CASE WHEN discovery_method='USER_CURATED_EXTENSION'
                             THEN discovery_method ELSE 'USER_CURATED_MEDIA_CATALOG' END
                           WHERE candidate_id=?""",
                        (classes, candidate_id),
                    )
                def flag(value):
                    return None if value is None else int(bool(value))
                connection.execute(
                    """INSERT INTO media_source_profiles VALUES(
                         ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(candidate_id) DO UPDATE SET
                         source_key=excluded.source_key,team=excluded.team,
                         conference=excluded.conference,coverage=excluded.coverage,
                         platform_json=excluded.platform_json,tags_json=excluded.tags_json,
                         priority=excluded.priority,youtube_url=excluded.youtube_url,
                         podcast_url=excluded.podcast_url,
                         podcast_page_url=excluded.podcast_page_url,website=excluded.website,
                         active_status=excluded.active_status,
                         last_verified_active=excluded.last_verified_active,
                         subscriber_count=excluded.subscriber_count,
                         episode_frequency=excluded.episode_frequency,
                         original_reporting=excluded.original_reporting,
                         program_access=excluded.program_access,
                         reporting_evidence=excluded.reporting_evidence,
                         content_focus=excluded.content_focus,notes=excluded.notes,
                         catalog_status=excluded.catalog_status,updated_at=excluded.updated_at""",
                    (candidate_id, record["source_key"], record["team"], record["conference"],
                     record["coverage"], json.dumps(record["platform"], separators=(",", ":")),
                     json.dumps(record["tags"], separators=(",", ":")), record["priority"],
                     record["youtube_url"], record["podcast_url"], record["podcast_page_url"],
                     record["website"], record["active_status"], record["last_verified_active"],
                     record["subscriber_count"], record["episode_frequency"],
                     flag(record["original_reporting"]), flag(record["program_access"]),
                     record["reporting_evidence"], record["content_focus"], record["notes"],
                     record["status"], now),
                )
            connection.commit()
        return len(records)

    def seed_configured_endpoints(self) -> int:
        """Attach endpoints already used or verified elsewhere in this application."""
        # NCAA publishes this endpoint in its official RSS directory. Keeping
        # the identity in the source graph preserves official-source attribution
        # instead of treating the feed as an unclassified publisher.
        self.upsert_entity(SourceEntityProfile(
            name="NCAA.com", organization="NCAA", entity_type="ORGANIZATION",
            entity_key="organization:ncaa",
            source_classes=("PRIMARY_SOURCE", "PUBLICATION"),
            specialties=("national_CFB", "FBS", "rankings", "playoff", "awards"),
            conferences=("ALL",), reliability_score=5, reporting_score=4,
            national_score=5, awards_score=5, official_score=5, priority=5,
            trust_status="OFFICIAL",
        ))
        self.upsert_entity(SourceEntityProfile(
            name="Yahoo Sports", organization="Yahoo", entity_type="ORGANIZATION",
            entity_key="organization:yahoo-sports",
            source_classes=("PUBLICATION",),
            specialties=("national_CFB", "news", "features", "analysis"),
            conferences=("ALL",), reliability_score=4, reporting_score=4,
            national_score=5, analytics_score=3, priority=4,
            trust_status="TRUSTED_SEED",
        ))
        for name, key, specialties in (
            ("CFBNumbers", "cfbnumbers", ("national_CFB", "EPA", "recruiting", "draft", "simulation")),
            ("CFB Graphs", "cfbgraphs", ("national_CFB", "modeling", "transfer_portal", "roster_construction")),
            ("The Mintner Method", "mintner-method", ("national_CFB", "PPA", "havoc", "returning_production")),
            ("The Spade", "the-spade", ("national_CFB", "NFL", "efficiency", "transfer_portal")),
            ("Bless Your Chart", "blessyourchart", ("national_CFB", "data_visualization", "margin_of_victory")),
            ("Bets and Reps", "betsandreps", ("national_CFB", "power_ratings", "betting")),
            ("Neil Paine", "neilpaine", ("national_CFB", "odds_tracker", "QB_value")),
        ):
            self.upsert_entity(SourceEntityProfile(
                name=name, organization=name, entity_type="ORGANIZATION",
                entity_key=f"publication:{key}",
                source_classes=("PUBLICATION", "NATIONAL_ANALYST"),
                specialties=specialties, conferences=("ALL",), reliability_score=4,
                reporting_score=1, national_score=4, analytics_score=5, priority=4,
                trust_status="TRUSTED_SEED",
            ))
        configured = (
            ("organization:espn", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS",
                platform_id="https://www.espn.com/espn/rss/ncf/news",
                url="https://www.espn.com/espn/rss/ncf/news",
                endpoint_key="rss:https://www.espn.com/espn/rss/ncf/news",
                verification_status="configured",
            )),
            ("organization:collegefootballdata", SourceEndpointProfile(
                platform="api", endpoint_type="API",
                platform_id="api.collegefootballdata.com",
                url="https://api.collegefootballdata.com/",
                endpoint_key="api:api.collegefootballdata.com",
                verification_status="verified",
            )),
            ("organization:ncaa", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS",
                platform_id="https://www.ncaa.com/news/football/fbs/rss.xml",
                url="https://www.ncaa.com/news/football/fbs/rss.xml",
                endpoint_key="rss:https://www.ncaa.com/news/football/fbs/rss.xml",
                verification_status="verified",
            )),
            ("organization:yahoo-sports", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS",
                platform_id="https://sports.yahoo.com/college-football/rss/",
                url="https://sports.yahoo.com/college-football/rss/",
                endpoint_key="rss:https://sports.yahoo.com/college-football/rss/",
                verification_status="verified",
            )),
            ("publication:cfbnumbers", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS", platform_id="https://cfbnumbers.substack.com/feed",
                url="https://cfbnumbers.substack.com/feed", endpoint_key="rss:https://cfbnumbers.substack.com/feed", verification_status="verified",
            )),
            ("publication:cfbgraphs", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS", platform_id="https://cfbgraphs.substack.com/feed",
                url="https://cfbgraphs.substack.com/feed", endpoint_key="rss:https://cfbgraphs.substack.com/feed", verification_status="verified",
            )),
            ("publication:mintner-method", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS", platform_id="https://themintnermethod.substack.com/feed",
                url="https://themintnermethod.substack.com/feed", endpoint_key="rss:https://themintnermethod.substack.com/feed", verification_status="verified",
            )),
            ("publication:the-spade", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS", platform_id="https://thespade.substack.com/feed",
                url="https://thespade.substack.com/feed", endpoint_key="rss:https://thespade.substack.com/feed", verification_status="verified",
            )),
            ("publication:blessyourchart", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS", platform_id="https://blessyourchart.substack.com/feed",
                url="https://blessyourchart.substack.com/feed", endpoint_key="rss:https://blessyourchart.substack.com/feed", verification_status="verified",
            )),
            ("publication:betsandreps", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS", platform_id="https://betsandreps.substack.com/feed",
                url="https://betsandreps.substack.com/feed", endpoint_key="rss:https://betsandreps.substack.com/feed", verification_status="verified",
            )),
            ("publication:neilpaine", SourceEndpointProfile(
                platform="rss", endpoint_type="WEBSITE_RSS", platform_id="https://neilpaine.substack.com/feed",
                url="https://neilpaine.substack.com/feed", endpoint_key="rss:https://neilpaine.substack.com/feed", verification_status="verified",
            )),
        )
        self.initialize(); added = 0
        with closing(self._connect()) as connection:
            entity_ids = {row["entity_key"]: row["source_entity_id"] for row in connection.execute(
                "SELECT source_entity_id,entity_key FROM source_entities")}
        for key, endpoint in configured:
            if key in entity_ids:
                self.upsert_endpoint(entity_ids[key], endpoint); added += 1
        return added

    def endpoints_by_platform(self, platform: str) -> list[dict]:
        self.initialize()
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM source_endpoints WHERE platform=? AND active=1 ORDER BY endpoint_id",
                (platform.casefold(),),
            )]

    def infer_organization_relationships(self) -> int:
        """Link people to publications only when organization and entity name agree exactly."""
        self.initialize(); now = _now(); added = 0
        with closing(self._connect()) as connection:
            organizations = {_key(row["name"]): row["source_entity_id"] for row in connection.execute(
                "SELECT source_entity_id,name FROM source_entities WHERE entity_type='ORGANIZATION'")}
            people = connection.execute(
                "SELECT source_entity_id,organization FROM source_entities WHERE entity_type='PERSON' AND organization IS NOT NULL"
            ).fetchall()
            for person in people:
                publication_id = organizations.get(_key(person["organization"]))
                if not publication_id or publication_id == person["source_entity_id"]:
                    continue
                before = connection.total_changes
                connection.execute(
                    """INSERT OR IGNORE INTO source_relationships(from_entity_id,to_entity_id,
                       relationship_type,confidence,verified_at) VALUES(?,?,'REPORTER_TO_PUBLICATION',1.0,?)""",
                    (person["source_entity_id"], publication_id, now),
                )
                added += connection.total_changes - before
            connection.commit()
        return added

    def coverage(self) -> dict:
        self.initialize()
        with closing(self._connect()) as connection:
            platforms = [dict(row) for row in connection.execute(
                """SELECT platform,COUNT(*) endpoint_count,
                   SUM(verification_status='verified') verified_count
                   FROM source_endpoints WHERE active=1 GROUP BY platform ORDER BY platform""")]
            classes = [dict(row) for row in connection.execute(
                """SELECT source_class,COUNT(*) entity_count FROM source_entity_classes c
                   JOIN source_entities e USING(source_entity_id) WHERE e.active=1
                   GROUP BY source_class ORDER BY entity_count DESC,source_class""")]
            topics = [dict(row) for row in connection.execute(
                """SELECT specialty,COUNT(*) entity_count FROM source_entity_specialties s
                   JOIN source_entities e USING(source_entity_id) WHERE e.active=1
                   GROUP BY specialty ORDER BY entity_count DESC,specialty LIMIT 50""")]
            has_teams = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='teams'").fetchone()
            conferences = []; teams = []
            if has_teams:
                conferences = [dict(row) for row in connection.execute(
                    """SELECT t.conference,COUNT(DISTINCT e.source_entity_id) entity_count
                       FROM teams t LEFT JOIN source_entity_conferences ec ON ec.conference=t.conference
                       LEFT JOIN source_entities e ON e.source_entity_id=ec.source_entity_id AND e.active=1
                       WHERE t.conference IS NOT NULL GROUP BY t.conference
                       ORDER BY entity_count,t.conference""")]
                teams = [dict(row) for row in connection.execute(
                    """SELECT t.team_id,t.school,t.conference,COUNT(DISTINCT e.source_entity_id) entity_count
                       FROM teams t LEFT JOIN source_entity_teams et ON et.team=t.school
                       LEFT JOIN source_entities e ON e.source_entity_id=et.source_entity_id AND e.active=1
                       GROUP BY t.team_id,t.school,t.conference ORDER BY entity_count,t.school""")]
        return {"platforms": platforms, "classes": classes, "topics": topics,
                "conferences": conferences, "teams": teams}

    def list_entities(self) -> list[dict]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM source_entities ORDER BY priority DESC,reliability_score DESC,name"
            ).fetchall(); result = []
            for row in rows:
                item = dict(row); entity_id = item["source_entity_id"]
                item["classes"] = [r[0] for r in connection.execute(
                    "SELECT source_class FROM source_entity_classes WHERE source_entity_id=? ORDER BY source_class", (entity_id,))]
                item["specialties"] = [r[0] for r in connection.execute(
                    "SELECT specialty FROM source_entity_specialties WHERE source_entity_id=? ORDER BY specialty", (entity_id,))]
                item["teams"] = [r[0] for r in connection.execute(
                    "SELECT team FROM source_entity_teams WHERE source_entity_id=? ORDER BY team", (entity_id,))]
                item["conferences"] = [r[0] for r in connection.execute(
                    "SELECT conference FROM source_entity_conferences WHERE source_entity_id=? ORDER BY conference", (entity_id,))]
                item["endpoints"] = [dict(r) for r in connection.execute(
                    "SELECT * FROM source_endpoints WHERE source_entity_id=? ORDER BY platform,endpoint_type", (entity_id,))]
                result.append(item)
        return result

    def status(self) -> dict:
        entities = self.list_entities()
        endpoints = [endpoint for entity in entities for endpoint in entity["endpoints"]]
        with closing(self._connect()) as connection:
            candidates = [dict(row) for row in connection.execute(
                "SELECT * FROM source_candidates ORDER BY name")]
            relationship_count = connection.execute(
                "SELECT COUNT(*) FROM source_relationships").fetchone()[0]
        return {
            "entity_count": len(entities), "endpoint_count": len(endpoints),
            "verified_endpoints": sum(item["verification_status"] == "verified" for item in endpoints),
            "relationship_count": relationship_count,
            "entities": entities, "candidates": candidates, "coverage": self.coverage(),
        }


SOURCE_TYPE_MAP = {
    "REPORTER": ("PERSON", ("NATIONAL_REPORTER",)),
    "REPORTER_ANALYST": ("PERSON", ("NATIONAL_REPORTER", "NATIONAL_ANALYST")),
    "BEAT_REPORTER": ("PERSON", ("BEAT_REPORTER",)),
    "INSIDER": ("PERSON", ("NATIONAL_REPORTER",)),
    "ANALYST": ("PERSON", ("NATIONAL_ANALYST",)),
    "DATA_ANALYST": ("ORGANIZATION", ("DATA_PROVIDER", "NATIONAL_ANALYST")),
    "DATA": ("ORGANIZATION", ("DATA_PROVIDER",)),
    "DEVELOPER_SOURCE": ("ORGANIZATION", ("DATA_PROVIDER",)),
    "SCOUT": ("PERSON", ("SCOUT", "RECRUITING_REPORTER")),
    "DRAFT_ANALYST": ("PERSON", ("DRAFT_ANALYST",)),
    "TEAM_OUTLET": ("ORGANIZATION", ("LOCAL_OUTLET",)),
    "OFFICIAL_TEAM": ("ORGANIZATION", ("PRIMARY_SOURCE", "OFFICIAL_TEAM")),
    "OUTLET": ("ORGANIZATION", ("PUBLICATION",)),
    "ANALYSIS_OUTLET": ("ORGANIZATION", ("PUBLICATION", "NATIONAL_ANALYST")),
    "COMMUNITY_ANALYSIS": ("ORGANIZATION", ("COMMUNITY", "TEAM_ANALYST")),
    "AGGREGATOR": ("ORGANIZATION", ("AGGREGATOR",)),
    "BOT": ("BOT", ("BOT",)), "BOT_DATA": ("BOT", ("BOT", "DATA_PROVIDER")),
    "SPECIALIST": ("ORGANIZATION", ("NATIONAL_ANALYST",)),
}


def migrate_bluesky_sources(database_path: str | Path) -> int:
    """Idempotently project the compatibility Bluesky table into the source graph."""
    registry = UnifiedSourceRegistry(database_path); registry.initialize(); count = 0
    with closing(registry._connect()) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'").fetchone():
            return 0
        sources = connection.execute("SELECT * FROM sources").fetchall()
        specialties = {row["source_id"]: [] for row in sources}
        teams = {row["source_id"]: [] for row in sources}
        conferences = {row["source_id"]: [] for row in sources}
        for row in connection.execute("SELECT * FROM source_specialties"):
            specialties.setdefault(row["source_id"], []).append(row["specialty"])
        for row in connection.execute("SELECT * FROM source_teams"):
            teams.setdefault(row["source_id"], []).append(row["team"])
        for row in connection.execute("SELECT * FROM source_conferences"):
            conferences.setdefault(row["source_id"], []).append(row["conference"])
    for row in sources:
        entity_type, classes = SOURCE_TYPE_MAP.get(row["source_type"], ("ORGANIZATION", ("PUBLICATION",)))
        tags = tuple(specialties[row["source_id"]]); lowered = {item.casefold() for item in tags}
        profile = SourceEntityProfile(
            name=row["display_name"], organization=row["organization"], entity_type=entity_type,
            source_classes=classes, specialties=tags,
            teams=tuple(teams[row["source_id"]]), conferences=tuple(conferences[row["source_id"]]),
            reliability_score=row["reliability"], reporting_score=row["original_reporting_score"],
            team_access_score=5 if row["source_type"] in {"BEAT_REPORTER", "TEAM_OUTLET", "OFFICIAL_TEAM"} else 2,
            national_score=5 if any("national" in item for item in lowered) else 2,
            analytics_score=row["analysis_score"],
            scheme_score=row["analysis_score"] if "scheme" in lowered else 1,
            recruiting_score=row["prospect_score"] if any("recruit" in item for item in lowered) else 1,
            transfer_score=row["prospect_score"] if any("transfer" in item or "portal" in item for item in lowered) else 1,
            draft_score=row["prospect_score"] if any("draft" in item or "prospect" in item for item in lowered) else 1,
            awards_score=5 if row["source_type"] == "OFFICIAL_TEAM" and "awards" in lowered else 1,
            g5_score=row["g5_score"], breaking_score=row["breaking_news_score"],
            official_score=5 if row["source_type"] == "OFFICIAL_TEAM" else 0,
            priority=row["priority"], trust_status=row["trust_status"], active=bool(row["active"]),
        )
        entity_id = registry.upsert_entity(profile)
        status = row["resolution_status"]
        registry.upsert_endpoint(entity_id, SourceEndpointProfile(
            platform="bluesky", endpoint_type="BLUESKY_ACCOUNT", handle=row["handle"],
            platform_id=row["did"], url=f"https://bsky.app/profile/{row['handle']}",
            endpoint_key=f"bluesky:{row['handle'].casefold()}",
            verification_status=status, active=bool(row["active"]),
        ))
        with closing(registry._connect()) as connection:
            connection.execute(
                """UPDATE source_endpoints SET verified_at=?,last_checked_at=?,last_error=?,
                   display_name=?,description=? WHERE endpoint_key=?""",
                (row["verified_at"], row["last_checked"], row["last_error"],
                 row["display_name"], row["profile_description"],
                 f"bluesky:{row['handle'].casefold()}"),
            )
            connection.commit()
        count += 1
    return count
