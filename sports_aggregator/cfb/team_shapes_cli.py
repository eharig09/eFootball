"""CLI for leak-safe CFB team-shape profiling."""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb import team_shapes


def main() -> None:
    parser = argparse.ArgumentParser(description="CFB team-shape matchup analysis")
    parser.add_argument("--db", default=None, help="Optional SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build")
    build.add_argument("--from-year", type=int, required=True)
    build.add_argument("--to-year", type=int, required=True)
    build.add_argument("--min-prior-games", type=int, default=1)

    report = sub.add_parser("report")
    report.add_argument("--year", type=int, required=True)
    report.add_argument("--clusters", type=int, default=8)
    report.add_argument("--neighbors", type=int, default=25)
    report.add_argument("--min-prior-games", type=int, default=3)

    load_dotenv()
    args = parser.parse_args()
    repository = CFBRepository(
        args.db or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))

    if args.command == "build":
        result = team_shapes.build(
            repository,
            from_season=args.from_year,
            to_season=args.to_year,
            min_prior_games=args.min_prior_games,
        )
    else:
        result = team_shapes.report(
            repository,
            test_season=args.year,
            clusters=args.clusters,
            neighbors=args.neighbors,
            min_prior_games=args.min_prior_games,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
