"""CLI for HC/QB global-vs-walk-forward normalization audit."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sports_aggregator.cfb.hc_qb_normalization_audit import report
from sports_aggregator.cfb.repository import CFBRepository

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit legacy versus walk-forward HC/QB normalization through 2025"
    )
    parser.add_argument("--end-season", type=int, default=2025)
    parser.add_argument("--elo-start-season", type=int, default=2015)
    parser.add_argument("--database", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    )
    payload = report(
        repository,
        end_season=args.end_season,
        elo_start_season=args.elo_start_season,
    )
    output = (
        Path(args.output).expanduser()
        if args.output
        else REPO_ROOT / "research_outputs" / "cfb_hc_qb_normalization_audit_2020_2025.json"
    )
    if not output.is_absolute():
        output = REPO_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"Full output written to: {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
