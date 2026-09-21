"""CLI for HC/QB Elo x Narrative interaction research."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sports_aggregator.cfb.rating_narrative_interaction import report
from sports_aggregator.cfb.repository import CFBRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "research_outputs"


def _default_output_path(start_season: int, end_season: int) -> Path:
    return DEFAULT_OUTPUT_DIR / (
        f"cfb_hc_qb_narrative_interaction_{int(start_season)}_{int(end_season)}.json"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backtest HC Elo, QB Elo and joint agreement against Narrative"
    )
    parser.add_argument("--start-season", type=int, default=2020)
    parser.add_argument("--end-season", type=int, default=2025)
    parser.add_argument("--database", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    )
    payload = report(
        repository,
        start_season=args.start_season,
        end_season=args.end_season,
    )
    output_path = (
        Path(args.output).expanduser()
        if args.output
        else _default_output_path(args.start_season, args.end_season)
    )
    if not output_path.is_absolute():
        output_path = REPO_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"Full output written to: {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
