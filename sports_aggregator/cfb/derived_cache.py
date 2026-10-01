"""Caches for values the college football game page derives from history rather than from the game itself.

Two kinds, both fail-open (a cache that cannot be read or written just means the value is computed):

* `derived` -- an in-process memo keyed by the database's contents stamp (see `_stamp`). Anything stored is shared
  between requests, so it must be treated as read-only. A write by any process (the refresh job included) changes
  the key, so a stale value is never served past the data it came from.
* `persisted` -- a small JSON value kept next to the database, keyed by a caller-supplied fingerprint of the data
  and a hash of the model source, for results that are expensive to rebuild and only change when either changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

MEMORY_LIMIT = 24
_MEMORY: "OrderedDict[tuple, Any]" = OrderedDict()
_LOCK = threading.Lock()
_MISS = object()


def _stamp(path: str | os.PathLike[str]) -> tuple[int, int, int]:
    """Identity of the database's contents: the main file's mtime and size plus the write-ahead log's size.

    The WAL's mtime is deliberately not used: the file is recreated whenever a connection opens an idle database,
    so its mtime moves on every request even when nothing was written. Committed data only ever lengthens the log
    until a checkpoint, and a checkpoint rewrites the main file (new mtime).
    """
    database = Path(path)
    try:
        main = database.stat()
        main_mtime, main_size = main.st_mtime_ns, main.st_size
    except OSError:
        main_mtime = main_size = 0
    try:
        wal_size = database.with_name(database.name + "-wal").stat().st_size
    except OSError:
        wal_size = 0
    return main_mtime, main_size, wal_size


def derived(repository, name: str, build: Callable[[], Any], *key: Any) -> Any:
    """Return `build()` once per (database state, name, key); the result is shared, so do not mutate it."""
    path = getattr(repository, "path", None)
    if not path:
        return build()
    full = (os.path.abspath(path), _stamp(path), name, key)
    try:
        hash(full)
    except TypeError:
        return build()
    with _LOCK:
        hit = _MEMORY.get(full, _MISS)
        if hit is not _MISS:
            _MEMORY.move_to_end(full)
            return hit
    value = build()
    with _LOCK:
        _MEMORY[full] = value
        _MEMORY.move_to_end(full)
        while len(_MEMORY) > MEMORY_LIMIT:
            _MEMORY.popitem(last=False)
    return value


def clear_memory() -> None:
    with _LOCK:
        _MEMORY.clear()


def source_hash(*modules) -> str:
    """Short hash of the given modules' source files, so editing a model retires what it produced."""
    digest = hashlib.sha1()
    for module in modules:
        try:
            digest.update(Path(module.__file__).read_bytes())
        except (OSError, TypeError):
            digest.update(repr(module).encode())
    return digest.hexdigest()[:12]


def persisted(repository, name: str, fingerprint: str, build: Callable[[], Any]) -> Any:
    """A JSON-serializable value cached on disk under <database dir>/model_cache/<name>-<fingerprint>.json."""
    if not getattr(repository, "path", None):
        return build()
    directory = Path(repository.path).resolve().parent / "model_cache"
    target = directory / f"{name}-{fingerprint}.json"
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        pass
    value = build()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f"{target.name}.{os.getpid()}.part"
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, target)
        for stale in directory.glob(f"{name}-*.json"):
            if stale.name != target.name:
                stale.unlink(missing_ok=True)
    except Exception:
        pass
    return value
