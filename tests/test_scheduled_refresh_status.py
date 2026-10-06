from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sports_aggregator import scheduled_refresh
from sports_aggregator.scheduled_refresh import (
    _acquire_lock, _process_alive, _touch_lock, run_scheduled_refresh)


def _phase_runner(results: list[dict]):
    """Stand in for the step loop, returning the results it would produce.

    `ae2a4fd` replaced the old `runner` hook, which returned log text to be
    parsed, with `phase_runner`, which returns step results directly. These
    tests were not moved across; the module could not be imported on Windows,
    so the breakage stayed invisible.
    """
    captured: list[dict] = []

    def run(phase, season, *, only=None, datasets=None):
        captured.append({"phase": phase, "only": only, "datasets": datasets})
        return results
    run.captured = captured
    return run


def test_optional_step_failure_marks_refresh_degraded(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CFB_REFRESH_STATE_PATH", str(tmp_path / "instance"))
    runner = _phase_runner([
        {"step": "cfbd-lines", "status": "success", "message": "lines=42",
         "seconds": 0.5, "optional": True},
        {"step": "bluesky", "status": "failed",
         "message": "endpoints=18 succeeded=17 errors=1",
         "seconds": 1.2, "optional": True},
    ])

    report = run_scheduled_refresh(2026, repo_root=tmp_path, phase_runner=runner)

    assert report["status"] == "degraded"
    assert report["exit_code"] == 0
    assert report["degraded_count"] == 1
    assert report["degraded_steps"][0]["step"] == "bluesky"
    assert report["degraded_steps"][0]["status"] == "failed"
    # the row now carries what to do about it, not only that it failed
    assert report["degraded_steps"][0]["category"] in {
        "unknown", "empty_result", "upstream_error"}
    assert report["degraded_steps"][0]["self_heals"] is False

    # and the per-segment roll-up the status page reads was written
    health = json.loads(
        (tmp_path / "instance" / "segment_health.json").read_text(encoding="utf-8"))
    assert health["heavy"]["last_status"] == "degraded"
    assert health["heavy"]["open_issues"][0]["step"] == "bluesky"


def test_clean_optional_steps_keep_refresh_successful(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CFB_REFRESH_STATE_PATH", str(tmp_path / "instance"))
    runner = _phase_runner([
        {"step": "bluesky", "status": "success",
         "message": "endpoints=18 succeeded=18 errors=0",
         "seconds": 1.0, "optional": True},
    ])

    report = run_scheduled_refresh(2026, repo_root=tmp_path, phase_runner=runner)

    assert report["status"] == "success"
    assert report["degraded_count"] == 0
    assert report["degraded_steps"] == []


# ---------------------------------------------------------------------------
# Lock reclamation
#
# A refresh killed by the platform never reaches its `finally`, so the lock
# survives it. Nothing read the recorded pid back, and the only other release
# path was a six-hour age check, so one out-of-memory kill disabled refreshes
# for the rest of the morning — silently, as "refresh_already_running".
# ---------------------------------------------------------------------------

def _write_lock(path: Path, pid: int, started: str = "2026-08-25T10:00:37+00:00") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "started_at": started}), encoding="utf-8")


