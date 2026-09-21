"""CLI: python -m sports_aggregator.cfb.weather_market_impact_cli"""
from __future__ import annotations

import argparse
import json
import os

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.weather_market_impact import report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CFB weather vs. market-movement/outcome report")
    parser.add_argument("--start-season", type=int, default=2015)
    parser.add_argument("--end-season", type=int, default=2025)
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    payload = report(repository, start_season=args.start_season, end_season=args.end_season)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
