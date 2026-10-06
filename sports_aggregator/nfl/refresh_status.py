"""Segment health for the NFL data-status page, derived from the refresh history file.

Two things the page used to get wrong:

* It looked only at the last 20 history rows. `availability` writes two rows every fifteen
  minutes, so that window covered about two and a half hours and any segment on a 3-12 hour
  cadence (`core-stats`, `core-depth`, `core-pbp`, `weather`, `pff`) fell out of it and read
  "unknown" while perfectly healthy.
* A run that started and never wrote a result -- the process was killed, usually by the
  platform running out of memory -- was invisible: only the "running" row exists, and the
  page skipped it. It is now reported as `interrupted`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

#: A run with no result this long after it started is treated as dead. The longest NFL segment
#: budget is 30 minutes; anything past two hours was killed rather than slow.
INTERRUPTED_AFTER = timedelta(hours=2)
FINISHED = frozenset({"success", "failed"})


def read_history(path: Path) -> list[dict[str, Any]]:
    """Every readable history row, oldest first. The file is bounded by `refresh_cli`."""
    rows: list[dict[str, Any]] = []
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    return rows


def _when(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def segment_health(rows: Iterable[dict[str, Any]], segments: Sequence[str], *,
                   now: datetime | None = None,
                   relative: Callable[[object], str | None] = lambda value: None
                   ) -> list[dict[str, Any]]:
    """One tile per segment: its most recent started run, resolved to what happened to it.

    Statuses: `success`, `failed`, `running` (started recently, no result yet), `interrupted`
    (started long ago, no result), `skipped` (the run yielded to another one), `unknown` (no
    run recorded). `last_success` is reported separately so a segment that fails today still
    says when it last worked.
    """
    moment = now or datetime.now(timezone.utc)
    by_segment: dict[str, list[dict[str, Any]]] = {name: [] for name in segments}
    for row in rows:
        if row.get("segment") in by_segment:
            by_segment[row["segment"]].append(row)

    tiles = []
    for name in segments:
        history = by_segment[name]
        finished_keys = {(r.get("pid"), r.get("started_at")) for r in history
                         if r.get("status") in FINISHED | {"skipped"}}
        last_success = next((r for r in reversed(history) if r.get("status") == "success"), None)
        latest = next((r for r in reversed(history)
                       if r.get("status") != "running"
                       or (r.get("pid"), r.get("started_at")) not in finished_keys), None)
        tile: dict[str, Any] = {"segment": name, "status": "unknown", "relative": None,
                                "seconds": None, "error": None, "last_success": None}
        if last_success:
            tile["last_success"] = relative(last_success.get("finished_at") or last_success.get("started_at"))
        if latest:
            status = str(latest.get("status"))
            started = _when(latest.get("started_at"))
            if status == "running":
                dead = started is not None and moment - started >= INTERRUPTED_AFTER
                status = "interrupted" if dead else "running"
            tile.update(
                status=status, seconds=latest.get("seconds"),
                relative=relative(latest.get("finished_at") or latest.get("started_at")))
            if status == "failed":
                tile["error"] = "Refresh failed; inspect authenticated logs."
            elif status == "interrupted":
                tile["error"] = ("Started but never finished; the process was killed, usually "
                                 "by running out of memory.")
            elif status == "skipped":
                tile["error"] = "Yielded to another refresh that was already running."
        tiles.append(tile)
    return tiles
