"""CLI for the four-signal convergence ladder backtest.

python -m sports_aggregator.cfb.convergence_four_signal_cli
"""
from __future__ import annotations

import argparse
import json
import os

from sports_aggregator.cfb.convergence_four_signal import report
from sports_aggregator.cfb.repository import CFBRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backtest HC/QB Elo as a fourth CFB convergence confirmation"
    )
    parser.add_argument("--test-season", type=int, default=2025)
    parser.add_argument("--elo-start-season", type=int, default=2015)
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    )
    payload = report(
        repository,
        test_season=args.test_season,
        elo_start_season=args.elo_start_season,
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
