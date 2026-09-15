"""Lock-safe, low-memory entry point for unattended CFB refreshes."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Callable

from dotenv import load_dotenv

from sports_aggregator.bootstrap import _env_satisfied, steps
from sports_aggregator.refresh_health import classify_step_rows, summarize_segment

try:
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]


# Normal refreshes deliberately exclude the expensive Google News crawl. It has
# its own bounded `news` profile below.
LIGHT_REFRESH_STEPS = [
    "cfbd-sync", "articles", "weather", "bluesky", "reddit", "youtube", "podcasts",
    # `articles` used to rebuild the story clusters itself; that spike is now
    # left to the dedicated step so the ingest stays light.
    "cluster", "nfl-rosters", "nfl-content",
]
SCORES_REFRESH_STEPS = ["cfbd-sync", "cfbd-lines"]
RESULTS_REFRESH_STEPS = ["cfbd-sync", "cfbd-box-scores", "cfbd-lines"]
SCORES_DATASETS = ["games"]
REFRESH_PROFILES = frozenset({"light", "heavy", "scores", "results", "news"})
RESUME_WINDOW_HOURS = 12.0
DEFAULT_CHILD_MEMORY_MB = 320

CFBD_DATASET_STEPS = [
    "teams", "players", "games", "betting_lines", "media", "records", "coaches",
    "rankings", "team_stats", "advanced_stats", "core_ratings",
]


def _child_memory_mb() -> int:
    raw = (os.getenv("CFB_REFRESH_CHILD_MB") or "").strip()
    if not raw:
        return DEFAULT_CHILD_MEMORY_MB
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_CHILD_MEMORY_MB


def _memory_limiter():
    megabytes = _child_memory_mb()
    if resource is None or not megabytes or sys.platform == "win32":
        return None

    def apply() -> None:  # pragma: no cover - child only
        limit = megabytes * 1024 * 1024
        try:
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            ceiling = limit if hard in (resource.RLIM_INFINITY, -1) else min(limit, hard)
            resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
        except (ValueError, OSError):
            pass
    return apply


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _lock_holder(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _acquire_lock(path: Path, started: datetime, stale_hours: float) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        holder = _lock_holder(path)
        pid = int(holder.get("pid") or 0)
        age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        if pid and not _process_alive(pid):
            path.unlink(missing_ok=True)
        elif age < stale_hours * 3600:
            return False
        else:
            path.unlink(missing_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "started_at": started.isoformat()}, handle)
    return True


def _touch_lock(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def _refresh_statistics(instance: Path) -> None:
    try:
        from sports_aggregator.cfb.repository import CFBRepository
        database = Path((os.getenv("CFB_DATABASE_PATH") or "").strip() or instance / "cfb.sqlite3")
        if database.exists():
            CFBRepository(database).optimize()
    except Exception:
        pass


def _rusage_mb(who: int) -> float | None:
    if resource is None:
        return None
    raw = resource.getrusage(who).ru_maxrss
    divisor = 1024.0 if sys.platform != "darwin" else 1024.0 * 1024.0
    return round(raw / divisor, 1)


def _rss_mb() -> float | None:
    return _rusage_mb(resource.RUSAGE_SELF) if resource is not None else None


def _children_rss_mb() -> float | None:
    return _rusage_mb(resource.RUSAGE_CHILDREN) if resource is not None else None


def _database_path(root: Path) -> Path:
    configured = (os.getenv("CFB_DATABASE_PATH") or "").strip()
    database = Path(configured) if configured else root / "instance" / "cfb.sqlite3"
    return database if database.is_absolute() else root / database


def _sqlite_values(database: Path, sql: str) -> list[str]:
    if not database.exists():
        return []
    try:
        with closing(sqlite3.connect(database)) as connection:
            return [str(row[0]) for row in connection.execute(sql).fetchall() if row[0]]
    except sqlite3.Error:
        return []


def _fbs_teams(root: Path) -> list[str]:
    return _sqlite_values(_database_path(root),
        "SELECT school FROM teams WHERE lower(classification)='fbs' ORDER BY school")


def _conferences(root: Path) -> list[str]:
    return _sqlite_values(_database_path(root),
        "SELECT DISTINCT conference FROM teams WHERE conference IS NOT NULL ORDER BY conference")


def _progress_path(instance: Path) -> Path:
    return instance / "refresh_progress.json"


def _read_progress(instance: Path) -> dict[str, Any]:
    try:
        return json.loads(_progress_path(instance).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _resumable_steps(instance: Path, season: int, profile: str, *,
                     window_hours: float, now: datetime) -> set[str]:
    if window_hours <= 0:
        return set()
    record = _read_progress(instance)
    if not record or record.get("completed"):
        return set()
    if record.get("season") != season or record.get("profile") != profile:
        return set()
    try:
        started = datetime.fromisoformat(str(record.get("started_at")))
    except (TypeError, ValueError):
        return set()
    if (now - started).total_seconds() > window_hours * 3600:
        return set()
    return {name for name, entry in (record.get("steps") or {}).items()
            if entry.get("status") == "success"}


def _write_progress(instance: Path, record: dict[str, Any]) -> None:
    path = _progress_path(instance)
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def _log_end(log) -> int:
    """Where this step's output will start in the shared log."""
    try:
        log.flush()
        return os.fstat(log.fileno()).st_size
    except OSError:
        return -1


