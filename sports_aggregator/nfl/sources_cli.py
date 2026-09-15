"""CLI for importing and inspecting the curated NFL source directory."""

from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.nfl.source_directory import DEFAULT_PATH, import_directory, load_directory
from sports_aggregator.social.registry import SourceRegistry


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "import", "status"))
    parser.add_argument("--input", default=str(DEFAULT_PATH))
    args = parser.parse_args(argv)
    profiles = load_directory(args.input)
    if args.command == "validate":
        print(json.dumps({
            "rows": len(profiles),
            "teams": len({item.team for item in profiles if item.team}),
            "sections": sorted({tag[2] for item in profiles for tag in item.tags}),
        }, indent=2))
        return 0
    database = os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    registry = SourceRegistry(database)
    if args.command == "import":
        print(f"nfl_sources_imported={import_directory(registry, args.input)}")
        return 0
    rows = registry.list_league_sources("nfl")
    print(json.dumps({
        "sources": len(rows),
        "verified": sum(row["resolution_status"] == "verified" for row in rows),
        "teams": len({row["team"] for row in rows if row["team"]}),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
