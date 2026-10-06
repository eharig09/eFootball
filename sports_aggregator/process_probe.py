"""Is a process running, and a cross-process lock built on the answer.

Standard library only: the refresh entry points import this before anything heavy.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable


def _windows_process_alive(pid: int) -> bool | None:
    """True/False when Windows says so, None when it cannot be determined.

    `os.kill(pid, 0)` is no probe on Windows: signal 0 is CTRL_C_EVENT there, and for an
    absent pid it returns without error, so a dead lock holder looked alive for the whole
    stale window. OpenProcess + GetExitCodeProcess answer the actual question.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover
        return None
    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter, error_access_denied = 87, 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False   # no such process
        return True if error == error_access_denied else None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    """Whether `pid` is a running process. Uncertainty counts as alive: a false 'alive' costs a
    wait, a false 'dead' runs two refreshes at once."""
    if pid <= 0:
        return True
    if sys.platform == "win32":
        verdict = _windows_process_alive(pid)
        return True if verdict is None else verdict
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def lock_is_held(path: Path, *, stale_seconds: float,
                 alive: Callable[[int], bool] = process_alive) -> bool:
    """Read-only: would `try_lock` refuse right now? True for a live holder's fresh lock."""
    try:
        holder: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) or {}
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        holder, age = {}, 0.0 if path.exists() else float("inf")
    pid = int(holder.get("pid") or 0) if isinstance(holder, dict) else 0
    if pid and not alive(pid):
        return False
    return age < stale_seconds


def try_lock(path: Path, *, stale_seconds: float,
             alive: Callable[[int], bool] = process_alive) -> bool:
    """Take an exclusive lock file holding our pid. A dead holder's lock, or one untouched for
    `stale_seconds`, is reclaimed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            holder: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            holder = {}
        pid = int(holder.get("pid") or 0)
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0.0
        if pid and not alive(pid):
            path.unlink(missing_ok=True)
        elif age < stale_seconds:
            return False
        else:
            path.unlink(missing_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "started_at": time.time()}, handle)
    return True


def lock_with_wait(path: Path, *, stale_seconds: float, wait_seconds: float,
                   poll_seconds: float = 5.0, sleep: Callable[[float], None] = time.sleep,
                   alive: Callable[[int], bool] = process_alive) -> bool:
    """`try_lock`, retried for up to `wait_seconds`: a short collision should queue, not be lost."""
    waited = 0.0
    while True:
        if try_lock(path, stale_seconds=stale_seconds, alive=alive):
            return True
        if waited >= wait_seconds:
            return False
        sleep(poll_seconds)
        waited += poll_seconds