def _last_line(log, mark: int) -> str:
    """The step's own last line of output, which is usually its summary.

    Steps write straight into the shared log rather than through a pipe, so
    nothing here held on to what they said and every successful step reported
    "exit code 0" -- which the status page then showed in the column meant for
    what changed. Reading back from where this step started costs one seek and
    keeps the subprocess's output unbuffered, which is why it goes to the file
    in the first place.
    """
    if mark < 0:
        return ""
    try:
        log.flush()
        with open(log.name, "r", encoding="utf-8", errors="replace") as source:
            source.seek(mark)
            lines = [line.strip() for line in source.read().splitlines()]
    except (OSError, ValueError):
        return ""
    for line in reversed(lines):
        if line:
            return line[:240]
    return ""


def _run_command(command: list[str], *, timeout: int, log) -> tuple[str, str, float]:
    started = datetime.now(timezone.utc)
    mark = _log_end(log)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", *command], stdout=log, stderr=subprocess.STDOUT,
            text=True, timeout=timeout, preexec_fn=_memory_limiter(),
        )
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return ("success" if completed.returncode == 0 else "failed",
                _last_line(log, mark) or f"exit code {completed.returncode}",
                round(elapsed, 1))
    except subprocess.TimeoutExpired:
        return "timeout", f"exceeded {timeout}s", float(timeout)
    except Exception as exc:
        return "failed", str(exc)[:240], 0.0


def _run_scoped_commands(label: str, scopes: list[str], command_for: Callable[[str], list[str]],
                         *, timeout: int, log, heartbeat: Callable[[], None] | None = None) -> list[str]:
    failures: list[str] = []
    total = len(scopes)
    for index, scope in enumerate(scopes, start=1):
        if heartbeat:
            heartbeat()
        print(f"        [{label}] {index}/{total} {scope} start parent_rss_mb={_rss_mb()} child_peak_rss_mb={_children_rss_mb()}", file=log, flush=True)
        status, message, seconds = _run_command(command_for(scope), timeout=timeout, log=log)
        print(f"        [{label}] {index}/{total} {scope} {status} ({seconds}s) parent_rss_mb={_rss_mb()} child_peak_rss_mb={_children_rss_mb()} {message}", file=log, flush=True)
        if status != "success":
            failures.append(scope)
    return failures


