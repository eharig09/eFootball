"""Refresh predictive/model data used by the college-football views.

This command intentionally groups model-shaped sources without blending them:
CFBD CORE ratings remain a CFBD model and ESPN FPI remains an independently
sourced SportsDataverse import.  Keeping the refresh together makes it easy for
the bootstrap workflow to update both whenever market/game data move.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import os
import sqlite3

from dotenv import load_dotenv

from sports_aggregator.cfb.cfbd import CFBDClient, CFBDConfigurationError
from sports_aggregator.cfb.external_cli import ingest_fpi
from sports_aggregator.cfb.repository import CFBRepository


#: Prior seasons of FPI kept for the margin model. The richer margin-v2 tiers all take FPI as an input and
#: are only fitted from history that carries it, so a database that imported just the current season can
#: reach only the base tier and projects differently from one that has the history.
FPI_HISTORY_SEASONS = 4


def missing_fpi_seasons(repository: CFBRepository, year: int, span: int = FPI_HISTORY_SEASONS) -> list[int]:
    """Prior seasons (the `span` before `year`) with no stored FPI rows."""
    with closing(repository._connect()) as connection:
        try:
            have = {int(row[0]) for row in connection.execute(
                "SELECT DISTINCT season FROM fpi_game_projections")}
        except sqlite3.OperationalError:
            have = set()
    return [season for season in range(year - span, year) if season not in have]


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Refresh CFB model datasets")
    parser.add_argument("command", choices=("sync",))
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--force", action="store_true", help="Bypass upstream caches")
    parser.add_argument("--database", default=None, help="Override CFB_DATABASE_PATH")
    args = parser.parse_args(argv)

    database = args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    repository = CFBRepository(database)
    failures: list[str] = []

    client = CFBDClient(raw_cache_path=os.getenv("CFBD_RAW_CACHE_PATH", "instance/cfbd_raw"))
    if not client.configured:
        raise CFBDConfigurationError("CFBD_API_KEY is required for model sync")

    try:
        count = repository.replace_core_ratings(
            args.year, client.core_ratings(args.year, args.force)
        )
        print(f"core_ratings {args.year}: stored {count}")
    except Exception as exc:
        failures.append("core_ratings")
        print(f"core_ratings {args.year}: failed ({str(exc)[:160]})")

    try:
        fpi_failures = ingest_fpi(repository, [args.year], force=args.force)
        if fpi_failures:
            failures.append("fpi")
    except Exception as exc:
        failures.append("fpi")
        print(f"fpi {args.year}: failed ({str(exc)[:160]})")

    # Idempotent and a one-time cost: only seasons with no rows are fetched. Never fatal, since the
    # current season is what the refresh is for and a missing history only costs the margin model a tier.
    try:
        backfill = missing_fpi_seasons(repository, args.year)
        if backfill:
            print(f"fpi history: importing {backfill}")
            ingest_fpi(repository, backfill, force=args.force)
    except Exception as exc:
        print(f"fpi history: skipped ({str(exc)[:160]})")

    if failures:
        print(f"model sync {args.year}: failed datasets: {', '.join(failures)}")
        return 1
    print(f"model sync {args.year}: complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
