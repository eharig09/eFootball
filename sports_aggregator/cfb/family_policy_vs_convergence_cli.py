"""CLI for Narrative-family candidate policies versus frozen convergence routes."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sports_aggregator.cfb.family_policy_vs_convergence import report
from sports_aggregator.cfb.repository import CFBRepository

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Narrative-family candidate policies with frozen convergence routes"
    )
    parser.add_argument("--start-season", type=int, default=2020)
    parser.add_argument("--end-season", type=int, default=2025)
    parser.add_argument("--test-season", type=int, default=2025)
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
        test_season=args.test_season,
    )
    output = (
        Path(args.output).expanduser()
        if args.output
        else REPO_ROOT / "research_outputs" / "cfb_family_policy_vs_convergence_2020_2025.json"
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
