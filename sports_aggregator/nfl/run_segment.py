"""Run one NFL refresh segment under a supervisor that records how it ended.

The web service launches refreshes as detached children and never looks at them again. A child that
raises is recorded as "failed" by `refresh_cli`, but one that dies without raising -- a segfault in
native code, a signal, a C library calling `exit()` (OpenBLAS does, when an allocation fails) -- wrote
a "running" row and nothing else, and the status page could only say "interrupted" with no hint why.
Days went into guessing at memory, deploys and downloads from timing alone.

This stays tiny and light on purpose (no pandas, no database) so it adds nothing to the footprint. It
starts `refresh_cli` as its own child, waits, and if that child ended non-zero without writing a
result row, records one: `crashed`, with the exit code or the name of the signal that ended it.
"""
from __future__ import annotations

import signal
import subprocess
import sys
from datetime import datetime, timezone

from sports_aggregator.nfl import refresh_cli
from sports_aggregator.nfl.refresh_status import read_history

RESULT_STATUSES = {"success", "failed", "skipped", "crashed"}


def describe_exit(returncode: int) -> str:
    """'killed by SIGKILL', 'killed by SIGSEGV', 'exit code 1' ..."""
    if returncode < 0:
        try:
            return f"killed by {signal.Signals(-returncode).name}"
        except ValueError:
            return f"killed by signal {-returncode}"
    return f"exit code {returncode}"


def record_if_unreported(args: list[str], child_pid: int, returncode: int, *, supervised_at: datetime) -> bool:
    """Write a `crashed` row when the child ended badly and left no result. True if one was written."""
    if returncode == 0:
        return False
    rows = read_history(refresh_cli._history_path())
    mine = [r for r in rows if r.get("pid") == child_pid]
    if any(r.get("status") in RESULT_STATUSES for r in mine):
        return False                                    # it reported its own outcome
    opened = next((r for r in reversed(mine) if r.get("status") == "running"), None)
    now = datetime.now(timezone.utc)
    started = (opened or {}).get("started_at") or supervised_at.isoformat()
    refresh_cli._append_history({
        "segment": args[0] if args else "unknown",
        "season": int(args[args.index("--season") + 1]) if "--season" in args else None,
        "pid": child_pid, "started_at": started, "finished_at": now.isoformat(),
        "seconds": round((now - supervised_at).total_seconds(), 1),
        "status": "crashed", "returncode": returncode, "exit": describe_exit(returncode),
        "reported_start": opened is not None,
    })
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    supervised_at = datetime.now(timezone.utc)
    child = subprocess.Popen([sys.executable, "-m", "sports_aggregator.nfl.refresh_cli", *args])

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except OSError:
            pass

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), forward)
    returncode = child.wait()
    try:
        record_if_unreported(args, child.pid, returncode, supervised_at=supervised_at)
    except Exception as exc:  # recording must never mask the child's own exit status
        print(f"run_segment: could not record exit: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return returncode if returncode >= 0 else 128 - returncode


if __name__ == "__main__":
    raise SystemExit(main())
