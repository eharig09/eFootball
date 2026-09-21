"""Freeze a pregame two-engine manifest before the selected week's games."""
from __future__ import annotations

import argparse
import json
import os

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.two_engine_live import freeze_week


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze the CFB two-engine pregame manifest")
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)
    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    )
    print(json.dumps(
        freeze_week(repository, season=args.season, week=args.week),
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
