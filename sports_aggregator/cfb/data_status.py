"""Sanitized refresh status and resilient content-linkage audit for the CFB app."""

from __future__ import annotations

from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from sports_aggregator import refresh_health


data_status_pages = Blueprint("cfb_data_status", __name__)

#: The "What changed" ledger has had no producer since 5292e14 removed the
#: snapshot-and-diff. The reader stays so a cheaper producer can light it back
#: up, but a record older than this is a fossil and is not shown.
_CHANGE_LEDGER_MAX_AGE_HOURS = 48

#: segment -> (env var naming its scheduled local hours, default)
_SEGMENT_SCHEDULE = {
    "core": ("CFB_REFRESH_CORE_HOURS", "6,18"),
    "content": ("CFB_REFRESH_CONTENT_HOURS", "10,16"),
    "rosters": ("CFB_REFRESH_ROSTER_HOURS", "12"),
    "stats": ("CFB_REFRESH_STATS_HOURS", "22"),
    "models": ("CFB_REFRESH_MODEL_HOURS", "23"),
    "analytics": ("CFB_REFRESH_ANALYTICS_HOURS", "2"),
    "news": ("CFB_REFRESH_NEWS_HOURS", "3,8,20"),
}
_SEGMENT_ORDER = ["core", "rosters", "stats", "models", "content", "analytics", "news"]

_STEP_LABELS = {
    "cfbd-sync": "Core CFBD data", "cfbd-lines": "Betting lines",
    "cfbd-box-scores": "Box scores", "cfbd-current-player-stats": "Player statistics",
    "articles": "Articles", "local-articles": "Local reporting",
    "local-news-shard": "Local reporting", "weather": "Weather",
    "bluesky": "Bluesky", "reddit": "Reddit", "youtube": "YouTube",
    "podcasts": "Podcasts",
}

_TABLE_LABELS = {
    "teams": "Teams", "players": "Rosters", "games": "Games & schedules",
    "game_lines": "Betting lines", "records": "Team records", "coaches": "Coaches",
    "rankings": "Rankings", "team_stats": "Team statistics",
    "advanced_stats": "Advanced statistics", "core_ratings": "CORE ratings",
    "content_items": "News & social content", "content_ingestion_runs": "Content ingestion runs",
}


def _database_path() -> Path:
    return Path(current_app.config["CFB_DATABASE_PATH"])


def _instance_dir() -> Path:
    return _database_path().parent


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_history(path: Path, limit: int = 12) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        rows: deque[dict[str, Any]] = deque(maxlen=limit)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
        return list(rows)[::-1]
    except Exception:
        return []


def _local_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        zone = ZoneInfo(current_app.config.get("CFB_DISPLAY_TIMEZONE", "America/New_York"))
        return parsed.astimezone(zone)
    except Exception:
        return None


def _display_time(value: Any) -> str:
    # `%-d`/`%-I` are a glibc extension: they raise ValueError on Windows, and
    # this label sits in the shared layout, so every page 500s there. Formatting
    # the unpadded numbers directly reads the same and runs anywhere.
    parsed = _local_datetime(value)
    if not parsed:
        return "Not recorded"
    hour = (parsed.hour - 1) % 12 + 1
    return (f"{parsed.strftime('%b')} {parsed.day}, {parsed.year} · "
            f"{hour}:{parsed.strftime('%M %p %Z')}").strip()


def _relative_time(value: Any) -> str:
    parsed = _local_datetime(value)
    if parsed is None:
        return "unknown"
    seconds = max(0, int((datetime.now(parsed.tzinfo) - parsed).total_seconds()))
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 36:
        return f"{hours} hr ago"
    return f"{hours // 24} d ago"


#: Public names for the import page, which shows the same two kinds of
#: timestamp and should not grow its own copy of them -- particularly of the
#: Windows fix above, which is invisible until every page 500s.
display_time = _display_time
relative_time = _relative_time


def _safe_step(row: dict[str, Any]) -> dict[str, Any]:
    step = str(row.get("step") or "unknown")
    result = {
        "step": step,
        "label": _STEP_LABELS.get(step, step.replace("-", " ").title()),
        "status": str(row.get("status") or "unknown"),
        "at": row.get("at") or row.get("finished_at"),
        "message": str(row.get("message") or "")[:180],
    }
    counts = []
    for key in ("added", "updated", "unchanged", "count"):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = int(value)
            counts.append(f"{int(value):,} {key}")
    # Steps that report numbers rather than a sentence had them collected here
    # and rendered nowhere, so "18 added, 9 updated" reached the page as the
    # word "Completed".
    result["changes"] = " · ".join(counts)
    return result


