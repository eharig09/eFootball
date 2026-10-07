"""Isolated subprocess entry points for the shared scheduled refresh.

Deliberately does not import `app` (Flask, every CFB blueprint, every NFL
web/analytics module). It used to: every segment here booted the entire
combined application just to reach a handful of narrow sync functions,
which was real, measured waste (see git history) but turned out not to be
the whole story for nfl-core specifically. Live production bisection
found the actual cause of its remaining "OpenBLAS error: Memory
allocation still failed": render.yaml sets OPENBLAS_NUM_THREADS=1 (and
OMP/MKL/NUMEXPR alongside it) precisely to stop OpenBLAS from sizing its
thread pool off the container's *visible* CPU count (16, the host's --
not the Starter plan's real ~0.5 vCPU share), but a live check found all
four completely unset in the running process. Whatever broke that
propagation, 16 threads each reserving their own stack/buffers is exactly
enough virtual address space to explain failures instantly, at a
resident-memory peak far too low to be the real numpy/pandas work.
Forcing these here does not depend on that propagation working at all.

Each function below imports only the sports_aggregator.nfl and
sports_aggregator.social modules it actually needs.
"""
from __future__ import annotations

# Must happen before anything that might import numpy/pandas (transitively
# or directly) -- OpenBLAS reads these at library-init time. setdefault, not
# assignment: an explicitly-configured value (should the env propagation
# above ever get fixed) still wins.
import os
for _threads_var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_threads_var, "1")

import argparse
from datetime import datetime, timezone
import faulthandler
import json
from pathlib import Path
import sys
import traceback
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]


def _path(env_name: str, default: str) -> Path:
    value = Path(os.getenv(env_name, default))
    return value if value.is_absolute() else ROOT / value


def _nfl_repository():
    from sports_aggregator.nfl.repository import NFLRepository
    return NFLRepository(_path("NFL_DATABASE_PATH", "instance/nfl.sqlite3"))


def _nflverse_cache_path() -> Path:
    return _path("NFLVERSE_RAW_CACHE_PATH", "instance/nflverse_raw")


def _nflverse_client():
    from sports_aggregator.nfl.nflverse import NflverseClient
    return NflverseClient(_nflverse_cache_path())


