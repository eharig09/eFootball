"""Run bounded production refresh segments without copying the live database.

This module used to snapshot and diff a large set of SQLite tables before and
after every automatic refresh. That audit was useful while building the
pipeline, but on the constrained Render web service it duplicated disk I/O and
CPU around even tiny score refreshes. Production refreshes now keep only the
bounded execution, progress/history logging, lock safety, and RSS telemetry.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from sports_aggregator.refresh_health import classify_step_rows, summarize_segment
from sports_aggregator.scheduled_refresh import (
    REFRESH_PROFILES,
    _acquire_lock,
    _children_rss_mb,
    _refresh_statistics,
    _rss_mb,
    _run_cfbd_split,
    _run_low_memory_phase,
    _run_news_shard,
    _run_command,
    _run_player_stats_split,
    _touch_lock,
    _write_progress,
    run_scheduled_refresh,
)

CORE_DATASETS = ["teams", "games", "betting_lines", "media", "records", "coaches", "rankings"]
STATS_DATASETS = ["team_stats", "advanced_stats", "core_ratings"]
CONTENT_STEPS = ["articles", "bluesky", "reddit", "youtube", "podcasts", "retag", "cluster", "roles", "score", "nfl-content"]
ROSTER_STEPS = ["cfbd-roster-context", "cfbd-recruits", "transfer-grades", "nfl-rosters"]
MODEL_STEPS = ["cfbd-models", "cfbd-game-ppa", "cfbd-box-scores", "cfbd-lines", "weather", "nfl-pff",
              "coach-elo", "qb-elo", "xdrives-model", "xpoints-model", "xredzone-dataset"]

#: Refresh steps the core segment runs by name after the CORE_DATASETS pull.
#: `pregame-snapshot` freezes each upcoming game's pregame state and belongs
#: on the most frequent segment there is -- core runs at 4/6/18 local -- so a
#: stage is captured before kickoff rather than lost.
CORE_STEPS = ["weather", "pregame-snapshot"]

#: Play-by-play and everything derived from it, plus the coordinator sync.
#:
#: None of these had a way to run in production. The cron drives the segments
#: in this file and none of them named these steps, and the only other route --
#: asking the web hook for a "heavy" profile -- silently narrowed to just the
#: core segment (see `_run_heavy`, fixed alongside this). So the box score's
#: EPA, the turning points, the middle-of-field splits and the coordinator
#: tendencies had no path to the deployed database at all, however long it ran.
#:
#: Ordered by the plan rather than by this list: `pbp` has to land before
#: anything derives from it, and `_run_low_memory_phase` keeps the plan's own
#: order. They are the most expensive steps in the refresh, which is why they
#: get a segment to themselves rather than a place inside another one.
ANALYTICS_STEPS = [
    "pbp", "pbp-derive", "epa", "play-detail", "build-tendencies",
    "team-advanced", "win-probability", "passing-detail", "passing-qb",
    # The matchup page reads cfbd_rushing_plays for both run-direction and
    # red-zone rushing panels. This step existed in bootstrap but was omitted
    # from the production analytics segment, so those panels silently vanished
    # as the deployed table went stale/empty.
    "rushing-detail", "coordinators",
    "nfl-core-foundation", "nfl-core-stats", "nfl-core-depth", "nfl-core-pbp",
]

#: Small, projection-specific refresh path. These are the only PBP-derived
#: tables game_projection.py needs to absorb a newly completed game. Keeping
#: this separate from the full analytics segment lets Render refresh matchup
#: inputs throughout the day without also rerunning EPA/WP/NFL analytics.
PROJECTION_STEPS = [
    "pbp", "pbp-derive", "team-pace", "team-scoring",
    "team-special-teams", "team-drive-outcomes",
]


def _nearest_upcoming_week(repository, season: int) -> int | None:
    upcoming = repository.upcoming_games(int(season), limit=1)
    if not upcoming:
        return None
    week = upcoming[0].get("week")
    return int(week) if week is not None else None


def _refresh_two_engine_manifest(season: int, *, root: Path, log) -> dict:
    from sports_aggregator.cfb.repository import CFBRepository

    configured = (os.getenv("CFB_DATABASE_PATH") or "").strip()
    database = Path(configured) if configured else root / "instance" / "cfb.sqlite3"
    if not database.is_absolute():
        database = root / database

    repository = CFBRepository(database)
    week = _nearest_upcoming_week(repository, season)
    if week is None:
        return {
            "step": "two-engine-manifest",
            "status": "skipped",
            "message": "no upcoming week found",
            "seconds": 0.0,
            "optional": True,
            "parent_rss_mb": _rss_mb(),
            "child_peak_rss_mb": _children_rss_mb(),
        }

    status, message, seconds = _run_command(
        [
            "sports_aggregator.cfb.two_engine_manifest_cli",
            "--season", str(int(season)),
            "--week", str(int(week)),
            "--database", str(database),
        ],
        timeout=600,
        log=log,
    )
    return {
        "step": "two-engine-manifest",
        "status": status,
        "message": f"week {week}: {message}",
        "seconds": seconds,
        "optional": True,
        "parent_rss_mb": _rss_mb(),
        "child_peak_rss_mb": _children_rss_mb(),
    }

#: The maintenance segments, which the hourly trigger reaches one at a time by
#: the clock. Nameable directly so a segment can also be run on demand: a
#: backfill should not have to wait for its hour to come round.
SEGMENTS = ("core", "rosters", "stats", "models", "content", "analytics", "projections", "news")

#: Every segment `profile=heavy` walks on an on-demand request. "news" is
#: excluded on purpose -- it already has its own profile and cadence, the same
#: carve-out `scheduled_refresh`'s original heavy profile made for the Google
#: News crawl.
HEAVY_SEGMENTS = tuple(segment for segment in SEGMENTS if segment != "news")


def _hours(name: str, default: str) -> set[int]:
    return {
        int(value.strip())
        for value in os.getenv(name, default).split(",")
        if value.strip()
    }


def _segment_for_light(now: datetime | None = None) -> str:
    zone = ZoneInfo(os.getenv("CFB_REFRESH_TIMEZONE", "America/New_York"))
    moment = (now or datetime.now(timezone.utc)).astimezone(zone)
    schedule = (
        ("core", _hours("CFB_REFRESH_CORE_HOURS", "6,18")),
        ("content", _hours("CFB_REFRESH_CONTENT_HOURS", "10,16")),
        ("rosters", _hours("CFB_REFRESH_ROSTER_HOURS", "12")),
        ("stats", _hours("CFB_REFRESH_STATS_HOURS", "22")),
        ("models", _hours("CFB_REFRESH_MODEL_HOURS", "23")),
        ("analytics", _hours("CFB_REFRESH_ANALYTICS_HOURS", "2")),
    )
    for name, hours in schedule:
        if moment.hour in hours:
            return name
    return "core"


def _segment_results(segment: str, season: int, *, root: Path, log, heartbeat) -> list[dict]:
    if segment == "core":
        results = [
            _run_cfbd_split(
                season,
                root=root,
                timeout=600,
                log=log,
                datasets=CORE_DATASETS,
                heartbeat=heartbeat,
            )
        ]
        results += _run_low_memory_phase(
            "refresh",
            season,
            root=root,
            only=CORE_STEPS,
            timeout=600,
            log=log,
            heartbeat=heartbeat,
        )
        return results

    if segment == "rosters":
        results = [
            _run_cfbd_split(
                season,
                root=root,
                timeout=300,
                log=log,
                datasets=["players"],
                heartbeat=heartbeat,
            )
        ]
        results += _run_low_memory_phase(
            "refresh",
            season,
            root=root,
            only=ROSTER_STEPS,
            timeout=600,
            log=log,
            heartbeat=heartbeat,
        )
        return results

    if segment == "stats":
        results = [
            _run_cfbd_split(
                season,
                root=root,
                timeout=600,
                log=log,
                datasets=STATS_DATASETS,
                heartbeat=heartbeat,
            )
        ]
        results.append(
            _run_player_stats_split(
                season,
                root=root,
                timeout=300,
                log=log,
                optional=True,
                heartbeat=heartbeat,
            )
        )
        return results

    if segment == "models":
        return _run_low_memory_phase(
            "refresh",
            season,
            root=root,
            only=MODEL_STEPS,
            timeout=600,
            log=log,
            heartbeat=heartbeat,
        )

    if segment == "analytics":
        # Each step carries its own budget -- 1,800 seconds for the play
        # ingest, less for what reads it -- so the driver's timeout here is
        # only a backstop.
        return _run_low_memory_phase(
            "refresh",
            season,
            root=root,
            only=ANALYTICS_STEPS,
            timeout=1800,
            log=log,
            heartbeat=heartbeat,
        )

    if segment == "projections":
        # Frequent live-model maintenance: ingest only newly completed PBP,
        # derive it, then rebuild the current-season team-game actuals consumed
        # directly by game_projection.py. No model fitting occurs here.
        results = _run_low_memory_phase(
            "refresh",
            season,
            root=root,
            only=PROJECTION_STEPS,
            timeout=1800,
            log=log,
            heartbeat=heartbeat,
        )
        if heartbeat:
            heartbeat()
        results.append(
            _refresh_two_engine_manifest(
                season,
                root=root,
                log=log,
            )
        )
        return results

    if segment == "content":
        return _run_low_memory_phase(
            "refresh",
            season,
            root=root,
            only=CONTENT_STEPS,
            timeout=600,
            log=log,
            heartbeat=heartbeat,
        )

    if segment == "news":
        return [_run_news_shard(season, timeout=600, log=log)]

    raise ValueError(f"unknown refresh segment: {segment}")


def _run_segment(segment: str, season: int, *, root: Path, instance: Path) -> dict:
    started = datetime.now(timezone.utc)
    lock = instance / "scheduled_refresh.lock"
    if not _acquire_lock(lock, started, 1):
        return {
            "status": "skipped",
            "reason": "refresh_already_running",
            "profile": segment,
            "season": season,
        }

    logs = instance / "refresh_logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"refresh-{started.strftime('%Y%m%dT%H%M%SZ')}.log"
    progress = {
        "season": season,
        "profile": segment,
        "started_at": started.isoformat(),
        "completed": False,
        "steps": {},
    }
    _write_progress(instance, progress)

    try:
        with log_path.open("w", encoding="utf-8") as log:
            print(
                f"segmented refresh: profile={segment} season={season} "
                f"parent_rss_mb={_rss_mb()}",
                file=log,
                flush=True,
            )
            results = _segment_results(
                segment,
                season,
                root=root,
                log=log,
                heartbeat=lambda: _touch_lock(lock),
            )
            for result in results:
                progress["steps"][str(result.get("step"))] = {
                    "status": str(result.get("status")),
                    "at": datetime.now(timezone.utc).isoformat(),
                    "message": str(result.get("message") or "")[:180],
                }
                _write_progress(instance, progress)

        _refresh_statistics(instance)
        finished = datetime.now(timezone.utc)
        required, degraded, skipped = classify_step_rows(results, segment=segment)
        status = "failed" if required else "degraded" if degraded else "success"
        report = {
            "status": status,
            "profile": segment,
            "season": season,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "seconds": round((finished - started).total_seconds(), 1),
            "exit_code": 1 if required else 0,
            "log": str(log_path),
            "step_count": len(results),
            "degraded_steps": degraded,
            "required_failures": required,
            "skipped_steps": skipped,
            "degraded_count": len(degraded),
            "required_failure_count": len(required),
            "parent_peak_rss_mb": _rss_mb(),
            "child_peak_rss_mb": _children_rss_mb(),
            "resumed_steps": [],
        }
        progress["completed"] = True
        progress["finished_at"] = finished.isoformat()
        _write_progress(instance, progress)
        with (instance / "scheduled_refresh_history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, separators=(",", ":")) + "\n")
        try:
            summarize_segment(instance, report)
        except Exception:  # a roll-up write must never fail the refresh
            pass
        return report
    finally:
        lock.unlink(missing_ok=True)


def _run_heavy(season: int, *, root: Path, instance: Path) -> dict:
    """Every maintenance segment in one on-demand pass.

    The hourly cron never asks for this -- it ticks one bounded segment per
    hour by design, to keep the constrained web service light. `profile=heavy`
    is the deliberate, operator-requested exception: catch a stale database up
    in one go. It used to silently narrow to just the `core` segment, which
    meant the pbp/EPA/tendencies/box-score chain that "populate everything now"
    is for never ran at all.

    Each segment still writes its own entry to scheduled_refresh_history.jsonl
    through `_run_segment`; this only rolls them up for the caller.
    """
    started = datetime.now(timezone.utc)
    segments = []
    for segment in HEAVY_SEGMENTS:
        segments.append(_run_segment(segment, season, root=root, instance=instance))
    finished = datetime.now(timezone.utc)
    required_failures = sum(int(report.get("required_failure_count") or 0) for report in segments)
    degraded = sum(int(report.get("degraded_count") or 0) for report in segments)
    status = "failed" if required_failures else "degraded" if degraded else "success"
    return {
        "status": status, "profile": "heavy", "season": season,
        "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "seconds": round((finished - started).total_seconds(), 1),
        "exit_code": 1 if required_failures else 0,
        "segments": [{"profile": report.get("profile"), "status": report.get("status"),
                      "seconds": report.get("seconds"), "log": report.get("log")}
                     for report in segments],
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    parser = argparse.ArgumentParser(
        description="Run a bounded scheduled refresh with low production overhead"
    )
    parser.add_argument("--season", type=int, default=datetime.now().year)
    parser.add_argument(
        "--profile",
        choices=tuple(sorted(REFRESH_PROFILES)),
        default="light",
    )
    parser.add_argument(
        "--segment",
        choices=SEGMENTS,
        default=None,
        help="Run one maintenance segment now instead of the one this hour is for",
    )
    args = parser.parse_args(argv)

    database = Path(
        (os.getenv("CFB_DATABASE_PATH") or "").strip()
        or root / "instance" / "cfb.sqlite3"
    )
    if not database.is_absolute():
        database = root / database
    instance = database.parent

    if args.segment:
        report = _run_segment(args.segment, args.season, root=root, instance=instance)
    elif args.profile in {"scores", "results"}:
        report = run_scheduled_refresh(
            args.season,
            profile=args.profile,
            repo_root=root,
        )
    elif args.profile == "heavy":
        report = _run_heavy(args.season, root=root, instance=instance)
    else:
        segment = "news" if args.profile == "news" else _segment_for_light()
        report = _run_segment(segment, args.season, root=root, instance=instance)

    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") == "skipped" else int(report.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