def _safe_change_ledger(instance: Path) -> dict[str, Any] | None:
    try:
        rows = _read_history(instance / "refresh_change_history.jsonl", limit=1)
        if not rows:
            return None
        raw = rows[0]
        finished = _local_datetime(raw.get("finished_at"))
        if finished is None or (datetime.now(finished.tzinfo) - finished
                                > timedelta(hours=_CHANGE_LEDGER_MAX_AGE_HOURS)):
            # No producer writes this file today; a stale record beside a live
            # "Degraded" pill only misleads. Hidden until something writes fresh.
            return None
        tables: list[dict[str, Any]] = []
        for item in raw.get("changes") or []:
            if not isinstance(item, dict):
                continue
            samples = []
            for sample in (item.get("samples") or [])[:8]:
                if not isinstance(sample, dict):
                    continue
                fields = []
                for field in (sample.get("fields") or [])[:4]:
                    if isinstance(field, dict):
                        fields.append({
                            "field": str(field.get("field") or "")[:60],
                            "before": str(field.get("before") or "")[:80],
                            "after": str(field.get("after") or "")[:80],
                        })
                samples.append({
                    "kind": str(sample.get("kind") or "changed")[:20],
                    "key": str(sample.get("key") or "")[:220],
                    "fields": fields,
                })
            table = str(item.get("table") or "unknown")
            tables.append({
                "table": table,
                "label": _TABLE_LABELS.get(table, table.replace("_", " ").title()),
                "added": int(item.get("added") or 0),
                "changed": int(item.get("changed") or 0),
                "removed": int(item.get("removed") or 0),
                "samples": samples,
            })
        totals = raw.get("totals") or {}
        return {
            "finished_label": _display_time(raw.get("finished_at")),
            "relative_label": _relative_time(raw.get("finished_at")),
            "profile": str(raw.get("profile") or "unknown"),
            "added": int(totals.get("added") or 0),
            "changed": int(totals.get("changed") or 0),
            "removed": int(totals.get("removed") or 0),
            "tracking_error": str(raw.get("tracking_error") or "")[:240],
            "tables": tables,
        }
    except Exception:
        return None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()}
    except sqlite3.Error:
        return set()


def _safe_url(value: Any) -> str:
    try:
        text = str(value or "").strip()
        parsed = urlparse(text)
        return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""
    except Exception:
        return ""


def _audit_empty(error: str = "", *, platform: str = "", sample_mode: str = "random",
                 limit: int = 80, connection_limit: int = 100) -> dict[str, Any]:
    return {
        "items": [], "connections": [], "flagged": [], "platforms": [],
        "available": False, "error": str(error or "")[:180],
        "selected_platform": platform, "sample_mode": sample_mode,
        "limit": limit, "connection_limit": connection_limit,
    }


