"""CLI for the CFB QB Elo engine: python -m sports_aggregator.cfb.qb_elo_cli ..."""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.qb_elo import build, leaderboard
from sports_aggregator.cfb.repository import CFBRepository


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="CFB QB Elo")
    parser.add_argument("command", choices=("build", "leaderboard"))
    parser.add_argument("--start-season", type=int, default=2015)
    parser.add_argument("--min-starts", type=int, default=6)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))

    if args.command == "build":
        n = build(repository, start_season=args.start_season)
        print(json.dumps({"starts_replayed": n}, indent=2))
    else:
        rows = leaderboard(repository, min_starts=args.min_starts)
        print(json.dumps(rows[:args.limit], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
