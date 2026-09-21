"""CLI for the CFB points-per-drive model: python -m sports_aggregator.cfb.xpoints_cli ...

Had no path to the deployed database at all: game_projection.py's live
expected-points projection loads cfb_xpoints_model, and
live_margin_calibration.py reads cfb_xpoints_dataset directly for
elo_difference/core_margin/fpi_margin/recent-margin features -- both existed
in production only from a one-off manual fit, with nothing to keep either
current or to rebuild them if the database is ever reset.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.xpoints import build_dataset, fit_model


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="CFB points-per-drive model")
    parser.add_argument("command", choices=("refresh",))
    parser.add_argument("--from-year", type=int, default=2022)
    parser.add_argument("--to-year", type=int, default=None,
                        help="Defaults to the current calendar year.")
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    to_year = args.to_year or datetime.now(timezone.utc).year

    if args.command == "refresh":
        # Dataset is rebuilt with no bounds (every consumer wants every
        # season's trailing features current), but the fit stays a bounded
        # rolling window -- fit_model() requires explicit bounds by design,
        # unlike xdrives' unbounded "best model on everything" choice.
        dataset_result = build_dataset(repository)
        fit_result = fit_model(repository, from_season=args.from_year, to_season=to_year)
        print(json.dumps({"dataset": dataset_result, "model": {
            "model_version": fit_result["model_version"],
            "training_rows": fit_result["training_rows"],
            "from_season": fit_result["from_season"],
            "to_season": fit_result["to_season"],
        }}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
