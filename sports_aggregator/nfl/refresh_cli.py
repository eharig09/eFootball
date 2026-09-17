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
import json
from pathlib import Path
import sys

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
    for dataset in report.datasets:
        print(f"{dataset.dataset}: {dataset.status} ({dataset.count})")
    if include_extras:
        from sports_aggregator.nfl.source_directory import DEFAULT_PATH, import_directory
        _sync_espn_context(repository, season, force=force)
        # Normally a fresh empty disk's fast path handles this (see
        # production_seed.restore_public_seed); repeated here so a disk
        # that already has core data but never got the registry seeded
        # still gets it.
        if DEFAULT_PATH.exists():
            sources = import_directory(_source_registry(), DEFAULT_PATH)
            print(f"nfl_sources: success ({sources})")
    return report.succeeded


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

    sources = _source_registry().list_league_sources("nfl", limit=158)
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


#: See sync.py's CORE_FOUNDATION/CORE_STATS/CORE_DEPTH/CORE_PBP for what
#: each group actually covers and why it's split this way.
_CORE_SEGMENTS = ("core-foundation", "core-stats", "core-depth", "core-pbp")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "segment", choices=("rosters", *_CORE_SEGMENTS, "content", "pff", "history", "weather")
    )
    parser.add_argument("--season", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.segment == "rosters":
            _sync_rosters(args.season)
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
                return 1
        elif args.segment == "content":
            _sync_content(args.season)
        elif args.segment == "pff":
            # Completed-season PFF is the stable baseline until a current export lands.
            _sync_pff(args.season - 1)
        elif args.segment == "history":
            _sync_history(2010, args.season - 1, include_pbp=False)
        elif args.segment == "weather":
            _sync_weather(args.season)
    except Exception as exc:
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
