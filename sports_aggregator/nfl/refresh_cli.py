"""Isolated subprocess entry points for the shared scheduled refresh."""
from __future__ import annotations
import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("segment", choices=("rosters", "core", "content", "pff"))
    parser.add_argument("--season", type=int, required=True)
    args = parser.parse_args(argv)
    from app import create_app
    app = create_app()
    command = {
        "rosters": ["sync-nfl-rosters", "--year", str(args.season)],
        "core": ["sync-nfl", "--year", str(args.season)],
        "content": ["sync-nfl-content", "--year", str(args.season)],
        # Completed-season PFF is the stable baseline until a current export lands.
        "pff": ["sync-nfl-pff", "--year", str(args.season - 1)],
    }[args.segment]
    result = app.test_cli_runner().invoke(args=command)
    if result.output:
        print(result.output.rstrip())
    if result.exception:
        print(f"{result.exception.__class__.__name__}: {result.exception}", file=sys.stderr)
    return result.exit_code

if __name__ == "__main__":
    raise SystemExit(main())
