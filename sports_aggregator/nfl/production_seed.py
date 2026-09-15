"""Restart-safe first-run population for the Render NFL data store.

Render makes an attached disk available only to the running service, not to its
build or pre-deploy instances.  The web worker therefore launches this bounded
helper when the canonical NFL database is empty.  Each expensive phase runs in
its own subprocess so pandas/Arrow memory is returned before the next phase.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import shutil
import sqlite3
from typing import Any

from sports_aggregator.nfl.nflverse import current_season
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.source_directory import DEFAULT_PATH as SOURCE_DIRECTORY_PATH, import_directory


LOCK_NAME = "nfl_production_seed.lock"
STATE_NAME = "nfl_production_seed.json"
LOG_NAME = "nfl_production_seed.log"
DEFAULT_RETRY_SECONDS = 6 * 3600
DEFAULT_SEED_ARCHIVE = Path(__file__).resolve().parents[2] / "data" / "nfl" / "render_seed.sqlite3.gz"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_stamp(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def needs_seed(repository: NFLRepository, season: int) -> bool:
    """Return true until the public-data shell has teams, games, and players."""
    repository.initialize()
    counts = repository.counts(season)
    return counts["teams"] < 32 or counts["games"] == 0 or counts["players"] == 0


def maybe_launch(*, database_path: str | os.PathLike[str], season: int | None = None,
                 retry_seconds: int = DEFAULT_RETRY_SECONDS) -> bool:
    """Atomically launch a detached seed helper when a production DB is empty."""
    if os.getenv("NFL_AUTO_SEED_CHILD", "").strip() == "1":
        return False
    season = int(season or current_season())
    database = Path(database_path)
    repository = NFLRepository(database)
    if not needs_seed(repository, season):
        return False

    state_path = database.parent / STATE_NAME
    prior_state = _read_json(state_path)
    deploy = os.getenv("RENDER_GIT_COMMIT", "").strip()[:16]
    last = _parse_stamp(prior_state.get("started_at"))
    if (last and prior_state.get("deploy") == deploy and
            datetime.now(timezone.utc) - last < timedelta(seconds=max(60, retry_seconds))):
        return False

    lock_path = database.parent / LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # A killed process cannot own a lock forever.  Its state timestamp is
        # the retry throttle; an old orphan can be replaced safely here.
        try:
            if (prior_state.get("deploy") == deploy and
                    datetime.now(timezone.utc).timestamp() - lock_path.stat().st_mtime <= retry_seconds):
                return False
            lock_path.unlink()
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, OSError):
            return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "created_at": _stamp()}))

    state_path.write_text(json.dumps({
        "status": "launching", "season": season, "deploy": deploy,
        "started_at": _stamp(), "stages": [],
    }), encoding="utf-8")
    environment = os.environ.copy()
    environment["NFL_AUTO_SEED_CHILD"] = "1"
    root = Path(__file__).resolve().parents[2]
    log_path = database.parent / LOG_NAME
    try:
        with log_path.open("a", encoding="utf-8") as log:
            subprocess.Popen(
                [sys.executable, "-m", "sports_aggregator.nfl.production_seed",
                 "--season", str(season), "--delay", "30"],
                cwd=str(root), env=environment, stdout=log, stderr=subprocess.STDOUT,
                close_fds=True,
            )
    except Exception:
        lock_path.unlink(missing_ok=True)
        raise
    return True


def _pff_available() -> bool:
    root = Path(os.getenv("NFL_PFF_SOURCE_ROOT", "")).expanduser()
    return any((root / relative).is_dir() for relative in ("nfl/pff", "pff_coverage_data"))


def _import_source_directory() -> int | None:
    """Seed the shared, CFB-hosted source registry with the NFL directory.

    Normally `sync-nfl` (the "essentials" stage) does this. The snapshot
    fast path below returns before that stage ever runs, so on a fresh disk
    the registry stayed empty forever even though the NFL data itself
    restored fine -- every "leaders"/"sources" page that reads it (team
    coverage, the source audit page, ingest_bluesky's account list) saw
    nothing configured. This is cheap (one workbook, no pandas) so it runs
    unconditionally after either seed path.
    """
    if not SOURCE_DIRECTORY_PATH.exists():
        return None
    from sports_aggregator.social.registry import SourceRegistry
    cfb_database = Path(os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    if not cfb_database.is_absolute():
        cfb_database = Path(__file__).resolve().parents[2] / cfb_database
    return import_directory(SourceRegistry(cfb_database), SOURCE_DIRECTORY_PATH)


def restore_public_seed(database: Path, archive: Path = DEFAULT_SEED_ARCHIVE) -> dict[str, int]:
    """Merge a compressed, public-only SQLite snapshot without loading pandas."""
    if not archive.is_file():
        return {"tables": 0, "rows": 0}
    temporary = database.parent / "nfl_render_seed.restore.sqlite3"
    temporary.unlink(missing_ok=True)
    with gzip.open(archive, "rb") as incoming, temporary.open("wb") as output:
        shutil.copyfileobj(incoming, output, length=1024 * 1024)
    connection = sqlite3.connect(database, timeout=60)
    tables = rows = 0
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ATTACH DATABASE ? AS seed", (str(temporary),))
        names = [row[0] for row in connection.execute(
            "SELECT name FROM seed.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        for name in names:
            dest_columns = [row[1] for row in connection.execute(f'PRAGMA main.table_info("{name}")')]
            if not dest_columns:
                continue
            # Column order can drift between databases whose "games"-style
            # tables picked up columns via different ALTER TABLE histories.
            # Matching by name (not position) keeps a stray reorder from
            # silently shifting values into the wrong column.
            source_columns = {row[1] for row in connection.execute(f'PRAGMA seed.table_info("{name}")')}
            shared = [column for column in dest_columns if column in source_columns]
            if not shared:
                continue
            column_list = ",".join(f'"{column}"' for column in shared)
            before = connection.total_changes
            connection.execute(
                f'INSERT OR IGNORE INTO main."{name}" ({column_list}) '
                f'SELECT {column_list} FROM seed."{name}"'
            )
            rows += connection.total_changes - before
            tables += 1
        connection.commit()
        connection.execute("DETACH DATABASE seed")
    finally:
        connection.close()
        temporary.unlink(missing_ok=True)
    return {"tables": tables, "rows": rows}


def run(season: int) -> dict[str, Any]:
    """Populate essentials first, then content and bounded historical context."""
    database = Path(os.getenv("NFL_DATABASE_PATH", "instance/nfl.sqlite3"))
    if not database.is_absolute():
        database = Path(__file__).resolve().parents[2] / database
    state_path = database.parent / STATE_NAME
    lock_path = database.parent / LOCK_NAME
    state: dict[str, Any] = {
        "status": "running", "season": season,
        "deploy": os.getenv("RENDER_GIT_COMMIT", "").strip()[:16],
        "started_at": _stamp(), "stages": [],
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    environment = os.environ.copy()
    environment["NFL_AUTO_SEED_CHILD"] = "1"
    root = Path(__file__).resolve().parents[2]

    try:
        restored = restore_public_seed(database)
    except Exception as exc:
        state.update(status="failed", error=f"public snapshot: {type(exc).__name__}: {exc}",
                     finished_at=_stamp())
        state_path.write_text(json.dumps(state), encoding="utf-8")
        lock_path.unlink(missing_ok=True)
        return state
    if restored["rows"]:
        state["stages"].append({"stage": "public_snapshot", "started_at": state["started_at"],
                                "finished_at": _stamp(), "status": "success", **restored})
        try:
            imported = _import_source_directory()
            state["stages"].append({"stage": "source_directory", "finished_at": _stamp(),
                                    "status": "success", "sources": imported})
        except Exception as exc:
            state["stages"].append({"stage": "source_directory", "finished_at": _stamp(),
                                    "status": "failed",
                                    "error": f"{type(exc).__name__}: {exc}"})
        state["status"] = "success" if not needs_seed(NFLRepository(database), season) else "degraded"
        state["counts"] = NFLRepository(database).counts(season)
        state["finished_at"] = _stamp()
        state_path.write_text(json.dumps(state), encoding="utf-8")
        lock_path.unlink(missing_ok=True)
        return state

    stages = ["essentials", "content", "history"]
    if _pff_available():
        stages.insert(2, "pff")
    try:
        for stage in stages:
            started = _stamp()
            completed = subprocess.run(
                [sys.executable, "-m", "sports_aggregator.nfl.refresh_cli", stage,
                 "--season", str(season)],
                cwd=str(root), env=environment, check=False,
            )
            state["stages"].append({
                "stage": stage, "started_at": started, "finished_at": _stamp(),
                "exit_code": completed.returncode,
                "status": "success" if completed.returncode == 0 else "failed",
            })
            state_path.write_text(json.dumps(state), encoding="utf-8")
        repository = NFLRepository(database)
        ready = not needs_seed(repository, season)
        state["status"] = "success" if ready else "degraded"
        state["counts"] = repository.counts(season)
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        state["finished_at"] = _stamp()
        state_path.write_text(json.dumps(state), encoding="utf-8")
        lock_path.unlink(missing_ok=True)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Populate an empty production NFL database")
    parser.add_argument("--season", type=int, default=current_season())
    parser.add_argument("--delay", type=int, default=0,
                        help="Let the web process bind before loading data libraries.")
    args = parser.parse_args(argv)
    if args.delay:
        time.sleep(max(0, min(args.delay, 120)))
    report = run(args.season)
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0 if report["status"] in {"success", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