def _source_registry():
    from sports_aggregator.social.registry import SourceRegistry
    return SourceRegistry(_path("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))


def _sync_espn_context(repository, season: int, *, force: bool = False) -> None:
    from sports_aggregator.nfl.espn import sync_espn_context
    context = sync_espn_context(repository, _nflverse_cache_path(), season, force=force)
    print(f"nfl_context: success (staff={context['staff']}, scheme_rates={context['scheme_rates']}, "
          f"pressure_rates={context['pressure_rates']}, injuries={context['injuries']})")


def _sync_rosters(season: int, *, force: bool = False) -> None:
    from sports_aggregator.nfl.sync import NFLDataSync
    repository = _nfl_repository()
    count = NFLDataSync(_nflverse_client(), repository).sync_players(season, force=force)
    print(f"players: success ({count})")
    _sync_espn_context(repository, season, force=force)


#: (dataset, message) for each dataset the most recent `_sync_core` reported as failed.
_LAST_DATASET_FAILURES: list[tuple[str, str]] = []


def _sync_core(season: int, *, include_pbp: bool, only: "frozenset[str] | None" = None,
               include_extras: bool = True, force: bool = False) -> bool:
    """`only` scopes this to one of the four subprocess-sized groups in
    sync.py (CORE_FOUNDATION/CORE_STATS/CORE_DEPTH/CORE_PBP) -- see that
    module's docstring for why a single "run everything" process no longer
    fits safely. `include_extras` gates the ESPN staff/injury sync and NFL
    source-directory seeding, neither of which is part of NFLDataSync's own
    job list; only one of the four segments (foundation) needs to run them."""
    from sports_aggregator.nfl.sync import NFLDataSync
    repository = _nfl_repository()
    report = NFLDataSync(_nflverse_client(), repository).sync(
        season, force=force, include_pbp=include_pbp, only=only,
    )
    _LAST_DATASET_FAILURES[:] = [(item.dataset, str(getattr(item, "message", "") or "failed")[:300])
                                 for item in report.datasets if item.status == "failed"]
    for dataset in report.datasets:
        # The reason a dataset failed used to be dropped here, leaving the status page with "failed
        # (0)" and a coverage line and no way to tell a memory error from bad data.
        reason = f" -- {dataset.message[:300]}" if dataset.status == "failed" and getattr(dataset, "message", "") else ""
        print(f"{dataset.dataset}: {dataset.status} ({dataset.count}){reason}")
    if include_extras:
        from sports_aggregator.nfl.source_directory import DEFAULT_PATH, import_directory
        _sync_espn_context(repository, season, force=force)
        _sync_market_lines(repository, season)
        # Normally a fresh empty disk's fast path handles this (see
        # production_seed.restore_public_seed); repeated here so a disk
        # that already has core data but never got the registry seeded
        # still gets it.
        if DEFAULT_PATH.exists():
            sources = import_directory(_source_registry(), DEFAULT_PATH)
            print(f"nfl_sources: success ({sources})")
    if include_pbp and report.succeeded:
        # Football Lab's walk-forward model inputs take minutes to build and are cached on disk; building them
        # here keeps that cost out of the first page view. A failure must never fail the refresh.
        try:
            from sports_aggregator.nfl import model_cache
            print("model_cache: " + json.dumps(model_cache.warm(repository, season), sort_keys=True))
        except Exception as exc:
            print(f"model_cache: skipped ({exc.__class__.__name__}: {exc})")
        _freeze_forecasts(repository, season)
    from sports_aggregator.nfl.data_health import season_coverage
    print("nfl_game_coverage: " + json.dumps(
        season_coverage(repository, season), sort_keys=True
    ))
    return report.succeeded


def _sync_market_lines(repository, season: int) -> None:
    """Keep ESPN's opening/current lines for the live weeks, and settle finished ones once.

    The upcoming week and the one after are refetched every run (lines move);
    earlier weeks are only touched while a finished game still lacks its final
    line, so a long season costs a handful of requests per run, not eighteen
    scoreboard calls. A failure must never fail the refresh.
    """
    try:
        from sports_aggregator.nfl.engine_picks import default_week
        from sports_aggregator.nfl.odds_history import sync_market_lines
        schedule = repository.schedule(season)
        current = default_week(repository, season)
        if current is None:
            return
        settled = repository.espn_market_game_ids(season)
        unsettled_past = {int(g["week"]) for g in schedule
                          if g["completed"] and g["game_id"] not in settled}
        weeks = sorted(unsettled_past | {current, current + 1})
        weeks = [w for w in weeks if any(int(g["week"]) == w for g in schedule)]
        print("nfl_market_lines: " + json.dumps(
            {"weeks": weeks, **sync_market_lines(repository, season, weeks)}, sort_keys=True))
    except Exception as exc:
        print(f"nfl_market_lines: skipped ({exc.__class__.__name__}: {exc})")


def _freeze_forecasts(repository, season: int) -> None:
    """Store the upcoming week's picks in the immutable ledger behind /nfl/picks/record/.

    Runs beside the model-cache warm-up because the walk-forward inputs are
    already built here, so freezing costs one cheap projection pass. Only
    changed forecasts are written, and freeze_dashboard refuses anything issued
    after kickoff. A failure must never fail the refresh.
    """
    try:
        from sports_aggregator.nfl.engine_picks import build_dashboard, default_week
        from sports_aggregator.nfl.forecast_ledger import freeze_dashboard
        week = default_week(repository, season)
        if week is None:
            print("forecast_ledger: skipped (no upcoming week)")
            return
        stored = freeze_dashboard(repository, build_dashboard(repository, season, week))
        print("forecast_ledger: " + json.dumps({"week": week, "stored": stored}, sort_keys=True))
    except Exception as exc:
        print(f"forecast_ledger: skipped ({exc.__class__.__name__}: {exc})")


def _sync_content(season: int) -> None:
    from sports_aggregator.catalog import get_league
    from sports_aggregator.nfl import rss_directory
    from sports_aggregator.nfl.content import NFLContentRepository
    from sports_aggregator.service import build_default_service

    content = NFLContentRepository(_nfl_repository())
    league = get_league("nfl")
    started = datetime.now(timezone.utc)
    result = build_default_service().aggregate(league)
    article_count = content.ingest_articles(result.articles, season)
    finished = datetime.now(timezone.utc)
    errors = [{"source": error.source, "error": error.message} for error in result.errors]
    failed_sources = {error["source"] for error in errors}
    articles_by_source = {feed.name: sum(article.source == feed.name for article in result.articles)
                          for feed in league.feeds}
    content.record_source_checks(({
        "platform": "rss", "source_key": feed.url, "display_name": feed.name,
        "success": feed.name not in failed_sources,
        "seen": articles_by_source[feed.name], "stored": articles_by_source[feed.name],
        "error": next((error["error"] for error in errors if error["source"] == feed.name), None),
    } for feed in league.feeds), checked_at=finished)
    content.record_ingestion_run(
        "rss", season, started, finished, len(league.feeds),
        len(league.feeds) - len(errors), len(result.articles), article_count, errors,
    )
    print(f"nfl_articles: success ({article_count})")
    if result.errors:
        print(f"nfl_article_errors: {len(result.errors)}")

    tasks = [(feed, None) for feed in rss_directory.national_feeds()]
    for team, feeds in rss_directory.team_feeds().items():
        tasks.extend((feed, team) for feed in feeds)
    directory = content.ingest_rss_feeds(tasks, season, workers=8)
    print(f"nfl_rss_directory: stored ({directory['stored']}) from {directory['feeds']} feeds")
    if directory["errors"]:
        print(f"nfl_rss_directory_errors: {len(directory['errors'])}")
        for error in directory["errors"][:20]:
            print(f"  {error['feed']}: {error['error']}")

    sources = _source_registry().list_league_sources("nfl")
    social = content.ingest_bluesky(sources, season, posts_per_source=4)
    print(f"nfl_bluesky: stored ({social['stored']}) from {social['sources']} sources")
    if social["errors"]:
        print(f"nfl_bluesky_errors: {len(social['errors'])}")
        for error in social["errors"][:20]:
            print(f"  {error['handle']}: {error['error']}")

    rescored = content.rescore_all()
    print(f"nfl_content_scored: {rescored}")
    print("nfl_content_links: " + json.dumps(content.counts(), sort_keys=True))


def _sync_pff(season: int, *, force_scan: bool = False) -> None:
    from sports_aggregator.nfl.pff import NFLPFFService
    source_root = os.getenv("NFL_PFF_SOURCE_ROOT", r"C:\Users\ehari\Desktop\scouting_report")
    upload_root = str(_path("NFL_PFF_UPLOAD_ROOT", "instance/nfl_pff_uploads"))
    result = NFLPFFService(_nfl_repository(), source_root, upload_root).sync(season, force_scan=force_scan)
    print("nfl_pff: " + json.dumps(result, sort_keys=True))


def _sync_history(start_year: int, end_year: int, *, include_pbp: bool, force: bool = False) -> None:
    from sports_aggregator.nfl.sync import NFLDataSync
    result = NFLDataSync(_nflverse_client(), _nfl_repository()).sync_history(
        start_year, end_year, force=force, include_pbp=include_pbp,
    )
    print("nfl_history: " + json.dumps(result, sort_keys=True))


def _sync_weather(season: int, *, force: bool = False) -> None:
    from sports_aggregator.nfl.weather import sync_game_weather
    from sports_aggregator.providers.weather import OpenMeteoClient
    repository = _nfl_repository()
    games = repository.schedule(season)
    client = OpenMeteoClient(cache_path=_path("NFL_WEATHER_CACHE_PATH", "instance/weather"))
    result = sync_game_weather(repository, games, client=client, force=force)
    print("nfl_weather: " + json.dumps(result, sort_keys=True))


def availability_refresh_plan(games: list[dict], *, cache_modified_at: float | None,
                              now: datetime | None = None) -> dict:
    """Choose the injury cadence from the nearest not-yet-stale kickoff."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    eastern = ZoneInfo("America/New_York")
    candidates = []
    for game in games:
        if game.get("completed"):
            continue
        game_date = str(game.get("game_date") or "").strip()
        if not game_date:
            continue
        game_time = str(game.get("game_time") or "12:00").strip() or "12:00"
        try:
            kickoff = datetime.fromisoformat(f"{game_date}T{game_time}").replace(
                tzinfo=eastern).astimezone(timezone.utc)
        except ValueError:
            continue
        seconds = (kickoff - moment.astimezone(timezone.utc)).total_seconds()
        if -30 * 60 <= seconds <= 48 * 60 * 60:
            candidates.append((abs(seconds), seconds, kickoff, game))
    if not candidates:
        return {"due": False, "reason": "no game within 48 hours"}

    _, seconds, kickoff, game = min(candidates, key=lambda item: item[0])
    interval_seconds = 15 * 60 if seconds <= 4 * 60 * 60 else 60 * 60
    cache_age = None if cache_modified_at is None else max(
        0.0, moment.timestamp() - float(cache_modified_at))
    due = cache_age is None or cache_age >= interval_seconds
    return {
        "due": due,
        "reason": "due" if due else "injury snapshot is inside the target cadence",
        "interval_minutes": interval_seconds // 60,
        "cache_age_minutes": round(cache_age / 60, 1) if cache_age is not None else None,
        "game_id": game.get("game_id"),
        "matchup": f"{game.get('away_team')} @ {game.get('home_team')}",
        "kickoff": kickoff.isoformat(),
        "hours_to_kickoff": round(seconds / 3600, 2),
    }


def _sync_availability(season: int, *, now: datetime | None = None) -> dict:
    from sports_aggregator.nfl.espn import sync_injuries

    repository = _nfl_repository()
    cache_root = _nflverse_cache_path()
    cache_file = cache_root / "espn_injuries.json"
    plan = availability_refresh_plan(
        repository.schedule(season),
        cache_modified_at=cache_file.stat().st_mtime if cache_file.exists() else None,
        now=now,
    )
    if not plan["due"]:
        print("nfl_availability: skipped " + json.dumps(plan, sort_keys=True))
        return plan
    count = sync_injuries(repository, cache_root, season, force=True)
    result = {**plan, "status": "refreshed", "injuries": count}
    print("nfl_availability: " + json.dumps(result, sort_keys=True))
    return result


#: See sync.py's CORE_FOUNDATION/CORE_STATS/CORE_DEPTH/CORE_PBP for what
#: each group actually covers and why it's split this way.
_CORE_SEGMENTS = ("core-foundation", "core-stats", "core-depth", "core-pbp")


#: History kept on disk. Without a bound the file grew by ~220 rows a day and is re-read by the
#: status page; this keeps roughly two months of real runs.
HISTORY_MAX_BYTES = 1_500_000
HISTORY_KEEP_ROWS = 4000
#: Segments queue behind each other rather than overlapping: the cron triggers fire
#: independently, a content run takes minutes, and two pandas-heavy children on one small
#: instance is how the whole service got OOM-killed before. A segment that cannot get the
#: lock within the wait is recorded as skipped and the next tick picks it up.
FAULT_DUMP_SECONDS = 300
LOCK_WAIT_SECONDS = 180.0
LOCK_STALE_SECONDS = 2 * 3600


def _state_dir() -> Path:
    """Where the history and lock live: beside the NFL database unless NFL_REFRESH_STATE_DIR
    says otherwise (the test suite points it at a temp directory so a test run cannot write
    fake runs into the real history)."""
    override = os.getenv("NFL_REFRESH_STATE_DIR", "").strip()
    return Path(override) if override else Path(_nfl_repository().path).parent


def _history_path() -> Path:
    return _state_dir() / "nfl_refresh_history.jsonl"


def _lock_path() -> Path:
    return _state_dir() / "nfl_refresh.lock"


def _trim_history(path: Path) -> None:
    try:
        if path.stat().st_size <= HISTORY_MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8").splitlines()[-HISTORY_KEEP_ROWS:]
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def _append_history(record: dict) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    _trim_history(path)


def _arm_faulthandler() -> bool:
    """Turn on stack dumps, unless stderr cannot take them. A diagnostic must never stop a refresh:
    faulthandler needs a real file descriptor and raises where there is none (a captured stream)."""
    try:
        faulthandler.enable()
        faulthandler.dump_traceback_later(FAULT_DUMP_SECONDS, repeat=True)
    except (OSError, ValueError, RuntimeError, AttributeError):   # io.UnsupportedOperation is an OSError
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "segment", choices=("rosters", *_CORE_SEGMENTS, "availability", "content", "pff", "history", "weather")
    )
    parser.add_argument("--season", type=int, required=True)
    args = parser.parse_args(argv)
    # A fatal signal (segfault, abort) prints the Python stack of every thread into the log, and a
    # run still going after FAULT_DUMP_SECONDS prints where it is, repeatedly: a hang names the line
    # it is stuck on instead of staying a bare "running".
    armed = _arm_faulthandler()
    try:
        return _main(args)
    finally:
        if armed:
            faulthandler.cancel_dump_traceback_later()


def _main(args) -> int:
    from sports_aggregator.process_probe import lock_with_wait
    started = datetime.now(timezone.utc)
    record = {
        "segment": args.segment,
        "season": int(args.season),
        "started_at": started.isoformat(),
        "status": "running",
        "pid": os.getpid(),
        "database_path": str(_nfl_repository().path),
    }
    lock = _lock_path()
    if not lock_with_wait(lock, stale_seconds=LOCK_STALE_SECONDS, wait_seconds=LOCK_WAIT_SECONDS):
        _append_history({**record, "status": "skipped", "reason": "another_nfl_refresh_running",
                         "finished_at": datetime.now(timezone.utc).isoformat(),
                         "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1)})
        print(f"{args.segment}: skipped, another NFL refresh holds {lock.name}")
        return 0
    try:
        return _run_segment(args, record, started)
    finally:
        lock.unlink(missing_ok=True)


def _run_segment(args, record: dict, started: datetime) -> int:
    _append_history(record)
    try:
        if args.segment == "rosters":
            _sync_rosters(args.season)
        elif args.segment == "availability":
            _sync_availability(args.season)
        elif args.segment in _CORE_SEGMENTS:
            from sports_aggregator.nfl import sync as sync_module
            group, include_pbp = {
                "core-foundation": (sync_module.CORE_FOUNDATION, False),
                "core-stats": (sync_module.CORE_STATS, False),
                "core-depth": (sync_module.CORE_DEPTH, False),
                "core-pbp": (sync_module.CORE_PBP, True),
            }[args.segment]
            succeeded = _sync_core(
                args.season, include_pbp=include_pbp, only=group,
                include_extras=(args.segment == "core-foundation"),
            )
            if not succeeded:
                # Returning here used to skip the history row altogether: only a raised exception
                # reached the handler that records "failed". Every core segment whose dataset failed
                # therefore left a bare "running" row, which the status page then labelled
                # "interrupted ... killed, usually by running out of memory".
                finished = datetime.now(timezone.utc)
                reasons = "; ".join(f"{name}: {message}" for name, message in _LAST_DATASET_FAILURES)
                _append_history({
                    **record, "status": "failed", "finished_at": finished.isoformat(),
                    "seconds": round((finished - started).total_seconds(), 1),
                    "error_type": "DatasetFailed",
                    "error": (reasons or "the dataset sync reported a failure")[:500],
                })
                return 1
        elif args.segment == "content":
            _sync_content(args.season)
        elif args.segment == "pff":
            # Rehydrate both the completed-season baseline and any current-season
            # snapshot that has been uploaded. A missing season is a no-op.
            _sync_pff(args.season - 1)
            _sync_pff(args.season)
        elif args.segment == "history":
            _sync_history(2010, args.season - 1, include_pbp=False)
        elif args.segment == "weather":
            _sync_weather(args.season)
    except Exception as exc:
        finished = datetime.now(timezone.utc)
        failed = {
            **record,
            "status": "failed",
            "finished_at": finished.isoformat(),
            "seconds": round((finished-started).total_seconds(), 1),
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:500],
            "traceback": traceback.format_exc(limit=8)[-4000:],
        }
        _append_history(failed)
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    finished = datetime.now(timezone.utc)
    _append_history({
        **record,
        "status": "success",
        "finished_at": finished.isoformat(),
        "seconds": round((finished-started).total_seconds(), 1),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