def _audit_model(*, limit: int = 80, platform: str = "", sample_mode: str = "random",
                 connection_limit: int = 100) -> dict[str, Any]:
    limit = max(10, min(int(limit), 200))
    connection_limit = max(25, min(int(connection_limit), 250))
    sample_mode = sample_mode if sample_mode in {"random", "recent"} else "random"
    platform = str(platform or "").strip().casefold()[:40]
    database = _database_path()
    if not database.exists():
        return _audit_empty("database not found", platform=platform, sample_mode=sample_mode,
                            limit=limit, connection_limit=connection_limit)
    try:
        # `closing`, not the Connection's own context manager: that one
        # commits and does not close, so this leaked a file handle on
        # every render of the page.
        with closing(sqlite3.connect(database, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            if not all(_table_exists(connection, table) for table in ("content_items", "content_teams", "teams")):
                return _audit_empty("content linkage tables are not available", platform=platform,
                                    sample_mode=sample_mode, limit=limit,
                                    connection_limit=connection_limit)

            ci_cols = _columns(connection, "content_items")
            ct_cols = _columns(connection, "content_teams")
            team_cols = _columns(connection, "teams")
            if not {"content_id", "platform", "title", "ingested_at"}.issubset(ci_cols):
                return _audit_empty("content linkage schema is incomplete", platform=platform,
                                    sample_mode=sample_mode, limit=limit,
                                    connection_limit=connection_limit)
            if not {"content_id", "team_id", "confidence", "method"}.issubset(ct_cols):
                return _audit_empty("content linkage schema is incomplete", platform=platform,
                                    sample_mode=sample_mode, limit=limit,
                                    connection_limit=connection_limit)
            if not {"team_id", "school"}.issubset(team_cols):
                return _audit_empty("content linkage schema is incomplete", platform=platform,
                                    sample_mode=sample_mode, limit=limit,
                                    connection_limit=connection_limit)

            def ci(name: str, fallback: str = "NULL") -> str:
                return f'ci."{name}"' if name in ci_cols else fallback

            platforms = [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT platform FROM content_items WHERE platform IS NOT NULL AND platform <> '' ORDER BY platform"
            ).fetchall()]
            if platform and platform not in {value.casefold() for value in platforms}:
                platform = ""

            feedback = _table_exists(connection, "content_team_feedback")
            feedback_join = (
                "LEFT JOIN content_team_feedback f ON f.content_id=ci.content_id AND f.team_id=ct.team_id"
                if feedback else ""
            )
            feedback_select = (
                "f.verdict AS feedback_verdict, f.reason AS feedback_reason"
                if feedback else "NULL AS feedback_verdict, NULL AS feedback_reason"
            )
            where_parts = ["1=1"]
            params: list[Any] = []
            if platform:
                where_parts.append("LOWER(ci.platform)=?")
                params.append(platform)
            if feedback:
                where_parts.append(
                    "NOT EXISTS (SELECT 1 FROM content_team_feedback fx WHERE fx.content_id=ci.content_id AND fx.team_id=ct.team_id AND fx.verdict='bad')"
                )
            where_sql = " AND ".join(where_parts)
            published_expr = ci("published_at", "ci.ingested_at")
            order_sql = "RANDOM()" if sample_mode == "random" else f"COALESCE({published_expr}, ci.ingested_at) DESC, ci.content_id DESC"

            rows = connection.execute(
                f"""
                SELECT ci.content_id, ci.platform, ci.title,
                       {ci('canonical_url')} AS canonical_url,
                       {ci('original_url')} AS original_url,
                       {ci('publisher_name')} AS publisher_name,
                       {ci('author_name')} AS author_name,
                       {published_expr} AS published_at,
                       ci.ingested_at,
                       {ci('content_type')} AS content_type,
                       {ci('source_role')} AS source_role,
                       ct.team_id, ct.confidence, ct.method, t.school,
                       {feedback_select}
                  FROM content_items ci
                  JOIN content_teams ct ON ct.content_id=ci.content_id
                  JOIN teams t ON t.team_id=ct.team_id
                  {feedback_join}
                 WHERE {where_sql}
                 ORDER BY {order_sql}
                 LIMIT ?
                """, (*params, limit)
            ).fetchall()

            items: list[dict[str, Any]] = []
            for row in rows:
                try:
                    published_raw = row["published_at"]
                    source = str(row["publisher_name"] or row["author_name"] or row["platform"] or "Unknown")
                    items.append({
                        "content_id": int(row["content_id"]),
                        "team_id": int(row["team_id"]),
                        "team": str(row["school"] or "Unknown")[:100],
                        "platform": str(row["platform"] or "unknown")[:30],
                        "source": source[:120],
                        "title": str(row["title"] or "Untitled")[:240],
                        "url": _safe_url(row["canonical_url"] or row["original_url"]),
                        "published_label": _display_time(published_raw),
                        "published_age": _relative_time(published_raw),
                        "ingested_label": _display_time(row["ingested_at"]),
                        "ingested_age": _relative_time(row["ingested_at"]),
                        "content_type": str(row["content_type"] or "")[:60],
                        "source_role": str(row["source_role"] or "")[:60],
                        "confidence": round(float(row["confidence"] or 0), 3),
                        "method": str(row["method"] or "unknown")[:120],
                        "feedback_verdict": str(row["feedback_verdict"] or ""),
                        "feedback_reason": str(row["feedback_reason"] or "")[:180],
                    })
                except Exception:
                    continue

            source_expr = (
                "COALESCE(NULLIF(ci.publisher_name,''), NULLIF(ci.author_name,''), ci.platform)"
                if {"publisher_name", "author_name"}.issubset(ci_cols) else "ci.platform"
            )
            conn_where = ["1=1"]
            conn_params: list[Any] = []
            if platform:
                conn_where.append("LOWER(ci.platform)=?")
                conn_params.append(platform)
            if feedback:
                conn_where.append(
                    "NOT EXISTS (SELECT 1 FROM content_team_feedback fx WHERE fx.content_id=ci.content_id AND fx.team_id=ct.team_id AND fx.verdict='bad')"
                )
            connections = [dict(row) for row in connection.execute(
                f"""
                SELECT {source_expr} AS source, ci.platform, t.school AS team,
                       COUNT(*) AS item_count, ROUND(AVG(ct.confidence), 3) AS avg_confidence,
                       MAX({published_expr}) AS newest_published,
                       MAX(ci.ingested_at) AS last_ingested
                  FROM content_items ci
                  JOIN content_teams ct ON ct.content_id=ci.content_id
                  JOIN teams t ON t.team_id=ct.team_id
                 WHERE {' AND '.join(conn_where)}
                 GROUP BY source, ci.platform, t.team_id, t.school
                 ORDER BY item_count DESC, newest_published DESC
                 LIMIT ?
                """, (*conn_params, connection_limit)
            ).fetchall()]
            for row in connections:
                row["source"] = str(row.get("source") or "Unknown")[:120]
                row["platform"] = str(row.get("platform") or "unknown")[:30]
                row["team"] = str(row.get("team") or "Unknown")[:100]
                newest = row.pop("newest_published", None)
                ingested = row.pop("last_ingested", None)
                row["newest_label"] = _display_time(newest)
                row["newest_age"] = _relative_time(newest)
                row["last_ingested_label"] = _display_time(ingested)

            flagged: list[dict[str, Any]] = []
            if feedback:
                flagged_rows = connection.execute(
                    f"""
                    SELECT f.content_id, f.team_id, f.reason, f.created_at,
                           ci.title, ci.platform,
                           {ci('canonical_url')} AS canonical_url,
                           {ci('original_url')} AS original_url,
                           {source_expr} AS source, t.school AS team
                      FROM content_team_feedback f
                      JOIN content_items ci ON ci.content_id=f.content_id
                      JOIN teams t ON t.team_id=f.team_id
                     WHERE f.verdict='bad'
                     ORDER BY f.created_at DESC
                     LIMIT 50
                    """
                ).fetchall()
                for raw in flagged_rows:
                    try:
                        row = dict(raw)
                        row["url"] = _safe_url(row.pop("canonical_url", "") or row.pop("original_url", ""))
                        row["created_label"] = _display_time(row.pop("created_at", None))
                        row["title"] = str(row.get("title") or "Untitled")[:240]
                        row["reason"] = str(row.get("reason") or "")[:180]
                        row["source"] = str(row.get("source") or "Unknown")[:120]
                        row["team"] = str(row.get("team") or "Unknown")[:100]
                        flagged.append(row)
                    except Exception:
                        continue

            return {
                "items": items, "connections": connections, "flagged": flagged,
                "platforms": platforms, "available": True, "error": "",
                "selected_platform": platform, "sample_mode": sample_mode,
                "limit": limit, "connection_limit": connection_limit,
            }
    except Exception as exc:
        return _audit_empty(f"audit unavailable: {type(exc).__name__}", platform=platform,
                            sample_mode=sample_mode, limit=limit,
                            connection_limit=connection_limit)


def _ensure_feedback_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS content_team_feedback (
            content_id INTEGER NOT NULL, team_id INTEGER NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('bad')),
            reason TEXT NOT NULL DEFAULT '', previous_confidence REAL,
            previous_method TEXT, created_at TEXT NOT NULL,
            PRIMARY KEY(content_id, team_id)
        );
        CREATE INDEX IF NOT EXISTS idx_content_team_feedback_verdict
            ON content_team_feedback(verdict, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS content_team_feedback_block_bad
        BEFORE INSERT ON content_teams
        WHEN EXISTS (
            SELECT 1 FROM content_team_feedback f
            WHERE f.content_id=NEW.content_id AND f.team_id=NEW.team_id AND f.verdict='bad'
        )
        BEGIN SELECT RAISE(IGNORE); END;
        """
    )


def _require_audit_auth() -> None:
    """Accept the same credentials as a manual refresh.

    The re-run button on the status page authenticates through the app's
    `require_refresh_auth`, which takes either the refresh token or the short
    admin PIN and a PIN sets a session. This used to accept the refresh token
    only, so someone who typed the PIN got the log tail rejected on the same
    page where the re-run had just been accepted.
    """
    from flask import session
    if session.get("cfb_admin") is True:
        return
    token = str(current_app.config.get("CFB_REFRESH_TOKEN") or "").strip()
    pin = str(current_app.config.get("CFB_ADMIN_PIN") or "").strip()
    provided = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token and not pin:
        abort(503, description="CFB_REFRESH_TOKEN or CFB_ADMIN_PIN is not configured")
    if provided and ((token and secrets.compare_digest(provided, token))
                     or (pin and secrets.compare_digest(provided, pin))):
        if pin and secrets.compare_digest(provided, pin):
            session["cfb_admin"] = True
            session.permanent = True
        return
    abort(401)


def _next_scheduled_label(segment: str) -> str:
    env_name, default = _SEGMENT_SCHEDULE.get(segment, ("", ""))
    if not env_name:
        return ""
    hours = sorted({int(part) for part in os.getenv(env_name, default).split(",")
                    if part.strip().isdigit()})
    if not hours:
        return ""
    zone = ZoneInfo(current_app.config.get("CFB_DISPLAY_TIMEZONE", "America/New_York"))
    now = datetime.now(zone)
    upcoming = next((h for h in hours if h > now.hour), hours[0])
    return f"{upcoming:02d}:00"


_STATUS_TONE = {"success": "ok", "degraded": "warn", "failed": "bad", "running": "warn"}


def _segment_grid(instance: Path) -> list[dict[str, Any]]:
    health = refresh_health.read_health(instance)
    tiles: list[dict[str, Any]] = []
    known = list(dict.fromkeys(_SEGMENT_ORDER + [k for k in health if isinstance(k, str)]))
    for segment in known:
        entry = health.get(segment)
        if not isinstance(entry, dict) and segment not in _SEGMENT_ORDER:
            continue
        entry = entry if isinstance(entry, dict) else {}
        status = str(entry.get("last_status") or "unknown")
        issues = [i for i in (entry.get("open_issues") or []) if isinstance(i, dict)]
        headline = ""
        if issues:
            worst = min(issues, key=lambda i: 0 if i.get("severity") == "failed" else 1)
            headline = f"{worst.get('step')}: {worst.get('category', '').replace('_', ' ')}"
        tiles.append({
            "segment": segment,
            "status": status,
            "tone": _STATUS_TONE.get(status, "muted"),
            "last_run_label": _relative_time(entry.get("last_run_at")),
            "last_success_label": _relative_time(entry.get("last_success_at")),
            "consecutive_degraded": int(entry.get("consecutive_degraded") or 0),
            "next_label": _next_scheduled_label(segment),
            "headline": headline,
            "seen": bool(entry),
        })
    return tiles


def _attention_model(instance: Path) -> dict[str, Any]:
    items = []
    for raw in refresh_health.attention_items(instance):
        items.append({
            **raw,
            "since_label": _relative_time(raw.get("since")),
            "last_success_label": _relative_time(raw.get("last_success_at")),
            "category_label": str(raw.get("category") or "").replace("_", " "),
        })
    not_configured = refresh_health.not_configured_items(instance)
    return {
        "items": items,
        "not_configured": not_configured,
        "overall": refresh_health.overall(instance),
    }


def _status_model(include_audit: bool = True, *, audit_options: dict[str, Any] | None = None) -> dict[str, Any]:
    instance = _instance_dir()
    progress = _read_json(instance / "refresh_progress.json")
    history = _read_history(instance / "scheduled_refresh_history.jsonl")
    running = (instance / "scheduled_refresh.lock").exists()
    latest = history[0] if history else {}
    latest_steps = latest.get("steps") if isinstance(latest.get("steps"), list) else []
    if not latest_steps:
        latest_steps = [
            {"step": name, **(entry if isinstance(entry, dict) else {})}
            for name, entry in (progress.get("steps") or {}).items()
        ]
    sections = [_safe_step(row) for row in latest_steps if isinstance(row, dict)]
    for row in sections:
        row["time_label"] = _display_time(row.get("at"))
        row["relative_label"] = _relative_time(row.get("at"))
    recent_runs = [{
        "profile": str(item.get("profile") or "unknown"),
        "season": item.get("season"), "status": str(item.get("status") or "unknown"),
        "started_label": _display_time(item.get("started_at")),
        "finished_label": _display_time(item.get("finished_at")),
        "seconds": item.get("seconds"), "step_count": item.get("step_count"),
        "degraded_count": item.get("degraded_count", 0),
        "required_failure_count": item.get("required_failure_count", 0),
    } for item in history]
    latest_finished = latest.get("finished_at") or progress.get("finished_at")
    options = audit_options or {}
    attention = _attention_model(instance)
    return {
        "running": running,
        "latest_status": str(latest.get("status") or ("running" if running else "unknown")),
        "latest_profile": str(latest.get("profile") or progress.get("profile") or "unknown"),
        "latest_finished_label": _display_time(latest_finished),
        "latest_relative_label": _relative_time(latest_finished),
        "sections": sections, "recent_runs": recent_runs,
        "attention": attention["items"],
        "not_configured": attention["not_configured"],
        "overall": attention["overall"],
        "segments": _segment_grid(instance),
        "change_ledger": _safe_change_ledger(instance),
        "audit": _audit_model(**options) if include_audit else _audit_empty(),
    }


#: How the whole-pipeline verdict reads in the site-wide freshness pill.
_PILL_STATUS = {"failed": "failed", "degraded": "degraded",
                "self_healing": "success", "healthy": "success"}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def _megabytes(raw: str | None) -> int | None:
    """Bytes as megabytes; None for "max" (no limit) or anything unreadable."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return round(value / 2**20) if value < 2**60 else None


def _memory_split(root: Path, v2: bool) -> tuple[int | None, int | None]:
    """(anonymous/process MB, file-cache MB) from the cgroup's memory.stat."""
    stat = root / "memory.stat" if v2 else root / "memory" / "memory.stat"
    values: dict[str, int] = {}
    for line in (_read_text(stat) or "").splitlines():
        key, _, number = line.partition(" ")
        if number.strip().isdigit():
            values[key] = int(number)
    anon = values.get("anon", values.get("rss"))
    file_cache = values.get("file", values.get("cache"))
    to_mb = lambda value: None if value is None else round(value / 2**20)
    return to_mb(anon), to_mb(file_cache)


def _cpu_limit(root: Path, v2: bool) -> float | None:
    """CPUs this container may use (its quota), not the host's count that os.cpu_count() returns."""
    try:
        if v2:
            quota, _, period = (_read_text(root / "cpu.max") or "").strip().partition(" ")
            return None if quota in ("", "max") else round(int(quota) / int(period or 100000), 2)
        quota = int((_read_text(root / "cpu" / "cpu.cfs_quota_us") or "-1").strip())
        period = int((_read_text(root / "cpu" / "cpu.cfs_period_us") or "100000").strip())
        return None if quota <= 0 else round(quota / period, 2)
    except (ValueError, ZeroDivisionError):
        return None


def host_resources(root: Path = Path("/sys/fs/cgroup")) -> dict[str, Any] | None:
    """The memory this container is allowed, what it is using, and how many times the kernel has
    killed something in it for memory -- read from the container's own cgroup.

    The refresh steps that die leave no trace of why, and what the instance's memory *is* could not
    be told from the code (render.yaml says one thing, a comment another). `oom_kill` is a count of
    processes the kernel killed because the container hit its limit, which is the direct answer to
    "were these killed for memory?". Counters are per container, so a deploy resets them. None when
    not running in a Linux container that exposes a cgroup.
    """
    if not root.exists():
        return None
    v2 = (root / "memory.max").exists()
    if v2:
        limit = _megabytes(_read_text(root / "memory.max"))
        used = _megabytes(_read_text(root / "memory.current"))
        peak = _megabytes(_read_text(root / "memory.peak"))
        events = {}
        for line in (_read_text(root / "memory.events") or "").splitlines():
            key, _, value = line.partition(" ")
            if value.isdigit():
                events[key] = int(value)
        oom_kills = events.get("oom_kill")
    else:
        legacy = root / "memory"
        limit = _megabytes(_read_text(legacy / "memory.limit_in_bytes"))
        used = _megabytes(_read_text(legacy / "memory.usage_in_bytes"))
        peak = _megabytes(_read_text(legacy / "memory.max_usage_in_bytes"))
        oom_kills = None
        for line in (_read_text(legacy / "memory.oom_control") or "").splitlines():
            if line.startswith("oom_kill "):
                oom_kills = int(line.split()[1])
    if limit is None and used is None:
        return None
    # `used` includes the kernel's file cache, which is reclaimable: reading a 3.9 GB SQLite file fills it, so the
    # headline number sits near the limit without anything being wrong (the peak equals the limit, with no kills).
    # What can actually get a process killed is the anonymous part, so report it separately.
    anon, cache = _memory_split(root, v2)
    return {"memory_limit_mb": limit, "memory_used_mb": used, "memory_peak_mb": peak,
            "memory_processes_mb": anon, "memory_file_cache_mb": cache,
            "oom_kills": oom_kills, "cpus": os.cpu_count(), "cpu_limit": _cpu_limit(root, v2)}


DISK_WALK_SECONDS = 4.0
DISK_CACHE_SECONDS = 300
_DISK_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _tree_bytes(path: Path, deadline: float) -> tuple[int, bool]:
    """Total size of everything under `path`, and whether the walk finished before the deadline."""
    total = 0
    for current, _dirs, files in os.walk(path, onerror=lambda _e: None):
        if time.monotonic() > deadline:
            return total, False
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                continue
    return total, True


def _mount_point(directory: Path) -> str:
    """The mount (or drive) the directory lives on, so the page can say WHICH disk it measured: /var/data on
    Render, the PC's own drive when the app runs locally."""
    path = Path(os.path.abspath(directory))
    while not os.path.ismount(path) and path.parent != path:
        path = path.parent
    return str(path)


def disk_report(directory: Path, *, top: int = 10, cache_seconds: float = DISK_CACHE_SECONDS,
                walk_seconds: float = DISK_WALK_SECONDS) -> dict[str, Any] | None:
    """How full the disk holding `directory` is, and which entries in it use the space.

    The NFL steps that rewrite large tables were failing with "database or disk is full" while the
    database was 758 MB and capped at nothing, and there was no way to see what else shared the
    disk. Sizes come from `shutil.disk_usage` (the filesystem, not the sum of files) plus one level
    of entries with their recursive sizes, largest first. The walk has a time budget and the result is
    cached for a few minutes so a page view never pays for it twice. None when the directory is not
    readable.
    """
    key = str(directory)
    cached = _DISK_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < cache_seconds:
        return cached[1]
    try:
        usage = shutil.disk_usage(directory)
        entries = list(os.scandir(directory))
    except OSError:
        return None
    deadline = time.monotonic() + walk_seconds
    sizes: list[dict[str, Any]] = []
    complete = True
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                size, finished = _tree_bytes(Path(entry.path), deadline)
                complete = complete and finished
            else:
                size, finished = entry.stat(follow_symlinks=False).st_size, True
        except OSError:
            continue
        sizes.append({"name": entry.name + ("/" if entry.is_dir(follow_symlinks=False) else ""),
                      "mb": round(size / 2**20, 1), "partial": not finished})
    sizes.sort(key=lambda item: -item["mb"])
    mb = lambda value: round(value / 2**20)
    report = {
        "mount": _mount_point(directory),
        "total_mb": mb(usage.total), "used_mb": mb(usage.used), "free_mb": mb(usage.free),
        "percent_used": round(100 * usage.used / usage.total, 1) if usage.total else None,
        "listed_mb": round(sum(item["mb"] for item in sizes)),
        "entries": sizes[:top], "complete": complete,
    }
    _DISK_CACHE[key] = (time.monotonic(), report)
    return report


def deployed_build() -> dict[str, str] | None:
    """The commit this process is running, when the platform says (Render sets RENDER_GIT_COMMIT).

    Whether a merged fix is actually live was not answerable from outside the dashboard, and
    "still failing" after a merge could mean either that the fix did not work or that it was never
    deployed. None when running anywhere that does not report it.
    """
    commit = (os.getenv("RENDER_GIT_COMMIT") or "").strip()
    if not commit:
        return None
    return {"commit": commit[:7], "branch": (os.getenv("RENDER_GIT_BRANCH") or "").strip()}


@data_status_pages.app_context_processor
def inject_data_freshness() -> dict[str, Any]:
    instance = _instance_dir()
    running = (instance / "scheduled_refresh.lock").exists()
    try:
        verdict = refresh_health.overall(instance)
    except Exception:
        verdict = {"state": "unknown", "actionable_count": 0}
    state = verdict.get("state", "unknown")
    if state == "unknown":
        # No roll-up yet: fall back to the newest history row.
        history = _read_history(instance / "scheduled_refresh_history.jsonl", limit=1)
        latest = history[0] if history else {}
        status = str(latest.get("status") or "unknown")
        latest_finished = latest.get("finished_at")
    else:
        status = _PILL_STATUS.get(state, "unknown")
        runs = [str(e.get("last_run_at") or "") for e in refresh_health.read_health(instance).values()
                if isinstance(e, dict)]
        latest_finished = max(runs) if runs else None
    # The disk report walks directories, so it is NOT built here: this runs on every page of the site.
    return {"deploy_build": deployed_build(), "host_resources": host_resources(), "data_freshness": {
        "running": running,
        "status": status,
        "relative": _relative_time(latest_finished) if latest_finished else "",
        "attention_count": int(verdict.get("actionable_count") or 0),
    }}


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


@data_status_pages.get("/college-football/data-status/")
def data_status():
    options = {
        "platform": request.args.get("platform", ""),
        "sample_mode": request.args.get("sample", "random"),
        "limit": _int_arg("limit", 80),
        "connection_limit": _int_arg("connections", 100),
    }
    try:
        model = _status_model(include_audit=True, audit_options=options)
    except Exception:
        model = _status_model(include_audit=False)
    return render_template("cfb_data_status.html", status=model, host_disk=disk_report(_instance_dir()))


@data_status_pages.get("/college-football/data-status/log-tail")
def log_tail():
    """The tail of the newest refresh log for one segment, behind the token.

    The status page links this from each attention card; the page itself never
    embeds a log path, so this is the only route to the run output.
    """
    _require_audit_auth()
    segment = str(request.args.get("segment") or "").strip().casefold()[:20]
    try:
        lines = max(20, min(int(request.args.get("lines", 120)), 400))
    except (TypeError, ValueError):
        lines = 120
    instance = _instance_dir()
    history = _read_history(instance / "scheduled_refresh_history.jsonl", limit=60)
    match = next((row for row in history
                  if not segment or str(row.get("profile") or "") == segment), None)
    if match is None:
        return jsonify({"segment": segment, "log": None, "lines": [],
                        "error": "no run recorded for that segment yet"})
    log_path = Path(str(match.get("log") or ""))
    if not log_path.is_absolute():
        log_path = instance / log_path
    try:
        tail = deque(log_path.open("r", encoding="utf-8", errors="replace"), maxlen=lines)
    except OSError:
        return jsonify({"segment": segment, "log": log_path.name, "lines": [],
                        "error": "log file is no longer on disk"})
    return jsonify({
        "segment": segment,
        "log": log_path.name,
        "finished_label": _display_time(match.get("finished_at")),
        "status": str(match.get("status") or "unknown"),
        "lines": [line.rstrip("\n") for line in tail],
    })


@data_status_pages.post("/college-football/data-status/team-link-feedback")
def team_link_feedback():
    _require_audit_auth()
    payload = request.get_json(silent=True) or {}
    try:
        content_id = int(payload.get("content_id")); team_id = int(payload.get("team_id"))
    except (TypeError, ValueError):
        abort(400, description="content_id and team_id are required")
    action = str(payload.get("action") or "bad").strip().casefold()
    reason = str(payload.get("reason") or "Not relevant to this team").strip()[:180]
    if action not in {"bad", "undo"}:
        abort(400, description="action must be bad or undo")

    # Closes and commits: the Connection's own context manager only does
    # the second, and this one writes.
    with closing(sqlite3.connect(_database_path())) as connection, connection:
        connection.row_factory = sqlite3.Row
        _ensure_feedback_schema(connection)
        if action == "bad":
            association = connection.execute(
                "SELECT confidence, method FROM content_teams WHERE content_id=? AND team_id=?",
                (content_id, team_id),
            ).fetchone()
            if association is None:
                abort(404, description="content/team association was not found")
            connection.execute(
                """
                INSERT INTO content_team_feedback
                    (content_id, team_id, verdict, reason, previous_confidence, previous_method, created_at)
                VALUES (?, ?, 'bad', ?, ?, ?, ?)
                ON CONFLICT(content_id, team_id) DO UPDATE SET
                    verdict='bad', reason=excluded.reason,
                    previous_confidence=excluded.previous_confidence,
                    previous_method=excluded.previous_method,
                    created_at=excluded.created_at
                """,
                (content_id, team_id, reason, float(association["confidence"] or 0),
                 str(association["method"] or "manual_restore"), datetime.now(timezone.utc).isoformat()),
            )
            connection.execute("DELETE FROM content_teams WHERE content_id=? AND team_id=?", (content_id, team_id))
        else:
            previous = connection.execute(
                "SELECT previous_confidence, previous_method FROM content_team_feedback WHERE content_id=? AND team_id=? AND verdict='bad'",
                (content_id, team_id),
            ).fetchone()
            if previous is None:
                abort(404, description="bad-link feedback was not found")
            connection.execute("DELETE FROM content_team_feedback WHERE content_id=? AND team_id=?", (content_id, team_id))
            connection.execute(
                "INSERT OR IGNORE INTO content_teams(content_id, team_id, confidence, method) VALUES (?, ?, ?, ?)",
                (content_id, team_id, float(previous["previous_confidence"] or 0),
                 str(previous["previous_method"] or "manual_restore")),
            )
        connection.commit()
    return jsonify({"status": "ok", "action": action, "content_id": content_id, "team_id": team_id})
