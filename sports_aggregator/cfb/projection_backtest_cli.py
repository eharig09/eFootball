"""CLI for full-chain, walk-forward CFB projection backtesting."""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.projection_backtest import (
    BACKTEST_VERSION, build, prepare_actuals, report, source_coverage,
)
from sports_aggregator.cfb.historical_coverage import audit as historical_coverage_audit, export_report as export_historical_coverage
from sports_aggregator.cfb.backfill_readiness import readiness as historical_backfill_readiness
from sports_aggregator.cfb.repository import CFBRepository


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Walk-forward backtest of the live CFB projection")
    p.add_argument("command", choices=("prepare", "coverage", "coverage-audit", "backfill-readiness", "run", "report"))
    p.add_argument("--from-year", type=int, default=None)
    p.add_argument("--to-year", type=int, default=None)
    p.add_argument("--year", type=int, default=None,
                   help="Shortcut for --from-year YEAR --to-year YEAR.")
    p.add_argument("--min-prior-games", type=int, default=1)
    p.add_argument("--points-train-from-year", type=int, default=2022,
                   help="Earliest season allowed into each prior-season xPoints fold.")
    p.add_argument("--backtest-version", default=BACKTEST_VERSION)
    p.add_argument("--database", default=None)
    p.add_argument("--output-dir", default="research_outputs",
                   help="Directory for coverage-audit artifacts.")
    return p


def _years(args, *, required: bool) -> tuple[int | None, int | None]:
    if args.year is not None:
        return args.year, args.year
    first, last = args.from_year, args.to_year
    if required and (first is None or last is None):
        raise SystemExit("run requires --year or both --from-year and --to-year")
    if first is not None and last is not None and first > last:
        raise SystemExit("--from-year must not be after --to-year")
    return first, last


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    first, last = _years(args, required=args.command in {"prepare", "coverage", "coverage-audit", "backfill-readiness", "run"})
    if args.command == "prepare":
        payload = prepare_actuals(
            repository, from_season=int(first), to_season=int(last))
    elif args.command == "coverage":
        payload = source_coverage(
            repository, from_season=int(first), to_season=int(last))
    elif args.command == "coverage-audit":
        payload = historical_coverage_audit(
            repository, from_season=int(first), to_season=int(last))
        payload["files"] = export_historical_coverage(payload, args.output_dir)
    elif args.command == "backfill-readiness":
        if int(first) != int(last):
            raise SystemExit("backfill-readiness requires exactly one --year")
        payload = historical_backfill_readiness(repository, season=int(first))
    elif args.command == "run":
        payload = build(
            repository, from_season=int(first), to_season=int(last),
            backtest_version=args.backtest_version,
            min_prior_games=args.min_prior_games,
            points_train_from_season=args.points_train_from_year,
        )
    else:
        payload = report(
            repository, from_season=first, to_season=last,
            backtest_version=args.backtest_version,
            min_prior_games=args.min_prior_games,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
