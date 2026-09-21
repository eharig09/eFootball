"""CLI for the CFB red-zone shrinkage dataset: python -m sports_aggregator.cfb.xredzone_cli ...

Had no path to the deployed database at all: live_margin_calibration.py's
red_zone_diff feature (and game_projection.py's own red-zone shrinkage) read
cfb_xredzone_dataset directly, but nothing ever built it in production --
500ing every CFB game page (_historical_rows() now guards a missing table,
but the fix here is giving this a real, scheduled path to the database).
"""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.xredzone import build_dataset


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="CFB red-zone shrinkage dataset")
    parser.add_argument("command", choices=("refresh",))
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))

    if args.command == "refresh":
        # No season bounds: this is a lookup table (shrunk trailing rates),
        # not a fitted model -- every consumer wants it current for every
        # season on record, not a held-out slice.
        result = build_dataset(repository)
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
