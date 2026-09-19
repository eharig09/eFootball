"""CLI for narrative-state and Line-Elo research."""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb import narrative_shapes


def main() -> None:
    parser = argparse.ArgumentParser(description="CFB narrative shape / Line Elo research")
    parser.add_argument("--db", default=None, help="Optional SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build")
    build.add_argument("--from-year", type=int, default=2022)
    build.add_argument("--to-year", type=int, default=2025)
    build.add_argument("--home-field-points", type=float, default=2.5)
    build.add_argument("--line-learning-rate", type=float, default=0.35)

    report = sub.add_parser("report")
    report.add_argument("--year", type=int, default=2025)
    report.add_argument("--min-train-rows", type=int, default=30)
    report.add_argument("--min-test-rows", type=int, default=12)

    load_dotenv()
    args = parser.parse_args()
    repository = CFBRepository(
        args.db or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))

    if args.command == "build":
        payload = narrative_shapes.build(
            repository,
            from_season=args.from_year,
            to_season=args.to_year,
            home_field_points=args.home_field_points,
            line_learning_rate=args.line_learning_rate,
        )
    else:
        payload = narrative_shapes.report(
            repository,
            test_season=args.year,
            min_train_rows=args.min_train_rows,
            min_test_rows=args.min_test_rows,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
