"""CLI for the four-signal convergence ladder backtest.

python -m sports_aggregator.cfb.convergence_four_signal_cli
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sports_aggregator.cfb.convergence_four_signal import report
from sports_aggregator.cfb.repository import CFBRepository


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESEARCH_OUTPUT_DIR = REPO_ROOT / "research_outputs"


def _default_output_path(test_season: int) -> Path:
    return DEFAULT_RESEARCH_OUTPUT_DIR / (
        f"cfb_convergence_four_signal_through_{int(test_season)}.json"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backtest HC/QB Elo as a fourth CFB convergence confirmation"
    )
    parser.add_argument("--test-season", type=int, default=2025)
    parser.add_argument("--elo-start-season", type=int, default=2015)
    parser.add_argument("--database", default=None)
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Path for the complete JSON output. Defaults to "
            "research_outputs/cfb_convergence_four_signal_through_<season>.json "
            "at the repository root."
        ),
    )
    parser.add_argument(
        "--include-game-rows",
        action="store_true",
        help="Also include the full game-level payload in terminal output.",
    )
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    )
    payload = report(
        repository,
        test_season=args.test_season,
        elo_start_season=args.elo_start_season,
    )

    output_path = Path(args.output).expanduser() if args.output else _default_output_path(
        args.test_season
    )
    if not output_path.is_absolute():
        output_path = REPO_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    terminal_payload = payload
    if not args.include_game_rows:
        terminal_payload = {k: v for k, v in payload.items() if k != "game_rows"}

    print(json.dumps(terminal_payload, indent=2))
    print(f"Full output written to: {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
