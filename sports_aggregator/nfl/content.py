"""League-scoped NFL news/social ingestion and conservative entity linking."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import json
import re
from typing import Any, Iterable

import requests

from sports_aggregator.models import Article, FeedConfig
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.providers.rss import RSSNewsProvider


GAME_LANGUAGE = re.compile(
    r"\b(?:vs\.?|versus|hosts?|matchup|game|kickoff|preview|recap|final|beats?|defeats?|week\s+\d+)\b",
    re.I,
)


def _normalized(value: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip() + " "


def _relative(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        hours = max(0, (datetime.now(timezone.utc) - parsed).total_seconds() / 3600)
    except (TypeError, ValueError):
        return "undated"
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    if hours < 168:
        return f"{int(hours // 24)}d ago"
    return parsed.strftime("%b %d")


class NFLContentRepository:
    """Persist content in nfl.sqlite3 so NFL IDs never enter CFB link tables."""

    def __init__(self, repository: NFLRepository) -> None:
        self.repository = repository

    def _team_candidates(self, connection, text: str, source_team: str | None) -> list[tuple[str, float, str]]:
        normalized = _normalized(text)
        found: dict[str, tuple[float, str]] = {}
        teams = [dict(row) for row in connection.execute("SELECT * FROM teams")]
        for team in teams:
            phrases = {team["name"], team["nickname"]}
            if any(f" {_normalized(phrase).strip()} " in normalized for phrase in phrases if phrase):
                found[team["abbreviation"]] = (0.94, "exact_team_name")
                continue
            abbreviation = team["abbreviation"]
            if len(abbreviation) >= 3 and re.search(
                    rf"(?<![A-Za-z]){re.escape(abbreviation)}(?![A-Za-z])", text):
                found[abbreviation] = (0.86, "explicit_abbreviation")
        if source_team:
            scoped = next((team for team in teams if source_team in {
                team["name"], team["nickname"], team["abbreviation"]}), None)
            if scoped:
                found.setdefault(scoped["abbreviation"], (0.64, "source_team_scope"))
        return [(team, confidence, method) for team, (confidence, method) in found.items()]

    def _player_candidates(self, connection, text: str, season: int,
                           teams: set[str]) -> list[tuple[int, str, float, str]]:
        normalized = _normalized(text)
        rows = [dict(row) for row in connection.execute(
            """SELECT season,player_id,team,full_name FROM players
               WHERE season IN (?,?) ORDER BY season DESC""", (season, season - 1)
        )]
        by_id: dict[str, list[dict]] = {}
        ids_by_name: dict[str, set[str]] = {}
        for row in rows:
            by_id.setdefault(row["player_id"], []).append(row)
            ids_by_name.setdefault(_normalized(row["full_name"]).strip(), set()).add(row["player_id"])
        found = []
        for player_id, identities in by_id.items():
            current = identities[0]
            name = _normalized(current["full_name"]).strip()
            if len(name) < 5 or f" {name} " not in normalized:
                continue
            relevant = [row for row in identities if row["team"] in teams]
            if relevant:
                confidence, method = 0.97, "exact_full_name_on_linked_team"
            elif len(ids_by_name.get(name, ())) == 1:
                confidence, method = 0.78, "exact_unique_full_name"
            else:
                continue
            found.append((current["season"], player_id, confidence, method))
        return found

    def _game_candidates(self, connection, text: str, published_at: str, season: int,
                         teams: set[str]) -> list[tuple[str, float, str]]:
        if not teams or not GAME_LANGUAGE.search(text):
            return []
        try:
            anchor = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            anchor = datetime.now(timezone.utc).date()
        rows = [dict(row) for row in connection.execute(
            "SELECT game_id,game_date,away_team,home_team FROM games WHERE season=?", (season,)
        )]
        if len(teams) >= 2:
            candidates = [row for row in rows if {row["away_team"], row["home_team"]}.issubset(teams)]
            method, confidence, window = "both_teams_game_context", 0.98, 45
        else:
            team = next(iter(teams))
            candidates = [row for row in rows if team in {row["away_team"], row["home_team"]}]
            method, confidence, window = "single_team_nearest_game", 0.76, 7
        dated = sorted(((abs((datetime.fromisoformat(row["game_date"]).date() - anchor).days), row)
                        for row in candidates), key=lambda item: item[0])
        return ([(dated[0][1]["game_id"], confidence, method)]
                if dated and dated[0][0] <= window else [])

    def store(self, *, platform: str, external_id: str, url: str, title: str,
              body: str, source_name: str, published_at: str, season: int,
              source_handle: str | None = None, source_team: str | None = None,
              raw: dict | None = None) -> int:
        self.repository.initialize()
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.repository._connect()) as connection:
            connection.execute(
                """INSERT INTO nfl_content_items
                   (platform,external_id,canonical_url,title,body_text,source_name,
                    source_handle,published_at,ingested_at,raw_json,content_type,
                    editorial_score,score_reasons_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform,external_id) DO UPDATE SET
                    canonical_url=excluded.canonical_url,title=excluded.title,
                    body_text=excluded.body_text,source_name=excluded.source_name,
                    source_handle=excluded.source_handle,published_at=excluded.published_at,
                    ingested_at=excluded.ingested_at,raw_json=excluded.raw_json""",
                (platform, external_id, url, title, body, source_name, source_handle,
                 published_at, now, json.dumps(raw or {}, separators=(",", ":")),
                 "update", 0, "[]"),
            )
            content_id = connection.execute(
                "SELECT content_id FROM nfl_content_items WHERE platform=? AND external_id=?",
                (platform, external_id),
            ).fetchone()[0]
            for table in ("nfl_content_teams", "nfl_content_players", "nfl_content_games"):
                connection.execute(f"DELETE FROM {table} WHERE content_id=?", (content_id,))
            text = f"{title} {body}"
            teams = self._team_candidates(connection, text, source_team)
            connection.executemany("INSERT INTO nfl_content_teams VALUES(?,?,?,?)",
                                   [(content_id, *row) for row in teams])
            team_codes = {row[0] for row in teams if row[1] >= 0.6}
            players = self._player_candidates(connection, text, season, team_codes)
            connection.executemany("INSERT INTO nfl_content_players VALUES(?,?,?,?,?)",
                                   [(content_id, *row) for row in players])
            games = self._game_candidates(connection, text, published_at, season, team_codes)
            connection.executemany("INSERT INTO nfl_content_games VALUES(?,?,?,?)",
                                   [(content_id, *row) for row in games])
            content_type, score, reasons = self._grade(
                platform, title, body, len(teams), len(players), len(games), raw or {},
            )
            connection.execute(
                """UPDATE nfl_content_items SET content_type=?,editorial_score=?,
                          score_reasons_json=? WHERE content_id=?""",
                (content_type, score, json.dumps(reasons, separators=(",", ":")), content_id),
            )
            connection.commit()
        return content_id

    @staticmethod
    def _grade(platform: str, title: str, body: str, teams: int,
               players: int, games: int, raw: dict | None = None) -> tuple[str, float, list[str]]:
        """Transparent editorial value score; freshness is added only at read time."""
        text = f"{title} {body}".casefold()
        score = 30.0 if platform == "rss" else 18.0
        reasons = ["published article" if platform == "rss" else "direct social update"]
        raw = raw or {}
        reliability = raw.get("reliability") or raw.get("reliability_score")
        if reliability is not None:
            quality = max(1, min(5, int(reliability)))
            score += (quality - 3) * 5
            reasons.append(f"source reliability {quality}/5")
        reporting = raw.get("reporting_score")
        if reporting is not None and int(reporting) >= 4:
            score += 5; reasons.append("original-reporting source")
        if len(body) >= 300:
            score += 14; reasons.append("substantive detail")
        elif len(body) >= 120:
            score += 8; reasons.append("useful detail")
        if players:
            score += min(12, players * 6); reasons.append("specific player linkage")
        if teams:
            score += min(8, teams * 4); reasons.append("specific team linkage")
        if games:
            score += 8; reasons.append("matchup linkage")
        if re.search(r"\b(film|breakdown|analysis|scheme|coverage|alignment|epa|grade)\b", text):
            content_type = "analysis"; score += 14; reasons.append("analysis evidence")
        elif re.search(r"\b(sign|trade|release|waive|roster|contract|injury|practice|draft)\b", text):
            content_type = "personnel"; score += 12; reasons.append("personnel reporting")
        elif re.search(r"\b(report|source|confirmed|announced|breaking)\b", text):
            content_type = "reporting"; score += 10; reasons.append("reported development")
        else:
            content_type = "update"
        if "http" in body.casefold():
            score += 4; reasons.append("supporting link")
        return content_type, min(100.0, round(score, 1)), reasons

    def rescore_all(self) -> int:
        self.repository.initialize()
        with closing(self.repository._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT i.*,COUNT(DISTINCT t.team) teams,COUNT(DISTINCT p.player_id) players,
                          COUNT(DISTINCT g.game_id) games
                   FROM nfl_content_items i
                   LEFT JOIN nfl_content_teams t ON t.content_id=i.content_id
                   LEFT JOIN nfl_content_players p ON p.content_id=i.content_id
                   LEFT JOIN nfl_content_games g ON g.content_id=i.content_id
                   GROUP BY i.content_id"""
            )]
            values = []
            for row in rows:
                kind, score, reasons = self._grade(
                    row["platform"], row["title"], row["body_text"],
                    row["teams"], row["players"], row["games"],
                    json.loads(row.get("raw_json") or "{}"),
                )
                values.append((kind, score, json.dumps(reasons, separators=(",", ":")),
                               row["content_id"]))
            connection.executemany(
                "UPDATE nfl_content_items SET content_type=?,editorial_score=?,score_reasons_json=? WHERE content_id=?",
                values,
            )
            connection.commit()
        return len(values)

    def ingest_articles(self, articles: Iterable[Article], season: int, *,
                        source_team: str | None = None) -> int:
        count = 0
        for article in articles:
            published = (article.published_at or datetime.now(timezone.utc)).isoformat()
            self.store(
                platform="rss", external_id=article.identity, url=article.url,
                title=article.title, body=article.summary, source_name=article.source,
                source_team=source_team, published_at=published, season=season,
                raw={"author": article.author, "discovered_via": article.discovered_via,
                     "reliability": article.reliability, "source_type": article.source_type,
                     "content_kind": article.content_kind},
            )
            count += 1
        return count

    @staticmethod
    def _fetch_feed(feed: FeedConfig, timeout: float) -> list[Article]:
        return RSSNewsProvider(feed, timeout_seconds=timeout).fetch()

    def ingest_rss_feeds(self, tasks: Iterable[tuple[FeedConfig, str | None]], season: int, *,
                         workers: int = 8, timeout: float = 20) -> dict[str, Any]:
        """Fetch a batch of team/national/community RSS feeds and store their articles.

        `tasks` pairs each feed with the team it's scoped to (or None for a
        league-wide feed) -- the same team-biasing `ingest_bluesky` gives
        Bluesky posts via `source_team`, just for RSS sources instead.
        """
        task_list = list(tasks)
        started = datetime.now(timezone.utc)
        stored = 0; seen = 0; succeeded = 0; errors = []; checks = []
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 12))) as pool:
            futures = {pool.submit(self._fetch_feed, feed, timeout): (feed, team)
                      for feed, team in task_list}
            for future in as_completed(futures):
                feed, team = futures[future]
                try:
                    articles = future.result()
                    succeeded += 1; seen += len(articles)
                    source_stored = self.ingest_articles(articles, season, source_team=team)
                    stored += source_stored
                    checks.append({"platform": "rss", "source_key": feed.url,
                                   "display_name": feed.name, "team": team, "success": True,
                                   "seen": len(articles), "stored": source_stored, "error": None})
                except Exception as exc:
                    message = str(exc)
                    errors.append({"feed": feed.name, "url": feed.url, "error": message})
                    checks.append({"platform": "rss", "source_key": feed.url,
                                   "display_name": feed.name, "team": team, "success": False,
                                   "seen": 0, "stored": 0, "error": message})
        finished = datetime.now(timezone.utc)
        self.record_source_checks(checks, checked_at=finished)
        self.record_ingestion_run(
            "rss_directory", season, started, finished, len(task_list), succeeded,
            seen, stored, errors,
        )
        return {"feeds": len(task_list), "succeeded": succeeded, "seen": seen,
                "stored": stored, "errors": errors}

    @staticmethod
    def _fetch_source(source: dict, posts_per_source: int, timeout: float) -> tuple[dict, list[dict]]:
        response = requests.get(
            "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
            params={"actor": source["handle"], "limit": posts_per_source,
                    "filter": "posts_no_replies", "includePins": "false"},
            headers={"User-Agent": "sports-news-aggregator/1.0"}, timeout=timeout,
        )
        response.raise_for_status()
        return source, response.json().get("feed", [])

    def ingest_bluesky(self, sources: Iterable[dict], season: int, *,
                       posts_per_source: int = 4, workers: int = 8,
                       timeout: float = 20) -> dict[str, Any]:
        source_rows = list(sources)
        started = datetime.now(timezone.utc)
        stored = 0; seen = 0; succeeded = 0; errors = []; checks = []
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 12))) as pool:
            futures = {pool.submit(self._fetch_source, source, posts_per_source, timeout): source
                       for source in source_rows}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    resolved, feed = future.result()
                    succeeded += 1; seen += len(feed); source_stored = 0
                    for item in feed:
                        if item.get("reason") or item.get("reply"):
                            continue
                        post = item.get("post") or {}; record = post.get("record") or {}
                        uri = str(post.get("uri") or ""); text = str(record.get("text") or "").strip()
                        if not uri or not text:
                            continue
                        author = post.get("author") or {}; handle = author.get("handle") or resolved["handle"]
                        rkey = uri.rsplit("/", 1)[-1]
                        embed = post.get("embed") or {}; external = embed.get("external") or {}
                        body = " ".join(filter(None, (text, external.get("title"), external.get("description"))))
                        title = (external.get("title") or text.splitlines()[0])[:240]
                        self.store(
                            platform="bluesky", external_id=uri,
                            url=f"https://bsky.app/profile/{handle}/post/{rkey}",
                            title=title, body=body, source_name=author.get("displayName") or resolved["display_name"],
                            source_handle=handle, source_team=resolved.get("team"),
                            published_at=record.get("createdAt") or post.get("indexedAt") or datetime.now(timezone.utc).isoformat(),
                            season=season, raw={"uri": uri, "cid": post.get("cid"),
                                                "external_url": external.get("uri"),
                                                "reliability_score": resolved.get("reliability_score"),
                                                "reporting_score": resolved.get("reporting_score"),
                                                "analytics_score": resolved.get("analytics_score")},
                        )
                        stored += 1; source_stored += 1
                    checks.append({"platform": "bluesky", "source_key": resolved["handle"],
                                   "display_name": resolved["display_name"],
                                   "team": resolved.get("team"), "success": True,
                                   "seen": len(feed), "stored": source_stored, "error": None})
                except Exception as exc:
                    message = str(exc)
                    errors.append({"handle": source["handle"], "error": message})
                    checks.append({"platform": "bluesky", "source_key": source["handle"],
                                   "display_name": source["display_name"],
                                   "team": source.get("team"), "success": False,
                                   "seen": 0, "stored": 0, "error": message})
        finished = datetime.now(timezone.utc)
        self.record_source_checks(checks, checked_at=finished)
        self.record_ingestion_run(
            "bluesky", season, started, finished, len(source_rows), succeeded,
            seen, stored, errors,
        )
        return {"sources": len(source_rows), "succeeded": succeeded, "seen": seen,
                "stored": stored, "errors": errors}

    def record_source_checks(self, checks: Iterable[dict[str, Any]], *,
                             checked_at: datetime | None = None) -> None:
        self.repository.initialize(); stamp = (checked_at or datetime.now(timezone.utc)).isoformat()
        rows = list(checks)
        with closing(self.repository._connect()) as connection:
            connection.executemany(
                """INSERT INTO nfl_content_source_checks
                   (platform,source_key,display_name,team,last_checked,last_success,
                    items_seen,items_stored,last_error) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform,source_key) DO UPDATE SET
                    display_name=excluded.display_name,team=excluded.team,
                    last_checked=excluded.last_checked,
                    last_success=COALESCE(excluded.last_success,nfl_content_source_checks.last_success),
                    items_seen=excluded.items_seen,items_stored=excluded.items_stored,
                    last_error=excluded.last_error""",
                [(row["platform"], str(row["source_key"]).casefold(), row["display_name"],
                  row.get("team"), stamp, stamp if row.get("success") else None,
                  int(row.get("seen") or 0), int(row.get("stored") or 0), row.get("error"))
                 for row in rows],
            )
            connection.commit()

    def record_ingestion_run(self, platform: str, season: int, started: datetime,
                             finished: datetime, attempted: int, succeeded: int,
                             seen: int, stored: int, errors: list[dict]) -> None:
        self.repository.initialize()
        with closing(self.repository._connect()) as connection:
            connection.execute(
                """INSERT INTO nfl_content_ingestion_runs
                   (platform,season,started_at,finished_at,attempted,succeeded,seen,stored,errors_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (platform, season, started.isoformat(), finished.isoformat(), attempted,
                 succeeded, seen, stored, json.dumps(errors, separators=(",", ":"))),
            )
            connection.commit()

    def latest_ingestion_run(self) -> dict | None:
        """Most recent content sync across every platform -- the cheap signal
        the nav health pill uses; the full per-platform history lives on the
        data-status page via `source_coverage()["latest_runs"]`."""
        self.repository.initialize()
        with closing(self.repository._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM nfl_content_ingestion_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def _items(self, join: str = "", where: str = "", parameters: tuple = (),
               limit: int = 30) -> list[dict]:
        self.repository.initialize()
        with closing(self.repository._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT DISTINCT i.* FROM nfl_content_items i {join}
                    {where} ORDER BY
                    (i.editorial_score + MAX(0,12-(julianday('now')-julianday(i.published_at)))) DESC,
                    i.published_at DESC LIMIT ?""",
                (*parameters, max(1, min(int(limit), 100))),
            )]
        for row in rows:
            row["published_relative"] = _relative(row["published_at"])
            row["source_type_label"] = "Bluesky" if row["platform"] == "bluesky" else "Article"
            row["score_reasons"] = json.loads(row.get("score_reasons_json") or "[]")
            with closing(self.repository._connect()) as connection:
                row["teams"] = [dict(item) for item in connection.execute(
                    """SELECT x.team,t.nickname,x.confidence,x.method FROM nfl_content_teams x
                       LEFT JOIN teams t ON t.abbreviation=x.team WHERE x.content_id=?
                       ORDER BY x.confidence DESC,x.team""", (row["content_id"],)
                )]
                row["players"] = [dict(item) for item in connection.execute(
                    """SELECT x.season,x.player_id,MAX(p.full_name) player_name,
                              x.confidence,x.method FROM nfl_content_players x
                       LEFT JOIN players p ON p.season=x.season AND p.player_id=x.player_id
                       WHERE x.content_id=? GROUP BY x.season,x.player_id,x.confidence,x.method
                       ORDER BY x.confidence DESC,player_name""", (row["content_id"],)
                )]
                row["games"] = [dict(item) for item in connection.execute(
                    "SELECT game_id,confidence,method FROM nfl_content_games WHERE content_id=?",
                    (row["content_id"],),
                )]
        return rows

    def source_coverage(self, sources: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Reconcile every configured account with checks and stored output."""
        self.repository.initialize(); configured = list(sources)
        with closing(self.repository._connect()) as connection:
            output = {str(row["source_key"]).casefold(): dict(row) for row in connection.execute(
                "SELECT * FROM nfl_content_source_checks WHERE platform='bluesky'"
            )}
            content = {str(row["source_key"]).casefold(): dict(row) for row in connection.execute(
                """SELECT LOWER(source_handle) source_key,COUNT(*) item_count,
                          MAX(published_at) latest_published,MAX(ingested_at) latest_ingested
                   FROM nfl_content_items WHERE platform='bluesky' AND source_handle IS NOT NULL
                   GROUP BY LOWER(source_handle)"""
            )}
            latest_runs = [dict(row) for row in connection.execute(
                """SELECT * FROM nfl_content_ingestion_runs ORDER BY run_id DESC LIMIT 10"""
            )]
            team_aliases = {}
            for team in connection.execute("SELECT abbreviation,name,nickname FROM teams"):
                for alias in (team["abbreviation"], team["name"], team["nickname"]):
                    if alias:
                        team_aliases[str(alias).casefold()] = team["abbreviation"]
        rows = []
        for source in configured:
            key = source["handle"].casefold(); check = output.get(key, {}); produced = content.get(key, {})
            sections = sorted({tag["section"] for tag in source.get("tags", [])})
            if check.get("last_error"):
                status = "error"
            elif check.get("last_checked") and not check.get("items_seen"):
                status = "silent"
            elif produced.get("item_count"):
                status = "producing"
            elif check.get("last_checked"):
                status = "no eligible items"
            else:
                status = "not checked"
            raw_team = check.get("team") or source.get("team")
            team_code = team_aliases.get(str(raw_team).casefold()) if raw_team else None
            rows.append({**source, **check, **produced, "team": team_code,
                         "team_label": raw_team,
                         "identity_last_checked": source.get("last_checked"),
                         "last_checked": check.get("last_checked"),
                         "sections": sections, "status": status,
                         "item_count": int(produced.get("item_count") or 0)})
        team_counts: dict[str, dict[str, int]] = {}
        for row in rows:
            if row.get("team"):
                bucket = team_counts.setdefault(row["team"], {"configured": 0, "producing": 0, "errors": 0})
                bucket["configured"] += 1
                bucket["producing"] += int(row["status"] == "producing")
                bucket["errors"] += int(row["status"] == "error")
        return {
            "configured": len(rows), "producing": sum(row["status"] == "producing" for row in rows),
            "checked": sum(bool(row.get("last_checked")) for row in rows),
            "errors": sum(row["status"] == "error" for row in rows),
            "silent": sum(row["status"] == "silent" for row in rows),
            "no_eligible": sum(row["status"] == "no eligible items" for row in rows),
            "rows": rows, "teams": team_counts, "latest_runs": latest_runs,
        }

    def latest(self, limit: int = 30) -> list[dict]:
        return self._items(limit=limit)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        pattern = f"%{str(query).strip().casefold()}%"
        return self._items(
            where="WHERE LOWER(i.title || ' ' || i.body_text || ' ' || i.source_name) LIKE ?",
            parameters=(pattern,), limit=limit,
        )

    def for_team(self, team: str, limit: int = 30) -> list[dict]:
        return self._items("JOIN nfl_content_teams x ON x.content_id=i.content_id",
                           "WHERE x.team=? AND x.confidence>=0.6", (team,), limit)

    def for_player(self, season: int, player_id: str, limit: int = 20) -> list[dict]:
        return self._items("JOIN nfl_content_players x ON x.content_id=i.content_id",
                           "WHERE x.season=? AND x.player_id=?", (season, player_id), limit)

    def for_game(self, game_id: str, limit: int = 30) -> list[dict]:
        return self._items("JOIN nfl_content_games x ON x.content_id=i.content_id",
                           "WHERE x.game_id=?", (game_id,), limit)

    def counts(self) -> dict[str, int]:
        self.repository.initialize()
        with closing(self.repository._connect()) as connection:
            return {name: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for name, table in (("items", "nfl_content_items"),
                                        ("team_links", "nfl_content_teams"),
                                        ("player_links", "nfl_content_players"),
                                        ("game_links", "nfl_content_games"))}
