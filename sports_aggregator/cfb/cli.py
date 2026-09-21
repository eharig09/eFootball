"""Standalone CFBD sync/status command: python -m sports_aggregator.cfb.cli ..."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os

from typing import Any

from dotenv import load_dotenv

from sports_aggregator.cfb.cfbd import (
    FINISHED_WEEK_TTL, LIVE_WEEK_TTL, CFBDClient, CFBDConfigurationError)
from sports_aggregator.cfb.models import Game, Player
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.sync import CFBDataSync


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize college-football data from CFBD")
    parser.add_argument("command", choices=("sync", "status", "sync-player-stats",
                                           "sync-roster-context", "backfill",
                                           "sync-history",
                                           "sync-box-scores", "sync-game-ppa",
                                           "sync-promoted", "coverage", "sync-lines",
                                           "sync-venues", "sync-recruits",
                                           "link-transfer-grades"))
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--force", action="store_true", help="Bypass raw-response cache")
    parser.add_argument("--basic", action="store_true", help="Skip advanced stats and CORE ratings")
    parser.add_argument("--conference", default=None,
                        help="Limit sync-player-stats to one conference display name")
    parser.add_argument("--database", default=None, help="Override CFB_DATABASE_PATH")
    parser.add_argument("--from-year", type=int, default=None,
                        help="First season for backfill (inclusive)")
    parser.add_argument(
        "--recent-weeks", type=int, default=None, metavar="N",
        help="sync-box-scores: only the last N completed weeks. The current "
             "season's newest weeks are the ones that move; re-walking every "
             "week of a finished season costs hundreds of thousands of rows.")
    parser.add_argument("--to-year", type=int, default=None,
                        help="Last season for backfill (inclusive)")
    return parser


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    """`client` is a seam for tests: the real one needs a key and a network."""
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    database = args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    repository = CFBRepository(database)
    if args.command == "status":
        print(json.dumps(repository.status(args.year), indent=2, default=str))
        return 0

    client = client or CFBDClient(
        raw_cache_path=os.getenv("CFBD_RAW_CACHE_PATH", "instance/cfbd_raw"))
    if not client.configured:
        raise CFBDConfigurationError("CFBD_API_KEY is required for sync")
    if args.command == "sync-roster-context":
        prior_year = args.year - 1
        datasets = (
            ("prior_roster", lambda: repository.replace_players(
                prior_year, (Player.from_cfbd(item, prior_year) for item in client.roster(prior_year, args.force))
            )),
            ("transfers", lambda: repository.replace_transfers(
                args.year, client.transfers(args.year, args.force)
            )),
            ("draft_picks", lambda: repository.replace_draft_picks(
                args.year, client.draft_picks(args.year, args.force)
            )),
            ("returning_production", lambda: repository.replace_returning_production(
                args.year, client.returning_production(args.year, args.force)
            )),
        )
        failures = []
        for name, operation in datasets:
            try:
                print(f"{name}: success ({operation()})")
            except Exception as exc:
                failures.append(name); print(f"{name}: failed ({exc})")
        return 1 if failures else 0
    if args.command == "link-transfer-grades":
        report = repository.confirm_transfer_pff_links(args.year)
        print(" ".join(f"{key}={value}" for key, value in report.items()))
        return 0
    if args.command == "sync-recruits":
        print(f"recruits: {repository.replace_recruits(args.year, client.recruits(args.year, args.force))}")
        return 0
    if args.command == "sync-venues":
        print(f"venues: {repository.replace_venues(client.venues(args.force))}")
        return 0
    if args.command == "sync-lines":
        from sports_aggregator.cfb.lines import store_lines
        first = args.from_year or args.year
        last = args.to_year or args.year
        if first > last:
            parser.error("--from-year must not be after --to-year")
        total = 0
        for year in range(first, last + 1):
            # A finished season's quotes never move again, so they are worth
            # caching for as long as the box scores beside them.
            settled = year < datetime.now().year
            count = store_lines(repository, year, client.betting_lines(
                year, args.force,
                cache_ttl_seconds=FINISHED_WEEK_TTL if settled else 1800))
            print(f"game_lines: {count} provider quotes for {year}")
            total += count
        if first != last:
            print(f"sync-lines {first}-{last} complete; {total} quotes")
        return 0
    if args.command == "coverage":
        print(json.dumps(repository.stat_coverage(), indent=2, default=str))
        return 0
    if args.command == "sync-history":
        first = args.from_year or args.year
        last = args.to_year or args.year
        if first > last:
            parser.error("--from-year must not be after --to-year")
        failures = []
        for year in range(first, last + 1):
            jobs = (
                ("games", lambda year=year: repository.replace_games(
                    year, (Game.from_cfbd(item) for item in client.games(year, args.force)))),
                ("records", lambda year=year: repository.replace_records(
                    year, (item for item in client.records(year, args.force)
                           if item.get("classification") == "fbs"))),
                ("team_stats", lambda year=year: repository.replace_team_stats(
                    year, client.team_stats(year, args.force))),
                ("advanced_stats", lambda year=year: repository.replace_advanced_stats(
                    year, client.advanced_team_stats(year, args.force))),
                ("coaches", lambda year=year: repository.replace_coach_seasons(
                    year, client.coaches(year, args.force))),
            )
            for name, operation in jobs:
                if (year < datetime.now().year and not args.force
                        and repository.history_dataset_cached(year, name)):
                    print(f"{year} {name}: cached (SQLite)")
                    continue
                try:
                    count = operation()
                    repository.mark_history_dataset(year, name, count)
                    print(f"{year} {name}: success ({count})")
                except Exception as exc:
                    failures.append(f"{year} {name}")
                    print(f"{year} {name}: failed ({exc})")
        print(f"sync-history {first}-{last} complete; {len(failures)} dataset failures")
        return 1 if failures else 0
    if args.command == "sync-box-scores":
        first = args.from_year or args.year
        last = args.to_year or args.year
        if first > last:
            parser.error("--from-year must not be after --to-year")
        failures = []
        for year in range(first, last + 1):
            wanted = {
                name for name in ("team_box_scores", "player_box_scores")
                if args.force or year >= datetime.now().year
                or not repository.history_dataset_cached(year, name)
            }
            if not wanted:
                print(f"{year} box scores: cached (SQLite)")
                continue
            if args.force and not args.recent_weeks:
                # Clearing the season and then re-importing two weeks of it
                # would delete the rest, so a scoped pass never clears.
                repository.clear_box_scores(year)
            weeks = repository.completed_weeks(year)
            # A week that is over never changes again; one still being played
            # changes every few minutes. Only the recent ones are read live.
            live_weeks: set[int] = set()
            if args.recent_weeks:
                weeks = weeks[-args.recent_weeks:]
                if year >= datetime.now().year:
                    live_weeks = set(weeks)
            ttl = {week: (LIVE_WEEK_TTL if week in live_weeks else FINISHED_WEEK_TTL)
                   for week in weeks}
            dataset_failed = {name: False for name in wanted}
            for week in weeks:
                jobs = []
                if "team_box_scores" in wanted:
                    jobs.append(("team_box_scores", lambda week=week, year=year: repository.store_game_team_box_scores(
                        client.game_team_box_scores(
                            year, week, args.force, cache_ttl_seconds=ttl[week]))))
                if "player_box_scores" in wanted:
                    jobs.append(("player_box_scores", lambda week=week, year=year: repository.store_game_player_box_scores(
                        client.game_player_box_scores(
                            year, week, args.force, cache_ttl_seconds=ttl[week]))))
                for name, operation in jobs:
                    try:
                        print(f"{year} week {week} {name}: success ({operation()})")
                    except Exception as exc:
                        dataset_failed[name] = True
                        failures.append(f"{year} week {week} {name}")
                        print(f"{year} week {week} {name}: failed ({exc})")
            counts = repository.box_score_counts(year)
            for name in wanted:
                if not dataset_failed[name] and not args.recent_weeks:
                    # A scoped pass has not seen the whole season, so it must
                    # not tell a later full run that the season is complete.
                    repository.mark_history_dataset(year, name, counts[name])
        print(f"sync-box-scores {first}-{last} complete; {len(failures)} failures")
        return 1 if failures else 0
    if args.command == "sync-game-ppa":
        # /ppa/players/games has no game-id field and no combined "both"
        # seasonType (confirmed live), so each (season, week, season_type)
        # actually present in the games table gets its own call -- cheap in
        # volume (one call covers a whole week of FBS) but real in count, so
        # this walks only weeks that exist rather than a fixed 1-20 range.
        first = args.from_year or args.year
        last = args.to_year or args.year
        if first > last:
            parser.error("--from-year must not be after --to-year")
        failures = []
        total_rows = 0
        for year in range(first, last + 1):
            with_ttl = FINISHED_WEEK_TTL if year < datetime.now().year else LIVE_WEEK_TTL
            slates = repository.completed_week_season_types(year)
            if args.recent_weeks:
                slates = slates[-args.recent_weeks:]
            for season_type, week in slates:
                try:
                    n = repository.replace_game_player_ppa(
                        client.ppa_players_games(year, week, season_type, args.force))
                    total_rows += n
                    print(f"{year} {season_type} week {week} game-ppa: success ({n})")
                except Exception as exc:
                    failures.append(f"{year} {season_type} week {week}")
                    print(f"{year} {season_type} week {week} game-ppa: failed ({exc})")
        print(f"sync-game-ppa {first}-{last} complete; {total_rows} rows; {len(failures)} failures")
        return 1 if failures else 0
    if args.command == "sync-promoted":
        # A team promoted from FCS has no history in any FBS-filtered dataset.
        # Sacramento State and North Dakota State joined for 2026 and carried no
        # statistics at all, which made their pages look broken rather than new.
        # Their prior seasons are fetched from the conference they actually
        # played in, using the abbreviation CFBD indexes by.
        first = args.from_year or (args.year - 3)
        last = args.to_year or (args.year - 1)
        current = {row["school"] for row in repository.teams(limit=200)}
        failures = []
        for year in range(first, last + 1):
            try:
                # The FBS-only endpoint cannot see a team that was still FCS,
                # which is exactly the team this command exists to find.
                catalog = client.get("/teams", {"year": year},
                                     cache_ttl_seconds=604800, force=args.force)
            except Exception as exc:
                failures.append(f"{year} teams"); print(f"{year} teams: failed ({exc})"); continue
            prior = {
                str(item["school"]): item
                for item in catalog
                if str(item.get("school")) in current
                and str(item.get("classification") or "").lower() != "fbs"
            }
            if not prior:
                print(f"{year}: no promoted teams to backfill")
                continue
            abbreviations = {
                item.get("name"): item.get("abbreviation") or item.get("name")
                for item in client.conferences(year, args.force)
            }
            by_conference: dict[str, list[str]] = {}
            for school, item in prior.items():
                by_conference.setdefault(str(item.get("conference") or ""), []).append(school)
            for conference, schools in by_conference.items():
                api_conference = abbreviations.get(conference, conference)
                try:
                    rows = client.player_season_stats(year, api_conference, args.force)
                except Exception as exc:
                    failures.append(f"{year} {conference}")
                    print(f"{year} {conference} [{api_conference}]: failed ({exc})")
                    continue
                wanted = [row for row in rows if str(row.get("team")) in schools]
                count = repository.replace_promoted_stats(year, wanted, schools)
                print(f"{year} {conference} [{api_conference}]: {count} rows for "
                      f"{', '.join(sorted(schools))}")
        print(f"sync-promoted {first}-{last} complete; {len(failures)} failures")
        return 1 if failures else 0
    if args.command == "backfill":
        # Careers span roughly five seasons, so a player who is a senior now was a
        # freshman well outside the current-season window. Backfilling rosters and
        # production together is what makes a full career visible on a player page.
        first = args.from_year or (args.year - 7)
        last = args.to_year or (args.year - 1)
        if first > last:
            parser.error("--from-year must not be after --to-year")
        catalog_year = last
        conferences = [item["conference"] for item in repository.conferences()]
        failures = []
        for year in range(first, last + 1):
            try:
                count = repository.replace_players(
                    year, (Player.from_cfbd(item, year)
                           for item in client.roster(year, args.force)))
                print(f"{year} roster: success ({count})")
            except Exception as exc:
                failures.append(f"{year} roster"); print(f"{year} roster: failed ({exc})")
            try:
                catalog = {
                    item["name"]: item.get("abbreviation") or item["name"]
                    for item in client.conferences(catalog_year, args.force)
                    if item.get("classification") == "fbs"
                }
            except Exception as exc:
                catalog = {}
                print(f"{year} conference catalog unavailable ({exc}); using display names")
            total = 0
            for conference in conferences:
                try:
                    rows = client.player_season_stats(
                        year, catalog.get(conference, conference), args.force)
                    total += repository.replace_player_stats(year, rows, conference)
                except Exception as exc:
                    failures.append(f"{year} {conference}")
                    print(f"{year} {conference}: failed ({exc})")
            print(f"{year} player stats: {total} rows")
        print(f"backfill {first}-{last} complete; {len(failures)} dataset failures")
        return 1 if failures else 0
    if args.command == "sync-player-stats":
        conferences=[item["conference"] for item in repository.conferences()]
        if args.conference:
            conferences = [
                name for name in conferences if name.casefold() == args.conference.casefold()
            ]
            if not conferences:
                parser.error(f"Unknown synchronized conference: {args.conference}")
        catalog = {
            item["name"]: item.get("abbreviation") or item["name"]
            for item in client.conferences(args.year, args.force)
            if item.get("classification") == "fbs"
        }
        total=0; failures=[]
        for conference in conferences:
            try:
                api_conference = catalog.get(conference, conference)
                rows=client.player_season_stats(args.year,api_conference,args.force)
                count=repository.replace_player_stats(args.year,rows,conference); total+=count
                print(f"{conference} [{api_conference}]: success ({count})")
            except Exception as exc:
                failures.append(conference); print(f"{conference}: failed ({exc})")
        print(f"player_stats: {total} rows across {len(conferences)-len(failures)} conferences")
        return 1 if failures else 0
    report = CFBDataSync(client, repository).sync(
        args.year, force=args.force, include_advanced=not args.basic
    )
    for dataset in report.datasets:
        suffix = f" — {dataset.message}" if dataset.message else ""
        print(f"{dataset.dataset}: {dataset.status} ({dataset.count}){suffix}")
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
