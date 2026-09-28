"""Read-only NFL SQLite capacity checks.

The production disk is shared with caches and the CFB database, so the NFL
database needs an explicit budget rather than growing until SQLite receives a
disk-full error. This module reports sizes without exposing configured paths.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any


def storage_status(database_path: str | Path, *, max_bytes: int = 0) -> dict[str, Any]:
    """Return a public-safe, read-only capacity summary for one SQLite store."""
    database = Path(database_path)
    files = (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
    sizes: list[int] = []
    for path in files:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            sizes.append(0)

    database_bytes, wal_bytes, shm_bytes = sizes
    total_bytes = sum(sizes)
    page_count = 0
    free_pages = 0
    page_size = 0
    if database_bytes:
        try:
            # immutable=1 keeps this metadata-only probe out of SQLite's WAL
            # coordination. It must never be used for application data reads,
            # but avoids holding or creating sidecar handles during a status
            # request while the separately measured file sizes stay current.
            uri = f"file:{database.resolve().as_posix()}?mode=ro&immutable=1"
            # sqlite3.Connection's context manager commits/rolls back but does
            # not close the handle; closing() matters on Windows where an open
            # read handle prevents a temporary database from being removed.
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
                page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
                free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        except (OSError, sqlite3.Error, TypeError, ValueError):
            pass

    max_bytes = max(0, int(max_bytes or 0))
    utilization = (total_bytes / max_bytes) if max_bytes else None
    if not max_bytes:
        status = "unconfigured"
    elif total_bytes >= max_bytes:
        status = "over-budget"
    elif utilization is not None and utilization >= 0.85:
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "database_bytes": database_bytes,
        "wal_bytes": wal_bytes,
        "shm_bytes": shm_bytes,
        "total_bytes": total_bytes,
        "max_bytes": max_bytes or None,
        "utilization": round(utilization, 4) if utilization is not None else None,
        "page_count": page_count,
        "free_pages": free_pages,
        "page_size": page_size,
        "reclaimable_bytes": free_pages * page_size,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the NFL SQLite storage budget.")
    parser.add_argument("--database", required=True)
    parser.add_argument("--max-bytes", type=int, default=0)
    parser.add_argument("--fail-over-budget", action="store_true")
    args = parser.parse_args(argv)
    packet = storage_status(args.database, max_bytes=args.max_bytes)
    print(json.dumps(packet, sort_keys=True))
    return 2 if args.fail_over_budget and packet["status"] == "over-budget" else 0


if __name__ == "__main__":
    raise SystemExit(main())
