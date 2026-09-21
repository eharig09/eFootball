"""CLI for the historical weather backfill: python -m sports_aggregator.cfb.weather_backfill_cli ..."""
from __future__ import annotations

import argparse
import json
import os

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.weather_backfill import backfill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill historical CFB game weather via Open-Meteo archive")
    parser.add_argument("--start-season", type=int, default=2015)
    parser.add_argument("--end-season", type=int, default=2025)
    parser.add_argument("--force", action="store_true", help="Re-fetch games already backfilled")
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    result = backfill(repository, start_season=args.start_season, end_season=args.end_season,
                      force=args.force)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
