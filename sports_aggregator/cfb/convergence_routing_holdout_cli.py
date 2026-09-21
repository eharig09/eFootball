"""CLI for the frozen CFB convergence routing holdout."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sports_aggregator.cfb.convergence_routing_holdout import report
from sports_aggregator.cfb.repository import CFBRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "research_outputs"


def _default_output_path(holdout_season: int) -> Path:
    return DEFAULT_OUTPUT_DIR / f"cfb_convergence_routing_holdout_{int(holdout_season)}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate frozen positive/fade convergence routes on a holdout season"
    )
    parser.add_argument("--holdout-season", type=int, default=2026)
    parser.add_argument("--elo-start-season", type=int, default=2015)
    parser.add_argument("--database", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--include-game-rows",
        action="store_true",
        help="Also print routed holdout game rows to the terminal.",
    )
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    )
    payload = report(
        repository,
        holdout_season=args.holdout_season,
        elo_start_season=args.elo_start_season,
    )

    output_path = Path(args.output).expanduser() if args.output else _default_output_path(
        args.holdout_season
    )
    if not output_path.is_absolute():
        output_path = REPO_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    terminal_payload = payload
    if not args.include_game_rows:
        terminal_payload = {
            k: v for k, v in payload.items() if k != "holdout_routed_game_rows"
        }

    print(json.dumps(terminal_payload, indent=2))
    print(f"Full output written to: {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