def _run_cfbd_split(season: int, *, root: Path, timeout: int, log,
                    datasets: list[str] | None = None,
                    heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    failures: list[str] = []
    planned = [name for name in (datasets or CFBD_DATASET_STEPS) if name in CFBD_DATASET_STEPS]
    for dataset in planned:
        if heartbeat:
            heartbeat()
        print(f"    [cfbd] {dataset} start parent_rss_mb={_rss_mb()} child_peak_rss_mb={_children_rss_mb()}", file=log, flush=True)
        if dataset == "players":
            teams = _fbs_teams(root)
            if teams:
                scoped = _run_scoped_commands(
                    "roster", teams,
                    lambda team: ["sports_aggregator.cfb.dataset_cli", "players", "--year", str(season), "--team", team],
                    timeout=timeout, log=log, heartbeat=heartbeat,
                )
                status = "failed" if scoped else "success"
                message = f"failed teams: {', '.join(scoped[:8])}" if scoped else f"{len(teams)} team rosters complete"
                seconds = 0.0
            else:
                status, message, seconds = _run_command(
                    ["sports_aggregator.cfb.dataset_cli", "players", "--year", str(season)], timeout=timeout, log=log)
            if status != "success":
                failures.append(dataset)
        else:
            status, message, seconds = _run_command(
                ["sports_aggregator.cfb.dataset_cli", dataset, "--year", str(season)], timeout=timeout, log=log)
            if status != "success":
                failures.append(dataset)
        print(f"    [cfbd] {dataset} {status} ({seconds}s) parent_rss_mb={_rss_mb()} child_peak_rss_mb={_children_rss_mb()} {message}", file=log, flush=True)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return {
        "step": "cfbd-sync", "status": "failed" if failures else "success",
        "message": f"failed datasets: {', '.join(failures)}" if failures else f"scoped datasets complete: {', '.join(planned)}",
        "seconds": round(elapsed, 1), "optional": False,
        "parent_rss_mb": _rss_mb(), "child_peak_rss_mb": _children_rss_mb(),
    }


def _run_player_stats_split(season: int, *, root: Path, timeout: int, log,
                            optional: bool, heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    conferences = _conferences(root)
    if conferences:
        failures = _run_scoped_commands(
            "player-stats", conferences,
            lambda conference: ["sports_aggregator.cfb.cli", "sync-player-stats", "--year", str(season), "--conference", conference],
            timeout=timeout, log=log, heartbeat=heartbeat,
        )
        status = "failed" if failures else "success"
        message = f"failed conferences: {', '.join(failures[:8])}" if failures else f"{len(conferences)} conferences complete"
        seconds = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    else:
        status, message, seconds = _run_command(
            ["sports_aggregator.cfb.cli", "sync-player-stats", "--year", str(season)], timeout=timeout, log=log)
    return {"step": "cfbd-current-player-stats", "status": status, "message": message,
            "seconds": seconds, "optional": optional, "parent_rss_mb": _rss_mb(),
            "child_peak_rss_mb": _children_rss_mb()}


def _run_news_shard(season: int, *, timeout: int, log) -> dict[str, Any]:
    status, message, seconds = _run_command(
        ["sports_aggregator.social.local_reporting_shard", "--season", str(season)],
        timeout=timeout, log=log,
    )
    return {"step": "local-news-shard", "status": status, "message": message,
            "seconds": seconds, "optional": True, "parent_rss_mb": _rss_mb(),
            "child_peak_rss_mb": _children_rss_mb()}


def _run_low_memory_phase(phase: str, season: int, *, root: Path,
                          only: list[str] | None = None, datasets: list[str] | None = None,
                          timeout: int = 1800, log,
                          heartbeat: Callable[[], None] | None = None,
                          completed: set[str] | None = None,
                          on_step: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
    plan = [step for step in steps(season) if phase in step.phases]
    if only:
        wanted = set(only)
        plan = [step for step in plan if step.name in wanted]
    results: list[dict[str, Any]] = []
    for step in plan:
        if completed and step.name in completed:
            result = {"step": step.name, "status": "skipped", "message": "already completed by an earlier attempt",
                      "seconds": 0.0, "optional": step.optional, "parent_rss_mb": _rss_mb(),
                      "child_peak_rss_mb": _children_rss_mb()}
            print(f"[--] {step.name}: resumed, already done", file=log, flush=True)
            results.append(result)
            if on_step: on_step(result)
            continue
        if heartbeat: heartbeat()
        print(f"[ ] {step.name}: {step.description} parent_rss_mb={_rss_mb()}", file=log, flush=True)
        if not _env_satisfied(step):
            requirements = list(step.requires_all_env)
            if step.requires_env:
                requirements.append("one of " + ", ".join(step.requires_env))
            result = {"step": step.name, "status": "skipped", "message": f"needs {', '.join(requirements)}",
                      "seconds": 0.0, "optional": step.optional, "parent_rss_mb": _rss_mb(),
                      "child_peak_rss_mb": _children_rss_mb()}
        elif step.name == "cfbd-sync":
            result = _run_cfbd_split(season, root=root, timeout=timeout, log=log,
                                     datasets=datasets, heartbeat=heartbeat)
        elif step.name == "cfbd-current-player-stats":
            result = _run_player_stats_split(season, root=root, timeout=timeout, log=log,
                                             optional=step.optional, heartbeat=heartbeat)
        else:
            status, message, seconds = _run_command(step.command, timeout=int(step.timeout_seconds or timeout), log=log)
            result = {"step": step.name, "status": status, "message": message, "seconds": seconds,
                      "optional": step.optional, "parent_rss_mb": _rss_mb(), "child_peak_rss_mb": _children_rss_mb()}
        marker = {"success": "[ok]", "skipped": "[--]"}.get(result["status"], "[!!]")
        print(f"{marker} {result['status']} ({result['seconds']}s) parent_rss_mb={result['parent_rss_mb']} child_peak_rss_mb={result['child_peak_rss_mb']} {result['message']}", file=log, flush=True)
        results.append(result)
        if on_step: on_step(result)
    return results


def run_scheduled_refresh(season: int, *, profile: str = "heavy",
                          repo_root: str | Path | None = None,
                          stale_lock_hours: float = 1,
                          resume_hours: float = RESUME_WINDOW_HOURS,
                          only: list[str] | None = None,
                          phase_runner: Callable[..., list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    normalized_profile = profile.strip().casefold()
    if normalized_profile not in REFRESH_PROFILES:
        raise ValueError("profile must be one of " + ", ".join(sorted(REFRESH_PROFILES)))
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[1]
    state_override = (os.getenv("CFB_REFRESH_STATE_PATH") or "").strip()
    database_override = (os.getenv("CFB_DATABASE_PATH") or "").strip()
    if state_override:
        instance = Path(state_override)
        if not instance.is_absolute(): instance = root / instance
    elif database_override:
        database = Path(database_override)
        if not database.is_absolute(): database = root / database
        instance = database.parent
    else:
        instance = root / "instance"

    logs = instance / "refresh_logs"
    logs.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    lock = instance / "scheduled_refresh.lock"
    if not _acquire_lock(lock, started, stale_lock_hours):
        return {"status": "skipped", "reason": "refresh_already_running"}

    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    log_path = logs / f"refresh-{stamp}.log"
    if only is None:
        if normalized_profile == "light":
            only = LIGHT_REFRESH_STEPS
        elif normalized_profile == "scores":
            only = SCORES_REFRESH_STEPS
        elif normalized_profile == "results":
            only = RESULTS_REFRESH_STEPS
        elif normalized_profile == "heavy":
            # Heavy keeps every refresh step except the Google News crawl,
            # which now has its own bounded profile.
            only = [step.name for step in steps(season)
                    if "refresh" in step.phases and step.name != "local-articles"]
    datasets = SCORES_DATASETS if normalized_profile in {"scores", "results"} else None

    completed = _resumable_steps(instance, season, normalized_profile,
                                 window_hours=resume_hours, now=started)
    progress = {"season": season, "profile": normalized_profile, "started_at": started.isoformat(),
                "completed": False, "resumed_from": sorted(completed),
                "steps": {name: {"status": "success", "resumed": True,
                                 "message": "carried over from an earlier attempt"}
                          for name in sorted(completed)}}
    _write_progress(instance, progress)

    def record_step(result: dict[str, Any]) -> None:
        """Persist what the step did, not only that it finished.

        Every step already reports it -- "9 forecasts updated", "failed teams:
        ...", "scoped datasets complete: ..." -- and the status page has a
        column for exactly that, which falls back to the word "Completed" when
        there is nothing to show. Only status and time were written here, so
        that column read "Completed" for every row of every run, and a refresh
        that quietly did nothing looked identical to one that did the work.
        """
        recorded: dict[str, Any] = {"status": str(result.get("status")),
                                    "at": datetime.now(timezone.utc).isoformat()}
        message = str(result.get("message") or "").strip()
        if message:
            recorded["message"] = message[:240]
        # The counts the status page renders beside the message, when a step
        # reports them as numbers rather than prose.
        for key in ("added", "updated", "unchanged", "count"):
            value = result.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                recorded[key] = value
        progress["steps"][str(result.get("step"))] = recorded
        _write_progress(instance, progress)

    try:
        with log_path.open("w", encoding="utf-8") as log:
            print(f"scheduled refresh: profile={normalized_profile} season={season} pid={os.getpid()} parent_rss_mb={_rss_mb()}", file=log, flush=True)
            os.fsync(log.fileno())
            if normalized_profile == "news" and phase_runner is None:
                result = _run_news_shard(season, timeout=600, log=log)
                results = [result]
                record_step(result)
            elif phase_runner is None:
                results = _run_low_memory_phase(
                    "refresh", season, root=root, only=only, datasets=datasets, log=log,
                    heartbeat=lambda: _touch_lock(lock), completed=completed, on_step=record_step)
            else:
                # The other two branches record each step as it finishes. This
                # one returns them all at once, and recorded none of them, so
                # the seam the tests run through produced an empty progress
                # file and could not have caught anything about its contents.
                results = phase_runner("refresh", season, only=only, datasets=datasets)
                for result in results:
                    record_step(result)

        _refresh_statistics(instance)
        finished = datetime.now(timezone.utc)
        required_failures, degraded_steps, skipped_steps = classify_step_rows(
            results, segment=normalized_profile)
        exit_code = 1 if required_failures else 0
        status = "failed" if required_failures else "degraded" if degraded_steps else "success"
        report = {
            "status": status, "profile": normalized_profile, "season": season,
            "started_at": started.isoformat(), "finished_at": finished.isoformat(),
            "seconds": round((finished - started).total_seconds(), 1), "exit_code": exit_code,
            "log": str(log_path), "step_count": len(results), "degraded_steps": degraded_steps,
            "required_failures": required_failures, "skipped_steps": skipped_steps,
            "degraded_count": len(degraded_steps), "required_failure_count": len(required_failures),
            "parent_peak_rss_mb": _rss_mb(), "child_peak_rss_mb": _children_rss_mb(),
            "resumed_steps": sorted(completed),
        }
        progress["completed"] = True
        progress["finished_at"] = finished.isoformat()
        _write_progress(instance, progress)
        with (instance / "scheduled_refresh_history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, separators=(",", ":")) + "\n")
        try:
            summarize_segment(instance, report)
        except Exception:  # a roll-up write must never fail the refresh
            pass
        return report
    finally:
        lock.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    parser = argparse.ArgumentParser(description="Run one lock-safe scheduled refresh")
    parser.add_argument("--season", type=int, default=datetime.now().year)
    parser.add_argument("--profile", choices=tuple(sorted(REFRESH_PROFILES)), default="heavy")
    parser.add_argument("--stale-lock-hours", type=float, default=1)
    parser.add_argument("--resume-hours", type=float, default=RESUME_WINDOW_HOURS)
    parser.add_argument("--only", nargs="+", metavar="STEP")
    parser.add_argument("--list-steps", action="store_true")
    args = parser.parse_args(argv)
    if args.list_steps:
        for step in steps(args.season):
            if "refresh" in step.phases:
                print(f"{step.name:28} {'optional' if step.optional else 'required':9} {step.description}")
        return 0
    report = run_scheduled_refresh(args.season, profile=args.profile,
                                   resume_hours=args.resume_hours, only=args.only,
                                   repo_root=root, stale_lock_hours=args.stale_lock_hours)
    print(json.dumps(report, sort_keys=True))
    if report["status"] == "skipped":
        return 0
    return int(report.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
