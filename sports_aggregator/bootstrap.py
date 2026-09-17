from __future__ import annotations
"""One entry point for building, refreshing, and repairing the CFB data store.

Phases:
* initial  -- complete normal app update: structured data, live ingestion and
              downstream processing; never historical player-stat backfills.
* refresh  -- current-season/live update; never prior-season history.
* history  -- historical games/team/coach context plus player-stat backfill,
              isolated one season per process.
* status   -- compact freshness/coverage report.
* plan     -- show the steps without running them.

Both initial and refresh are intentionally insulated from historical player-stat
work. A slow CFBD historical endpoint must never block betting lines, models,
current games, weather, reporting, or the rest of the normal update workflow.
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
from typing import Any

from dotenv import load_dotenv


#: Earliest season results, coaches and lines are filled back to.
#:
#: A fixed floor rather than a rolling window: it is the point a coach's record
#: against the number can be counted from, and that should not shorten by a
#: year every September. Kirby Smart took Georgia in 2016 and the records were
#: reading "2019 or earlier" because 2019 was as far back as anything went.
RESULT_HISTORY_FLOOR = 2015

#: Seasons of box scores and player history, which are the expensive ones.
DETAIL_HISTORY_SEASONS = 7

#: Address-space ceiling for NFL segments. History here matters, because the
#: obvious-looking fix is wrong:
#:   - 200MB, then 380MB: both failed "nfl-core" fast (<1s) with an OpenBLAS
#:     allocation error at a *resident* peak of only 44-94MB -- nowhere near
#:     either ceiling, since RLIMIT_AS bounds virtual address space, not
#:     RSS, and just booting the full combined app reserves more of that
#:     than it seems like it should.
#:   - 0 (no ceiling): removed on the theory that a tiny real working set
#:     meant the ceiling was pure downside. This was wrong -- live testing
#:     immediately took the whole 512MB instance down with a platform-level
#:     OOM kill (Render event: "Ran out of memory (used over 512MB)").
#:     Once nothing stopped it, the process's real appetite turned out to
#:     be much larger than the RLIMIT_AS failures ever revealed, likely the
#:     PBP/snap-count/depth-chart processing this step actually does.
#: 380 is the largest value proven NOT to crash the instance (it fails the
#: step alone instead), so that is where this sits until nfl-core's own
#: memory footprint is reduced (chunking the PBP work, or splitting it into
#: its own step) enough to actually complete under a safe ceiling -- raising
#: this number back toward "no limit" is not a safe way to get there.
NFL_CHILD_MEMORY_MB = 380


@dataclass
class Step:
    name: str
    description: str
    command: list[str]
    phases: tuple[str, ...]
    optional: bool = False
    requires_env: tuple[str, ...] = field(default_factory=tuple)
    requires_all_env: tuple[str, ...] = field(default_factory=tuple)
    #: Seconds this step gets before the driver kills it. None uses the
    #: driver's own timeout, which is a backstop rather than a budget.
    timeout_seconds: float | None = None
    #: Address-space ceiling (MB) for this step's child process. None uses
    #: the driver's CFB_REFRESH_CHILD_MB default; 0 would disable the
    #: ceiling entirely -- do not do this for a step that boots the full
    #: app (see NFL_CHILD_MEMORY_MB's history for why that took the whole
    #: instance down instead of just failing the one step).
    memory_mb: int | None = None


def steps(season: int, *, history_from: int | None = None,
          history_to: int | None = None) -> list[Step]:
    year = str(season)
    plan: list[Step] = [
        Step("cfbd-sync", "Current teams, roster, games, lines, records, rankings and stats",
             ["sports_aggregator.cfb.cli", "sync", "--year", year],
             ("initial", "refresh"), requires_env=("CFBD_API_KEY",)),
        # Prioritize the reader-facing news wire before the more expensive
        # model/media work. On a memory-constrained host a later optional step
        # may degrade, but the article stream should still be current.
        Step("social-seed", "Seed curated reporting/source registry",
             ["sports_aggregator.social.cli", "seed"], ("initial",), optional=True),
        Step("social-prepare", "Prepare curated social sources",
             ["sports_aggregator.social.cli", "prepare"], ("initial",), optional=True),
        Step("articles", "RSS reporting",
             ["sports_aggregator.social.content_cli", "ingest-reporting", "--season", year],
             ("initial", "refresh"), optional=True),
        # NFL shares this plan and lock, but each segment exits before the next
        # begins. That preserves the low-memory, one-child-at-a-time contract.
        Step("nfl-rosters", "Current NFL roster release",
             ["sports_aggregator.nfl.refresh_cli", "rosters", "--season", year],
             ("initial", "refresh"), optional=True, timeout_seconds=900, memory_mb=NFL_CHILD_MEMORY_MB),
        Step("nfl-content", "NFL reporting and curated Bluesky feeds",
             ["sports_aggregator.nfl.refresh_cli", "content", "--season", year],
             ("initial", "refresh"), optional=True, timeout_seconds=900, memory_mb=NFL_CHILD_MEMORY_MB),
        Step("nfl-core", "NFL schedules, stats, snaps, depth and play-by-play analytics",
             ["sports_aggregator.nfl.refresh_cli", "core", "--season", year],
             ("initial", "refresh"), optional=True, timeout_seconds=1800, memory_mb=NFL_CHILD_MEMORY_MB),
        Step("nfl-pff", "Read-only completed-season NFL PFF baseline",
             ["sports_aggregator.nfl.refresh_cli", "pff", "--season", year],
             ("initial", "refresh"), optional=True, timeout_seconds=900, memory_mb=NFL_CHILD_MEMORY_MB),
        Step("cfbd-current-player-stats", "Current-season player production by conference",
             ["sports_aggregator.cfb.cli", "sync-player-stats", "--year", year],
             ("initial", "refresh"), optional=True, requires_env=("CFBD_API_KEY",)),
        Step("cfbd-models", "CFBD CORE ratings and ESPN FPI model data",
             ["sports_aggregator.cfb.models_cli", "sync", "--year", year],
             ("initial", "refresh"), requires_env=("CFBD_API_KEY",)),
        # Box scores were only ever synced by the history phase, for seasons
        # that were already over. Nothing refreshed the current one, so a game
        # played this season had an empty box score page and never joined the
        # opponent-history record. Scoped to the weeks still moving, because a
        # full season is most of half a million rows.
        Step("cfbd-box-scores", "Box scores for the weeks just played",
             ["sports_aggregator.cfb.cli", "sync-box-scores", "--year", year,
              "--recent-weeks", "2"],
             ("initial", "refresh"), requires_env=("CFBD_API_KEY",)),
        Step("cfbd-lines", "Fast market-only betting-line refresh",
             ["sports_aggregator.cfb.cli", "sync-lines", "--year", year, "--force"],
             ("initial", "refresh"), requires_env=("CFBD_API_KEY",)),
        Step("cfbd-venues", "Stadium coordinates, elevation and domes",
             ["sports_aggregator.cfb.cli", "sync-venues", "--year", year],
             ("initial",), requires_env=("CFBD_API_KEY",)),
        Step("cfbd-roster-context", "Prior roster, portal, draft and returning production",
             ["sports_aggregator.cfb.cli", "sync-roster-context", "--year", year],
             ("initial", "refresh"), requires_env=("CFBD_API_KEY",)),
        Step("cfbd-prior-history", "Prior-season games, records, team stats and coaches",
             ["sports_aggregator.cfb.cli", "sync-history", "--year", str(season - 1)],
             ("initial",), requires_env=("CFBD_API_KEY",)),
        Step("cfbd-prior-player-stats", "Prior-season roster and production baseline",
             ["sports_aggregator.cfb.history_cli", "--year", str(season - 1)],
             ("initial",), requires_env=("CFBD_API_KEY",)),
        Step("cfbd-promoted", "Prior-classification history for promoted teams",
             ["sports_aggregator.cfb.cli", "sync-promoted", "--year", year],
             ("initial",), requires_env=("CFBD_API_KEY",)),
        Step("cfbd-recruits", "Signing class ratings",
             ["sports_aggregator.cfb.cli", "sync-recruits", "--year", year],
             ("initial", "refresh"), requires_env=("CFBD_API_KEY",)),
        Step("pff", "Import local PFF snapshot",
             ["sports_aggregator.cfb.pff_cli", "import", "--season", str(season - 1),
              "--roster-season", year, "--directory", "PFF"],
             ("initial",), optional=True),
        Step("transfer-grades", "Confirm PFF identities using portal evidence",
             ["sports_aggregator.cfb.cli", "link-transfer-grades", "--year", year],
             ("initial", "refresh")),
        # Nothing has ever run this. `coordinator_seasons` was empty, so the
        # matchup's run/pass section returned None for every team and removed
        # itself from the page, and there was no name to attribute a tempo or a
        # tendency to. Wikipedia needs no key, which is why this asks for no
        # environment; optional because one encyclopedia being unreachable is
        # not a reason to fail a refresh.
        Step("coordinators", "Offensive and defensive coordinators",
             ["sports_aggregator.cfb.coordinator_cli", "--year", year, "--source", "auto"],
             ("initial", "refresh"), optional=True, timeout_seconds=600),
        Step("prospects", "Import consensus NFL draft board",
             ["sports_aggregator.cfb.prospects_cli", "2027_nfl_mock_draft_database_top_100.csv",
              "--draft-year", str(season + 1), "--roster-season", year,
              "--source", "mock_draft_database_consensus"],
             ("initial",), optional=True),
        Step("weather", "Kickoff weather forecasts from Open-Meteo",
             ["sports_aggregator.cfb.external_cli", "weather", "--season", year],
             ("initial", "refresh"), optional=True),
        Step("bluesky-resolve", "Resolve and verify curated Bluesky handles",
             ["sports_aggregator.social.cli", "resolve"],
             ("initial", "refresh"), optional=True),
        Step("reddit-validate", "Validate configured subreddit endpoints",
             ["sports_aggregator.social.cli", "validate-reddit"],
             ("initial", "refresh"), optional=True,
             requires_all_env=("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")),
        Step("media-seed", "Import versioned podcast and YouTube research catalog",
             ["sports_aggregator.social.media_cli", "seed"],
             ("initial", "refresh"), optional=True),
        Step("media-validate", "Validate/promote YouTube and podcast endpoints",
             ["sports_aggregator.social.media_cli", "validate-all"],
             ("initial", "refresh"), optional=True),
        Step("bluesky", "Curated Bluesky author feeds",
             ["sports_aggregator.social.content_cli", "ingest", "--season", year],
             ("initial", "refresh"), optional=True),
        Step("reddit", "Curated subreddit submissions",
             ["sports_aggregator.social.content_cli", "ingest-reddit", "--season", year],
             ("initial", "refresh"), optional=True,
             requires_all_env=("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")),
        Step("youtube", "Verified channel uploads",
             ["sports_aggregator.social.content_cli", "ingest-youtube", "--season", year],
             ("initial", "refresh"), optional=True,
             requires_env=("YOUTUBE_API_KEY", "YOUTUBE_API")),
        Step("podcasts", "Verified podcast feeds",
             ["sports_aggregator.social.content_cli", "ingest-podcasts", "--season", year],
             ("initial", "refresh"), optional=True),
        # It stops itself at its own deadline; this is only the backstop for a
        # step that has stopped responding altogether. Before both existed it
        # ran to the driver's thirty minutes and stored nothing.
        Step("local-articles", "Verified team-scoped local reporting",
             ["sports_aggregator.social.content_cli", "ingest-local-reporting",
              "--season", year, "--limit", "15"],
             ("initial", "refresh"), optional=True, timeout_seconds=600),
        Step("retag", "Classify CFB eligibility, then re-resolve current entities",
             ["sports_aggregator.social.content_cli", "retag", "--season", year],
             ("initial", "refresh"), optional=True),
        Step("cluster", "Cross-source story clustering",
             ["sports_aggregator.social.content_cli", "cluster"],
             ("initial", "refresh"), optional=True),
        # Determination reads the stored text plus the item's position in its
        # cluster, so it follows clustering; relevance weights the role, so it
        # precedes scoring. Without this step every newly ingested item kept the
        # ingestion-time REPORTING_UNDETERMINED placeholder and carried no
        # evidence, which is what made the on-page explanations empty.
        Step("roles", "Determine source role and record the evidence",
             ["sports_aggregator.social.content_cli", "roles"],
             ("initial", "refresh"), optional=True),
        Step("score", "Relevance scoring",
             ["sports_aggregator.social.content_cli", "score"],
             ("initial", "refresh"), optional=True),
        # Freeze what the app knew before kickoff for games inside the next 24h.
        # capture_due used to ride along on the tail of `cfb.cli sync`, but the
        # scheduled refresh runs the CFBD datasets one subprocess at a time and
        # never carried that enrichment over -- so "Expectation vs reality" was
        # empty on every game, and a stage missed here cannot be recaptured once
        # the game starts. Reads only stored data, so it needs no API key, but it
        # must land after cfbd-sync / cfbd-lines / weather.
        Step("pregame-snapshot", "Freeze pregame market, model and roster state for upcoming games",
             ["sports_aggregator.cfb.intelligence_cli", "snapshot", "--season", year],
             ("initial", "refresh"), optional=True, timeout_seconds=900),
        # The analytics layer, which had no scheduled step at all. Every model
        # the postgame report reads -- EPA, win probability, pace, turning
        # points, tendencies -- is built from these, and they were only ever run
        # by hand, so rebuilding a database silently emptied all of it with
        # nothing to notice or restore it.
        Step("pbp", "Play-by-play for the weeks just played",
             ["sports_aggregator.cfb.pbp_cli", "backfill", "--year", year],
             ("initial", "refresh"), optional=True,
             requires_env=("CFBD_API_KEY",), timeout_seconds=1800),
        Step("pbp-derive", "Per-play and per-drive metrics from stored plays",
             ["sports_aggregator.cfb.pbp_cli", "derive", "--year", year],
             ("initial", "refresh"), optional=True, timeout_seconds=1800),
        # Scored against the stored ep-v2 model rather than refitted: a fit
        # wants several seasons and does not change week to week.
        Step("epa", "Score plays with the event-aligned ep-v2 model",
             ["sports_aggregator.cfb.expected_points_event_cli", "score",
              "--from-year", year, "--to-year", year],
             ("initial", "refresh"), optional=True, timeout_seconds=1800),
        # Same story as the rest of this block: a CLI that was run by hand and
        # nothing else. `play-detail` parses rush/pass direction, depth and
        # catch-spot air yards out of the play text; `build-tendencies` rolls
        # those up per team-game against ep-v2. Without them "Middle of the
        # field" renders every split as "0 plays" and the postgame "Play
        # tendencies" section is empty.
        Step("play-detail", "Parse rush/pass direction, depth and air yards from play text",
             ["sports_aggregator.cfb.play_detail_cli", "build", "--year", year],
             ("initial", "refresh"), optional=True, timeout_seconds=1800),
        Step("build-tendencies", "Team-game run/pass tendency splits the reports read",
             ["sports_aggregator.cfb.play_detail_cli", "build-tendencies",
              "--year", year, "--model-version", "ep-v2"],
             ("initial", "refresh"), optional=True, timeout_seconds=900),
        Step("team-advanced", "Team-game efficiency the report and matchup read",
             ["sports_aggregator.cfb.pbp_cli", "build-team-advanced",
              "--from-year", year, "--to-year", year, "--model-version", "ep-v2"],
             ("initial", "refresh"), optional=True, timeout_seconds=900),
        Step("win-probability", "Score win probability for leverage and turning points",
             ["sports_aggregator.cfb.pbp_cli", "score-wp-v2",
              "--from-year", year, "--to-year", year],
             ("initial", "refresh"), optional=True, timeout_seconds=1800),
        Step("passing-detail", "Pass direction, depth, air yards and YAC per attempt",
             ["sports_aggregator.cfb.passing_cli", "sync", "--year", year],
             ("initial", "refresh"), optional=True,
             requires_env=("CFBD_API_KEY",), timeout_seconds=1200),
        Step("passing-qb", "Quarterback air-yard summaries from measured attempts",
             ["sports_aggregator.cfb.passing_cli", "build-qb", "--year", year],
             ("initial", "refresh"), optional=True, timeout_seconds=600),
        Step("rushing-detail", "Rush direction and rusher attribution per attempt",
             ["sports_aggregator.cfb.rushing_cli", "sync", "--year", year],
             ("initial", "refresh"), optional=True,
             requires_env=("CFBD_API_KEY",), timeout_seconds=1200),
    ]

    # Two horizons, because the two kinds of history cost wildly different
    # amounts. Results, coaches and lines are a few thousand rows a season and
    # are what a coach's record against the number is counted from, so they
    # reach back to RESULT_HISTORY_FLOOR. Box scores and player history are
    # closer to half a million rows a season, so they stay a rolling window --
    # extending those to 2015 would have added roughly two million rows to a
    # database already asking 1.7 GB of a 5 GB disk.
    first_results = history_from if history_from is not None else min(
        RESULT_HISTORY_FLOOR, season - DETAIL_HISTORY_SEASONS)
    first_detail = history_from if history_from is not None else (
        season - DETAIL_HISTORY_SEASONS)
    last_history = history_to if history_to is not None else season - 1

    for historical_year in range(first_results, last_history + 1):
        plan.append(Step(
            f"game-history-{historical_year}",
            f"Games, records, team stats and coaches for {historical_year}",
            ["sports_aggregator.cfb.cli", "sync-history", "--year", str(historical_year)],
            ("history",), requires_env=("CFBD_API_KEY",),
        ))
        # Without this a fresh install has lines for the current season only,
        # and the against-the-number records beside them have nothing to count.
        plan.append(Step(
            f"lines-history-{historical_year}",
            f"Closing betting lines for {historical_year}",
            ["sports_aggregator.cfb.cli", "sync-lines", "--year", str(historical_year)],
            ("history",), requires_env=("CFBD_API_KEY",),
        ))
        # In this loop rather than the detail one below: a coordinator's career
        # is the point of the measurement, so it has to reach as far back as
        # the tendencies it explains, and `team_stats` carries the rushing and
        # passing attempts from RESULT_HISTORY_FLOOR. The detail window is
        # seven seasons, which would have stopped at 2019.
        plan.append(Step(
            f"coordinators-history-{historical_year}",
            f"Coordinators for {historical_year}",
            ["sports_aggregator.cfb.coordinator_cli", "--year", str(historical_year)],
            ("history",), optional=True, timeout_seconds=600,
        ))

    for historical_year in range(first_detail, last_history + 1):
        plan.append(Step(
            f"history-{historical_year}",
            f"Player statistics and roster history for {historical_year}",
            ["sports_aggregator.cfb.history_cli", "--year", str(historical_year)],
            ("history",), requires_env=("CFBD_API_KEY",),
        ))
        # Recruiting was synced for the current season only, so a player who
        # signed in any earlier class had no pedigree on his page -- which is
        # most of a roster. The detail window is the right one: a class stops
        # mattering once nobody from it is still playing.
        plan.append(Step(
            f"recruits-history-{historical_year}",
            f"Signing class ratings for {historical_year}",
            ["sports_aggregator.cfb.cli", "sync-recruits", "--year", str(historical_year)],
            ("history",), optional=True, requires_env=("CFBD_API_KEY",),
        ))
        plan.append(Step(
            f"box-history-{historical_year}",
            f"Cached team and player box scores for {historical_year}",
            ["sports_aggregator.cfb.cli", "sync-box-scores", "--year", str(historical_year)],
            ("history",), requires_env=("CFBD_API_KEY",),
        ))
    return plan


def _env_satisfied(step: Step) -> bool:
    alternatives_ok = (not step.requires_env or
                       any((os.getenv(name) or "").strip() for name in step.requires_env))
    required_ok = all((os.getenv(name) or "").strip() for name in step.requires_all_env)
    return alternatives_ok and required_ok


def run_step(step: Step, *, timeout: int = 1800) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    if not _env_satisfied(step):
        requirements = list(step.requires_all_env)
        if step.requires_env:
            requirements.append("one of " + ", ".join(step.requires_env))
        return {"step": step.name, "status": "skipped",
                "message": f"needs {', '.join(requirements)}", "seconds": 0.0}
    try:
        completed = subprocess.run([sys.executable, "-m", *step.command],
                                   capture_output=True, text=True, timeout=timeout)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        output = (completed.stdout or completed.stderr or "").strip().splitlines()
        return {"step": step.name,
                "status": "success" if completed.returncode == 0 else "failed",
                "message": output[-1][:240] if output else "", "seconds": round(elapsed, 1)}
    except subprocess.TimeoutExpired:
        return {"step": step.name, "status": "timeout",
                "message": f"exceeded {timeout}s", "seconds": float(timeout)}
    except Exception as exc:
        return {"step": step.name, "status": "failed",
                "message": str(exc)[:240], "seconds": 0.0}


def run_phase(phase: str, season: int, *, only: list[str] | None = None,
              timeout: int = 1800, history_from: int | None = None,
              history_to: int | None = None) -> list[dict[str, Any]]:
    plan = [step for step in steps(
        season, history_from=history_from, history_to=history_to) if phase in step.phases]
    if only:
        wanted = set(only)
        plan = [step for step in plan if step.name in wanted]
    results = []
    for step in plan:
        print(f"[ ] {step.name}: {step.description}")
        result = run_step(step, timeout=timeout)
        result["optional"] = step.optional
        marker = {"success": "[ok]", "skipped": "[--]"}.get(result["status"], "[!!]")
        print(f"{marker} {result['status']} ({result['seconds']}s) {result['message']}")
        results.append(result)
    return results


def status_report(season: int) -> dict[str, Any]:
    from sports_aggregator.cfb.external import import_status
    from sports_aggregator.cfb.repository import CFBRepository
    from sports_aggregator.social.content import ContentRepository

    database = os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    repository = CFBRepository(database)
    report: dict[str, Any] = {"season": season, "database": database}
    for name, produce in (
        ("cfbd", lambda: repository.status(season)),
        ("stat_coverage", lambda: repository.stat_coverage()),
        ("history_coverage", lambda: repository.history_coverage()),
        ("secondary", lambda: import_status(repository, limit=12)),
        ("content", lambda: ContentRepository(database).summary()),
    ):
        try:
            report[name] = produce()
        except Exception as exc:
            report[name] = {"error": str(exc)[:200]}
    return report


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Build and refresh the CFB Intelligence data store")
    parser.add_argument("command", choices=("initial", "refresh", "history", "status", "plan"))
    parser.add_argument("--season", type=int, default=datetime.now().year)
    parser.add_argument("--only", nargs="*", default=None,
                        help="Run only named steps, e.g. history-2021 history-2022")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Per-step timeout in seconds; history also enforces a shorter timeout per conference")
    parser.add_argument("--from-year", type=int, default=None,
                        help="First historical season to add (history/plan only)")
    parser.add_argument("--to-year", type=int, default=None,
                        help="Last historical season to add (history/plan only)")
    args = parser.parse_args(argv)

    if args.command == "status":
        print(json.dumps(status_report(args.season), indent=2, default=str))
        return 0
    if args.command == "plan":
        for step in steps(args.season, history_from=args.from_year, history_to=args.to_year):
            phases = ",".join(step.phases)
            flag = " (optional)" if step.optional else ""
            print(f"  {step.name:22s} [{phases:15s}] {step.description}{flag}")
        return 0

    if args.from_year is not None and args.to_year is not None and args.from_year > args.to_year:
        parser.error("--from-year must not be after --to-year")
    results = run_phase(args.command, args.season, only=args.only, timeout=args.timeout,
                        history_from=args.from_year, history_to=args.to_year)
    failures = [row for row in results
                if row["status"] not in ("success", "skipped") and not row["optional"]]
    print(f"\n{args.command}: {len(results)} steps, {len(failures)} failed")
    for row in failures:
        print(f"   FAILED {row['step']}: {row['message']}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
