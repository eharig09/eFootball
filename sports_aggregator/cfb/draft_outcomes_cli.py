"""Import a historical NFL draft results export (round, pick, career outcome)."""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.draft_outcomes import import_outcomes
from sports_aggregator.cfb.repository import CFBRepository


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path", help="Pro-Football-Reference-style CSV: Rnd,Pick,Tm,Player,Pos,...,Year")
    args = parser.parse_args(argv)
    repository = CFBRepository(os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    counts = import_outcomes(repository, args.path)
    print(f"rows={counts['rows']} draft_years={counts['draft_years']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
