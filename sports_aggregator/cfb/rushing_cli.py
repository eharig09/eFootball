"""CLI for CFBD per-attempt rushing detail: direction and rusher attribution."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.cfbd import CFBDClient, CFBDConfigurationError
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.rushing_plays import (
    coverage, sync_season, sync_week, team_season_rushing,
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CFBD rushing detail (direction, rusher attribution)")
    p.add_argument("command", choices=("sync", "coverage", "splits"))
    p.add_argument("--year", type=int, default=datetime.now().year)
    p.add_argument("--week", type=int, default=None,
                   help="One week. Omitted, sync walks the whole season.")
    p.add_argument("--team", default=None, help="For `splits`.")
    p.add_argument("--role", default="offense", choices=("offense", "defense"))
    p.add_argument("--force", action="store_true", help="Refetch instead of using the raw cache.")
    p.add_argument("--database", default=None)
    return p


def main(argv: list[str] | None = None, *, client=None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))

    if args.command == "coverage":
        print(json.dumps(coverage(repository, args.year), indent=2))
        return 0

    if args.command == "splits":
        if not args.team:
            raise SystemExit("--team is required for `splits`")
        print(json.dumps(
            team_season_rushing(repository, args.team, args.year, role=args.role), indent=2))
        return 0

    try:
        client = client or CFBDClient(
            raw_cache_path=os.getenv("CFBD_RAW_CACHE_PATH", "instance/cfbd_raw"))
    except CFBDConfigurationError as exc:
        raise SystemExit(str(exc)) from None

    if args.week is not None:
        report = sync_week(repository, client, season=args.year, week=args.week, force=args.force)
    else:
        report = sync_season(repository, client, season=args.year, force=args.force)
    print(json.dumps(report if args.week is not None
                     else {k: v for k, v in report.items() if k != "weeks"}, indent=2))
    return 1 if report.get("failures") else 0


if __name__ == "__main__":
    raise SystemExit(main())