def test_a_lock_whose_owner_is_gone_is_reclaimed_immediately(tmp_path, monkeypatch):
    """The reclaim decision, independent of how liveness is detected.

    The mtime is fresh, so the age fallback would refuse this lock; only the
    liveness check can take it.
    """
    monkeypatch.setattr(scheduled_refresh, "_process_alive", lambda pid: False)
    lock = tmp_path / "scheduled_refresh.lock"
    _write_lock(lock, 4242)
    assert _acquire_lock(lock, datetime.now(timezone.utc), stale_hours=6) is True
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_a_departed_process_is_actually_detected_as_gone():
    """The probe itself, on the platform this runs on in production."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert _process_alive(process.pid) is False


def test_a_lock_held_by_a_live_process_is_respected(tmp_path: Path):
    lock = tmp_path / "scheduled_refresh.lock"
    _write_lock(lock, os.getpid())
    assert _acquire_lock(lock, datetime.now(timezone.utc), stale_hours=6) is False


def test_an_unreadable_lock_still_ages_out(tmp_path: Path):
    """A truncated lock must not become permanent."""
    lock = tmp_path / "scheduled_refresh.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("{not json", encoding="utf-8")
    assert _acquire_lock(lock, datetime.now(timezone.utc), stale_hours=6) is False
    old = datetime.now(timezone.utc).timestamp() - 7 * 3600
    os.utime(lock, (old, old))
    assert _acquire_lock(lock, datetime.now(timezone.utc), stale_hours=6) is True


def test_progress_keeps_a_working_refresh_from_ageing_out(tmp_path: Path):
    lock = tmp_path / "scheduled_refresh.lock"
    _write_lock(lock, os.getpid())
    stale = datetime.now(timezone.utc).timestamp() - 2 * 3600
    os.utime(lock, (stale, stale))
    _touch_lock(lock)
    assert _acquire_lock(lock, datetime.now(timezone.utc), stale_hours=1) is False


def test_an_uncertain_pid_is_never_assumed_dead(tmp_path: Path):
    """A false 'alive' costs a wait; a false 'dead' runs two refreshes at once."""
    assert _process_alive(0) is True
    assert _process_alive(-1) is True
    assert _process_alive(os.getpid()) is True




def test_the_cli_stale_window_matches_the_function_default():
    """The CLI passes its own default explicitly, so a mismatch silently wins.

    run_scheduled_refresh dropped to an hour, but argparse still handed it six,
    which is what production actually used.
    """
    import argparse
    import inspect

    from sports_aggregator import scheduled_refresh as module

    signature = inspect.signature(module.run_scheduled_refresh)
    function_default = signature.parameters["stale_lock_hours"].default

    parser = argparse.ArgumentParser()
    parser.add_argument("--stale-lock-hours", type=float, default=1)
    source = inspect.getsource(module.main)
    assert "default=1" in source, "CLI default drifted from the function default"
    assert function_default == 1


def test_a_scores_pass_narrows_both_the_steps_and_the_datasets(
        tmp_path: Path, monkeypatch):
    """The whole point of the profile: it must not run the roster crawl.

    A game-day pass fires every quarter hour during a slate. If it planned the
    full twenty-two steps it would take eight minutes and hold the lock through
    the next four firings.
    """
    from sports_aggregator.scheduled_refresh import (
        SCORES_DATASETS, SCORES_REFRESH_STEPS)

    monkeypatch.setenv("CFB_REFRESH_STATE_PATH", str(tmp_path / "instance"))
    runner = _phase_runner([
        {"step": "cfbd-sync", "status": "success", "message": "scoped to games",
         "seconds": 2.2, "optional": False},
    ])

    report = run_scheduled_refresh(
        2026, profile="scores", repo_root=tmp_path, phase_runner=runner)

    assert report["profile"] == "scores"
    assert runner.captured[0]["only"] == SCORES_REFRESH_STEPS
    assert runner.captured[0]["datasets"] == SCORES_DATASETS


def test_a_heavy_pass_takes_everything_but_the_news_crawl(tmp_path: Path, monkeypatch):
    """Heavy used to scope nothing at all. `local-articles` walked 350 Google
    News feeds and ate the run, so it was given its own bounded profile and
    heavy now names every other refresh step rather than sweeping it back in.
    """
    from sports_aggregator.bootstrap import steps
    monkeypatch.setenv("CFB_REFRESH_STATE_PATH", str(tmp_path / "instance"))
    runner = _phase_runner([])
    run_scheduled_refresh(2026, profile="heavy", repo_root=tmp_path,
                          phase_runner=runner)

    scoped = runner.captured[0]["only"]
    expected = [step.name for step in steps(2026) if "refresh" in step.phases]
    assert "local-articles" not in scoped
    assert set(scoped) == set(expected) - {"local-articles"}
    # Datasets stay unscoped: heavy is the pass that refreshes all of them.
    assert runner.captured[0]["datasets"] is None


def test_what_each_step_did_is_written_down_not_just_that_it_finished(
        tmp_path: Path, monkeypatch):
    """The status page's "What changed" column read "Completed" for every row.

    Not because nothing changed: every step reports what it did, and the page
    renders it. The progress file recorded only status and time and dropped the
    rest, so a refresh that quietly did nothing looked exactly like one that
    did the work.
    """
    monkeypatch.setenv("CFB_REFRESH_STATE_PATH", str(tmp_path / "instance"))
    runner = _phase_runner([
        {"step": "weather", "status": "success", "message": "9 forecasts updated",
         "seconds": 0.4, "updated": 9},
        {"step": "cfbd-lines", "status": "success",
         "message": "scoped datasets complete: betting_lines", "seconds": 0.5},
    ])

    run_scheduled_refresh(2026, repo_root=tmp_path, phase_runner=runner)

    steps = json.loads(
        (tmp_path / "instance" / "refresh_progress.json").read_text(encoding="utf-8")
    )["steps"]
    assert steps["weather"]["message"] == "9 forecasts updated"
    assert steps["weather"]["updated"] == 9
    assert steps["cfbd-lines"]["message"] == "scoped datasets complete: betting_lines"


def test_a_failing_step_records_why_it_failed(tmp_path: Path, monkeypatch):
    """The column matters most when something went wrong, and that is exactly
    when "Completed" was the least true thing the page could say."""
    monkeypatch.setenv("CFB_REFRESH_STATE_PATH", str(tmp_path / "instance"))
    runner = _phase_runner([
        {"step": "cfbd-current-player-stats", "status": "failed",
         "message": "0 rows across 10 conferences", "seconds": 2.0, "optional": True},
    ])

    run_scheduled_refresh(2026, repo_root=tmp_path, phase_runner=runner)

    steps = json.loads(
        (tmp_path / "instance" / "refresh_progress.json").read_text(encoding="utf-8")
    )["steps"]
    assert steps["cfbd-current-player-stats"]["message"] == "0 rows across 10 conferences"


def test_a_long_message_is_trimmed_before_it_reaches_the_file(
        tmp_path: Path, monkeypatch):
    """A step that lists every failure can produce a very long line, and this
    file is rewritten after each step."""
    monkeypatch.setenv("CFB_REFRESH_STATE_PATH", str(tmp_path / "instance"))
    runner = _phase_runner([
        {"step": "weather", "status": "failed", "message": "x" * 4000, "seconds": 1.0},
    ])

    run_scheduled_refresh(2026, repo_root=tmp_path, phase_runner=runner)

    steps = json.loads(
        (tmp_path / "instance" / "refresh_progress.json").read_text(encoding="utf-8")
    )["steps"]
    assert len(steps["weather"]["message"]) == 240


def test_a_step_reports_its_own_last_line_not_its_exit_code(tmp_path: Path):
    """Steps write into the shared log rather than through a pipe, so nothing
    held on to what they said and every successful step reported "exit code 0"
    -- in the column meant for what changed."""
    from sports_aggregator.scheduled_refresh import _run_command

    log_path = tmp_path / "refresh.log"
    with log_path.open("w", encoding="utf-8") as log:
        print("[ ] bluesky-resolve: starting", file=log, flush=True)
        # A real subprocess that prints a summary line, the way these steps do.
        status, message, _seconds = _run_command(
            ["timeit", "-n", "1", "-r", "1", "pass"], timeout=60, log=log)

    assert status == "success"
    assert message and not message.startswith("exit code"), message
    # And the line it reported is genuinely the last thing the step printed.
    tail = [line.strip() for line in
            log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert message == tail[-1][:240]


def test_a_step_that_prints_nothing_still_reports_something(tmp_path: Path):
    from sports_aggregator.scheduled_refresh import _run_command

    log_path = tmp_path / "refresh.log"
    with log_path.open("w", encoding="utf-8") as log:
        status, message, _ = _run_command(["compileall", "-q", str(tmp_path)],
                                          timeout=60, log=log)

    assert status == "success"
    assert message  # falls back to the exit code rather than an empty column


def test_one_step_does_not_report_the_previous_step_s_output(tmp_path: Path):
    """Every step appends to the same file, so the read has to start where
    this step started rather than at the top."""
    from sports_aggregator.scheduled_refresh import _run_command

    log_path = tmp_path / "refresh.log"
    with log_path.open("w", encoding="utf-8") as log:
        _run_command(["timeit", "-n", "1", "-r", "1", "pass"], timeout=60, log=log)
        print("SENTINEL FROM THE HARNESS", file=log, flush=True)
        _status, message, _ = _run_command(["compileall", "-q", str(tmp_path)],
                                           timeout=60, log=log)

    assert "SENTINEL" not in message
