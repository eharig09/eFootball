"""Cache for the walk-forward model pipeline behind Football Lab.

`build_rows`, the out-of-fold score rows, the game rows and the calibrated residuals are deterministic
functions of three tables (games, game_team_situational, game_team_efficiency) for seasons up to
`end_season`, and of the model code itself. They take tens of seconds to compute, used to be recomputed on
every dashboard render (several times over), and were invalidated by any database write even though an
in-season write cannot change a completed season.

`history_cached` keys each result on

* a fingerprint of those tables' rows for seasons <= end_season (row counts and sums of the numeric inputs,
  so a corrected score or a re-synced season changes it), and
* a hash of the model source files (this module's dependencies), so editing the model retires the cache.

Results live in memory for the life of the process and on disk (``<database dir>/model_cache``) across
restarts and across the web and refresh processes. Anything that goes wrong reading the cache falls back to
computing the value.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import inspect
import os
import pickle
import re
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from sports_aggregator.nfl.repository import NFLRepository, clone

MEMORY_LIMIT = 16
_MEMORY: "OrderedDict[tuple, Any]" = OrderedDict()
_LOCK = threading.Lock()
_CODE_HASH: dict[str, str] = {}
_MISS = object()
ROOT_MODULES = ("drive_projection", "scoring_bridge", "score_calibration", "uncertainty_calibration")
#: Plumbing the model modules import for types or data access. Editing these does not change what the models
#: compute, so they must not retire a cache that takes minutes to rebuild.
INFRASTRUCTURE_MODULES = frozenset({"repository", "models", "naming", "nflverse", "sync", "web", "views", "model_cache"})
_IMPORT = re.compile(r"^\s*(?:from\s+sports_aggregator\.nfl\.(\w+)\s+import|from\s+sports_aggregator\.nfl\s+import\s+(\w+))", re.M)

_FINGERPRINT_QUERIES = (
    """SELECT COUNT(*), SUM(completed), SUM(COALESCE(home_score,0)), SUM(COALESCE(away_score,0)),
              ROUND(SUM(COALESCE(spread_line,0)),3), ROUND(SUM(COALESCE(total_line,0)),3),
              SUM(COALESCE(home_rest,0)+COALESCE(away_rest,0)), SUM(COALESCE(division_game,0))
       FROM games WHERE season<=?""",
    """SELECT COUNT(*), SUM(plays), ROUND(SUM(total_epa),3), SUM(successful_plays), SUM(pass_plays),
              ROUND(SUM(pass_epa),3), SUM(rush_plays), ROUND(SUM(rush_epa),3), SUM(explosive_plays)
       FROM game_team_efficiency WHERE season<=?""",
    """SELECT COUNT(*), SUM(plays), SUM(drives), SUM(seconds_sum), SUM(neutral_plays), SUM(neutral_passes),
              SUM(clocked_plays) FROM game_team_situational WHERE season<=?""",
)


def _nfl_dir() -> Path:
    return Path(__file__).resolve().parent


def code_modules(roots: tuple[str, ...] | None = None) -> list[str]:
    """The model modules whose source decides what the cached functions compute (imports followed)."""
    seen: set[str] = set()
    pending = list(roots or ROOT_MODULES)
    while pending:
        name = pending.pop()
        if name in seen or name in INFRASTRUCTURE_MODULES:
            continue
        path = _nfl_dir() / f"{name}.py"
        if not path.exists():
            continue
        seen.add(name)
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _IMPORT.finditer(text):
            pending.append(match.group(1) or match.group(2))
    return sorted(seen)


def code_hash(roots: tuple[str, ...] | None = None) -> str:
    """Hash of the model source files, computed once per process.

    `roots` scopes the hash to the closure of specific modules, so a cache for a newer model does not change the
    hash (and therefore force a rebuild) of the long-standing Football Lab caches, which use the default roots.
    """
    slot = "value" if not roots else "roots:" + ",".join(roots)
    cached = _CODE_HASH.get(slot)
    if cached:
        return cached
    digest = hashlib.sha1()
    for name in code_modules(roots):
        digest.update(name.encode())
        digest.update((_nfl_dir() / f"{name}.py").read_bytes())
    value = digest.hexdigest()[:16]
    _CODE_HASH[slot] = value
    return value


def history_fingerprint(repository: NFLRepository, end_season: int, extra_queries: tuple[str, ...] = ()) -> str:
    repository.initialize()
    parts = []
    with closing(repository._connect()) as connection:
        for query in (*_FINGERPRINT_QUERIES, *extra_queries):
            parts.append(tuple(connection.execute(query, (int(end_season),)).fetchone()))
    return hashlib.sha1(repr(parts).encode()).hexdigest()[:16]


def cache_dir(repository: NFLRepository) -> Path:
    return Path(repository.path).resolve().parent / "model_cache"


def clear_memory() -> None:
    with _LOCK:
        _MEMORY.clear()


def _read(path: Path) -> Any:
    try:
        with path.open("rb") as handle:
            return pickle.load(handle)
    except Exception:
        return _MISS




def _write(directory: Path, prefix: str, name: str, value: Any) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        temporary = directory / (name + f".{os.getpid()}.part")
        with temporary.open("wb") as handle:
            pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, target)
        for stale in directory.glob(prefix + "*.pkl"):          # older fingerprints of the same result
            if stale.name != name:
                stale.unlink(missing_ok=True)
    except Exception:
        pass   # a cache that cannot be written is just a cache miss next time


def history_cached(name: str, *, roots: tuple[str, ...] | None = None, extra_queries: tuple[str, ...] = ()) -> Callable:
    """Decorate `fn(repository, ..., start_season, end_season, ...)` with the fingerprinted cache.

    `roots` scopes the source hash to those modules' import closure; `extra_queries` (each taking the end season as its one
    parameter) add tables the shared fingerprint does not cover to the data fingerprint.
    """
    def decorate(function: Callable) -> Callable:
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapper(repository, *args, **kwargs):
            if not isinstance(repository, NFLRepository):
                return function(repository, *args, **kwargs)
            try:
                bound = signature.bind(repository, *args, **kwargs)
                bound.apply_defaults()
                arguments = dict(bound.arguments)
                start, end = int(arguments["start_season"]), int(arguments["end_season"])
                extras = tuple(sorted((key, repr(value)) for key, value in arguments.items()
                                      if key not in {"repository", "start_season", "end_season"}))
                fingerprint = history_fingerprint(repository, end, extra_queries)
            except Exception:
                return function(repository, *args, **kwargs)
            digest = hashlib.sha1(repr(extras).encode()).hexdigest()[:8]
            prefix = f"{name}-{start}-{end}-{digest}-"
            filename = f"{prefix}{fingerprint}-{code_hash(roots)}.pkl"
            memory_key = (os.path.abspath(repository.path), filename)
            with _LOCK:
                hit = _MEMORY.get(memory_key, _MISS)
                if hit is not _MISS:
                    _MEMORY.move_to_end(memory_key)
            if hit is _MISS:
                path = cache_dir(repository) / filename
                loaded = _read(path) if path.exists() else _MISS
                if loaded is _MISS:
                    loaded = function(repository, *args, **kwargs)
                    _write(cache_dir(repository), prefix, filename, loaded)
                with _LOCK:
                    _MEMORY[memory_key] = loaded
                    _MEMORY.move_to_end(memory_key)
                    while len(_MEMORY) > MEMORY_LIMIT:
                        _MEMORY.popitem(last=False)
                hit = loaded
            return clone(hit)

        wrapper.uncached = function   # type: ignore[attr-defined]
        return wrapper
    return decorate


def warm(repository: NFLRepository, season: int) -> dict[str, float]:
    """Build the cached model inputs for `season`'s Football Lab report, returning seconds per piece.

    Run by the refresh job after play-by-play lands so the first page view never pays for it. Results that are
    already cached (same data and model code) return immediately.
    """
    import time
    from sports_aggregator.nfl import drive_projection, live_margin, score_calibration, scoring_bridge, uncertainty_calibration

    end = max(2010, int(season) - 1)
    steps = (
        ("core_oof", lambda: scoring_bridge._core_oof_rows(repository, 2010, end)),
        ("game_rows", lambda: score_calibration._game_rows(repository, start_season=2010, end_season=end)),
        ("calibrated_oof", lambda: uncertainty_calibration._calibrated_oof(repository, 2010, end)),
        ("drive_rows", lambda: drive_projection.build_rows(repository, start_season=2010, end_season=int(season))),
        ("live_margin_fit", lambda: live_margin.fit_model(repository, 2010, end)),
    )
    seconds: dict[str, float] = {}
    for name, step in steps:
        started = time.perf_counter()
        step()
        seconds[name] = round(time.perf_counter() - started, 1)
    return seconds

