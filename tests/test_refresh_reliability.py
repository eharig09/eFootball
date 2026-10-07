"""Regression tests for the refresh audit: each one pins a failure seen in real run history."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from sports_aggregator import process_probe, refresh_remediation, scheduled_refresh
from sports_aggregator.nfl import refresh_cli
from sports_aggregator.social import cli as social_cli


# -- reddit-validate crashed with AttributeError whenever an endpoint was unreachable ------------
def test_endpoint_exit_reports_unreachable_endpoints_without_crashing(capsys):
    results = [SimpleNamespace(endpoint_key="reddit:a", status="verified", permanent=False),
               SimpleNamespace(endpoint_key="reddit:b", status="unreachable", permanent=False)]
    assert social_cli._endpoint_exit(results, kind="reddit") == 1     # 50% unreachable > tolerance
    assert "reddit:b" in capsys.readouterr().out


def test_endpoint_exit_names_gone_handles_and_does_not_fail_for_them(capsys):
    results = [SimpleNamespace(endpoint_key=f"bsky:{i}", status="verified", permanent=False) for i in range(9)]
    results.append(SimpleNamespace(endpoint_key="bsky:dead", status="failed", permanent=True))
    assert social_cli._endpoint_exit(results, kind="bluesky") == 0
    assert "bsky:dead" in capsys.readouterr().out


# -- production's nfl-core-pbp failure was classified "unknown" ---------------------------------
def test_openblas_allocation_failure_is_classified_as_a_resource_problem():
    message = "OpenBLAS error: Memory allocation still failed after 10 retries, giving up."
    assert refresh_remediation.classify(status="failed", message=message) == "resource"


def test_a_rate_limit_message_is_an_upstream_error():
    message = "failed conferences: FBS Independents -- too many 429 error responses"
    assert refresh_remediation.classify(status="failed", message=message) == "upstream_error"


# -- a failed step reports its error, not whatever it printed last -------------------------------
def _log_with(tmp_path, text):
    path = tmp_path / "step.log"
    path.write_text("earlier output\n", encoding="utf-8")
    handle = path.open("a", encoding="utf-8")
    mark = path.stat().st_size
    handle.write(text)
    handle.flush()
    return handle, mark


def test_a_failed_step_reports_the_error_line_over_the_trailing_summary(tmp_path):
    handle, mark = _log_with(
        tmp_path,
        "starting\nOpenBLAS error: Memory allocation still failed after 10 retries\n"
        'nfl_game_coverage: {"completed_games": 63, "healthy": true}\n')
    try:
        assert "OpenBLAS" in scheduled_refresh._last_line(handle, mark, failed=True)
        assert scheduled_refresh._last_line(handle, mark).startswith("nfl_game_coverage")
    finally:
        handle.close()


def test_a_failed_step_with_no_error_line_falls_back_to_the_last_line(tmp_path):
    handle, mark = _log_with(tmp_path, "one\ntwo\n")
    try:
        assert scheduled_refresh._last_line(handle, mark, failed=True) == "two"
    finally:
        handle.close()


# -- CFBD rate limits: the same conferences failed in a third of runs ---------------------------
def _scripted(monkeypatch, outcomes):
    calls = []

    def fake(command, *, timeout, log, memory_mb=None):
        scope = command[-1]
        calls.append(scope)
        status = outcomes[scope].pop(0)
        return status, f"{scope}: {'ok' if status == 'success' else 'HTTP 429 too many requests'}", 1.0

    monkeypatch.setattr(scheduled_refresh, "_run_command", fake)
    return calls


def test_failed_scopes_get_one_retry_after_the_cooldown(monkeypatch, tmp_path):
    outcomes = {"ACC": ["success"], "MAC": ["failed", "success"], "MWC": ["failed", "failed"]}
    calls = _scripted(monkeypatch, outcomes)
    naps, reasons = [], {}
    with (tmp_path / "log.txt").open("w") as log:
        failed = scheduled_refresh._run_scoped_commands(
            "x", ["ACC", "MAC", "MWC"], lambda s: ["cmd", s], timeout=5, log=log,
            retry_failed_after=60.0, reasons=reasons, sleep=naps.append)
    assert failed == ["MWC"]                      # MAC recovered on the retry, MWC did not
    assert calls == ["ACC", "MAC", "MWC", "MAC", "MWC"]
    assert naps == [60.0]
    assert "429" in reasons["MWC"] and "MAC" not in reasons


def test_no_retry_is_attempted_unless_asked_for(monkeypatch, tmp_path):
    calls = _scripted(monkeypatch, {"A": ["failed"], "B": ["success"]})
    with (tmp_path / "log.txt").open("w") as log:
        failed = scheduled_refresh._run_scoped_commands(
            "x", ["A", "B"], lambda s: ["cmd", s], timeout=5, log=log, sleep=lambda s: pytest.fail("slept"))
    assert failed == ["A"] and calls == ["A", "B"]


def test_player_stats_summary_names_the_cause(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduled_refresh, "_conferences", lambda root: ["ACC", "MAC"])
    _scripted(monkeypatch, {"ACC": ["success"], "MAC": ["failed", "failed"]})
    with (tmp_path / "log.txt").open("w") as log:
        result = scheduled_refresh._run_player_stats_split(
            2026, root=tmp_path, timeout=5, log=log, optional=True, retry_after=0.0)
    assert result["status"] == "failed"
    assert result["message"].startswith("failed conferences: MAC -- ")
    assert "429" in result["message"]
    assert refresh_remediation.classify(status="failed", message=result["message"]) == "upstream_error"


# -- liveness / locks ---------------------------------------------------------------------------
def test_a_departed_process_is_detected_on_every_platform():
    import subprocess
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert process_probe.process_alive(process.pid) is False
    assert process_probe.process_alive(os.getpid()) is True
    assert process_probe.process_alive(0) is True and process_probe.process_alive(-1) is True


def test_lock_is_exclusive_and_a_dead_holder_is_reclaimed(tmp_path):
    lock = tmp_path / "x.lock"
    assert process_probe.try_lock(lock, stale_seconds=3600) is True
    assert process_probe.try_lock(lock, stale_seconds=3600) is False          # held by this live process
    lock.write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    assert process_probe.try_lock(lock, stale_seconds=3600, alive=lambda pid: False) is True


def test_lock_wait_queues_a_short_collision_then_gives_up(tmp_path):
    lock = tmp_path / "x.lock"
    assert process_probe.try_lock(lock, stale_seconds=3600)
    naps = []
    assert process_probe.lock_with_wait(lock, stale_seconds=3600, wait_seconds=12,
                                        poll_seconds=5, sleep=naps.append) is False
    assert naps == [5, 5, 5]
    # released after one nap: the waiter gets it
    state = {"n": 0}

    def release_on_second_nap(_):
        state["n"] += 1
        if state["n"] == 2:
            lock.unlink()

    assert process_probe.lock_with_wait(lock, stale_seconds=3600, wait_seconds=60,
                                        poll_seconds=5, sleep=release_on_second_nap) is True


# -- NFL refresh: serialised, isolated, bounded -------------------------------------------------
@pytest.fixture()
def nfl_state(tmp_path, monkeypatch):
    monkeypatch.setenv("NFL_REFRESH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(refresh_cli, "_nfl_repository", lambda: SimpleNamespace(path=str(tmp_path / "nfl.sqlite3")))
    return tmp_path


def _history(directory):
    return [json.loads(line) for line in (directory / "nfl_refresh_history.jsonl").read_text().splitlines()]


def test_an_nfl_segment_is_skipped_not_overlapped_when_another_is_running(nfl_state, monkeypatch):
    process_probe.try_lock(nfl_state / "nfl_refresh.lock", stale_seconds=3600)    # held by this live pid
    monkeypatch.setattr(refresh_cli, "LOCK_WAIT_SECONDS", 0.0)
    ran = []
    monkeypatch.setattr(refresh_cli, "_sync_weather", lambda season, **k: ran.append(season))
    assert refresh_cli.main(["weather", "--season", "2026"]) == 0
    assert ran == []
    last = _history(nfl_state)[-1]
    assert last["status"] == "skipped" and last["reason"] == "another_nfl_refresh_running"


def test_an_nfl_segment_runs_and_releases_the_lock(nfl_state, monkeypatch):
    ran = []
    monkeypatch.setattr(refresh_cli, "_sync_weather", lambda season, **k: ran.append(season))
    assert refresh_cli.main(["weather", "--season", "2026"]) == 0
    assert ran == [2026]
    assert not (nfl_state / "nfl_refresh.lock").exists()
    assert [row["status"] for row in _history(nfl_state)] == ["running", "success"]


def test_the_lock_is_released_when_the_segment_fails(nfl_state, monkeypatch):
    def explode(season, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(refresh_cli, "_sync_weather", explode)
    assert refresh_cli.main(["weather", "--season", "2026"]) == 1
    assert not (nfl_state / "nfl_refresh.lock").exists()
    assert _history(nfl_state)[-1]["status"] == "failed"


def test_history_is_bounded(nfl_state, monkeypatch):
    monkeypatch.setattr(refresh_cli, "HISTORY_MAX_BYTES", 2000)
    monkeypatch.setattr(refresh_cli, "HISTORY_KEEP_ROWS", 10)
    for i in range(200):
        refresh_cli._append_history({"segment": "x", "i": i, "status": "success"})
    rows = _history(nfl_state)
    assert len(rows) < 60 and rows[-1]["i"] == 199          # trimmed, newest kept


def test_the_test_run_does_not_write_into_the_real_history():
    """conftest points NFL_REFRESH_STATE_DIR at a temp dir for the whole session."""
    assert os.environ.get("NFL_REFRESH_STATE_DIR")
    assert "instance" not in os.environ["NFL_REFRESH_STATE_DIR"]


# -- NFL status page: segments fell out of a 20-row window and read "unknown" ---------------------
from datetime import datetime, timedelta, timezone

from sports_aggregator.nfl import refresh_status

NOW = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)


def _row(segment, status, *, minutes_ago, pid=1, seconds=None):
    started = NOW - timedelta(minutes=minutes_ago)
    row = {"segment": segment, "status": status, "pid": pid, "started_at": started.isoformat()}
    if status in ("success", "failed", "skipped"):
        row["finished_at"] = (started + timedelta(seconds=seconds or 5)).isoformat()
        row["seconds"] = seconds or 5
    return row


def _tiles(rows, *segments):
    return {t["segment"]: t for t in refresh_status.segment_health(rows, segments, now=NOW,
                                                                      relative=lambda v: "ago")}


def test_a_segment_that_ran_long_ago_is_not_unknown_just_because_availability_is_chatty():
    rows = [_row("core-stats", "running", minutes_ago=600, pid=7), _row("core-stats", "success", minutes_ago=600, pid=7)]
    for i in range(400):                       # 800 availability rows: far more than the old 20-row window
        rows.append(_row("availability", "running", minutes_ago=300 - i * 0.7, pid=100 + i))
        rows.append(_row("availability", "success", minutes_ago=300 - i * 0.7, pid=100 + i))
    tiles = _tiles(rows, "core-stats", "availability", "pff")
    assert tiles["core-stats"]["status"] == "success"
    assert tiles["availability"]["status"] == "success"
    assert tiles["pff"]["status"] == "unknown"


def test_a_run_that_started_and_never_finished_is_reported_as_interrupted():
    rows = [_row("core-depth", "success", minutes_ago=900, pid=1),
            _row("core-depth", "running", minutes_ago=400, pid=2)]     # started 6h40 ago, no result
    tile = _tiles(rows, "core-depth")["core-depth"]
    assert tile["status"] == "interrupted"
    assert "killed" in tile["error"] and tile["last_success"] == "ago"


def test_a_recent_run_without_a_result_is_still_running():
    rows = [_row("content", "running", minutes_ago=4, pid=3)]
    assert _tiles(rows, "content")["content"]["status"] == "running"


def test_a_finished_run_is_not_mistaken_for_an_interrupted_one():
    rows = [_row("content", "running", minutes_ago=400, pid=3), _row("content", "success", minutes_ago=400, pid=3)]
    assert _tiles(rows, "content")["content"]["status"] == "success"


def test_a_failure_keeps_the_last_success_visible():
    rows = [_row("weather", "success", minutes_ago=900, pid=1), _row("weather", "failed", minutes_ago=60, pid=2)]
    tile = _tiles(rows, "weather")["weather"]
    assert tile["status"] == "failed" and tile["last_success"] == "ago"


def test_a_skipped_run_is_labelled_as_yielding():
    rows = [_row("weather", "skipped", minutes_ago=5, pid=2)]
    tile = _tiles(rows, "weather")["weather"]
    assert tile["status"] == "skipped" and "another refresh" in tile["error"]


def test_unreadable_history_lines_are_ignored(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text('{"segment":"content","status":"success"}\nnot json\n[1,2]\n', encoding="utf-8")
    assert len(refresh_status.read_history(path)) == 1
    assert refresh_status.read_history(tmp_path / "missing.jsonl") == []


# -- the CFB hook does not spawn a process just to find the lock held ---------------------------
def _hook_app(tmp_path):
    from app import create_app
    return create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False, "CFB_REFRESH_TOKEN": "t",
                       "CFB_DEFAULT_SEASON": 2026, "CFB_DATABASE_PATH": str(tmp_path / "cfb.sqlite3")})


def _post_hook(app):
    from unittest.mock import patch
    with patch("app.subprocess.Popen") as popen:
        response = app.test_client().post("/internal/cfb-refresh?profile=light&segment=models",
                                          headers={"Authorization": "Bearer t"})
    return response, popen


def test_the_hook_spawns_nothing_while_a_refresh_holds_the_lock(tmp_path):
    process_probe.try_lock(tmp_path / "scheduled_refresh.lock", stale_seconds=3600)   # live pid: ours
    response, popen = _post_hook(_hook_app(tmp_path))
    assert response.status_code == 200
    assert response.get_json() == {"status": "skipped", "reason": "refresh_already_running",
                                   "season": 2026, "profile": "light", "segment": "models"}
    popen.assert_not_called()


def test_the_hook_starts_a_refresh_when_the_lock_is_free_or_its_holder_is_dead(tmp_path):
    response, popen = _post_hook(_hook_app(tmp_path))
    assert response.status_code == 202 and popen.called
    (tmp_path / "scheduled_refresh.lock").write_text(json.dumps({"pid": 2 ** 22 + 12345}), encoding="utf-8")
    assert not process_probe.lock_is_held(tmp_path / "scheduled_refresh.lock", stale_seconds=3600)
    response, popen = _post_hook(_hook_app(tmp_path))
    assert response.status_code == 202 and popen.called


def test_a_stale_lock_does_not_block_the_hook(tmp_path):
    lock = tmp_path / "scheduled_refresh.lock"
    process_probe.try_lock(lock, stale_seconds=3600)
    old = lock.stat().st_mtime - 2 * 3600
    os.utime(lock, (old, old))
    assert not process_probe.lock_is_held(lock, stale_seconds=3600)


# -- a failing step explains itself: its own output is kept and shown --------------------------------
from datetime import datetime as _dt, timezone as _tz

from sports_aggregator import refresh_health


def test_the_excerpt_is_the_end_of_that_steps_output_only(tmp_path):
    handle, mark = _log_with(tmp_path, "".join(f"line {i}\n" for i in range(40)) + "\nlast one\n")
    try:
        excerpt = scheduled_refresh._step_excerpt(handle, mark, lines=5)
        assert excerpt.splitlines() == ["line 36", "line 37", "line 38", "line 39", "last one"]
        assert "earlier output" not in scheduled_refresh._step_excerpt(handle, mark, lines=500)
        assert len(scheduled_refresh._step_excerpt(handle, mark, chars=30)) == 30
    finally:
        handle.close()


def test_the_detail_travels_from_a_failed_step_to_the_attention_item(tmp_path):
    results = [{"step": "nfl-core-stats", "status": "failed", "message": "exit code 1", "optional": True,
                "detail": "weekly_stats: failed (0) -- Unable to allocate 310 MiB for an array"}]
    required, degraded, _ = refresh_health.classify_step_rows(results, segment="analytics")
    assert degraded[0]["detail"].startswith("weekly_stats: failed")
    refresh_health.summarize_segment(tmp_path, {
        "profile": "analytics", "status": "degraded", "finished_at": _dt.now(_tz.utc).isoformat(),
        "degraded_steps": degraded, "required_failures": [], "skipped_steps": [], "seconds": 5, "step_count": 1})
    [item] = refresh_health.attention_items(tmp_path)
    assert "Unable to allocate" in item["detail"]


def test_a_step_that_succeeded_carries_no_detail():
    required, degraded, _ = refresh_health.classify_step_rows(
        [{"step": "x", "status": "success", "message": "ok", "detail": "noise"}])
    assert required == [] and degraded == []


def test_the_status_page_shows_the_step_output(tmp_path):
    from flask import render_template_string
    from app import create_app
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})
    html = open("templates/cfb_data_status.html", encoding="utf-8").read()
    assert "item.detail" in html and "Step output" in html


# -- the NFL CLI no longer drops the reason a dataset failed ------------------------------------------
def test_a_failed_nfl_dataset_prints_its_reason(nfl_state, monkeypatch, capsys):
    from sports_aggregator.nfl import sync as sync_module
    from sports_aggregator.nfl import data_health
    from sports_aggregator.nfl.models import SyncDatasetResult, SyncReport

    report = SyncReport(2026, _dt.now(_tz.utc), _dt.now(_tz.utc), (
        SyncDatasetResult("weekly_stats", 0, "failed", "MemoryError: Unable to allocate 310 MiB"),
        SyncDatasetResult("snap_counts", 5970, "success")))

    class FakeSync:
        def __init__(self, *a, **k): ...
        def sync(self, *a, **k): return report

    monkeypatch.setattr(sync_module, "NFLDataSync", FakeSync)
    monkeypatch.setattr(refresh_cli, "_nflverse_client", lambda: None)
    monkeypatch.setattr(data_health, "season_coverage", lambda repository, season: {"healthy": True})
    assert refresh_cli._sync_core(2026, include_pbp=False, only=frozenset({"weekly_stats"}), include_extras=False) is False
    out = capsys.readouterr().out
    assert "weekly_stats: failed (0) -- MemoryError: Unable to allocate 310 MiB" in out
    assert "snap_counts: success (5970)" in out and " -- " not in out.split("snap_counts")[1].splitlines()[0]


# -- children get allocator settings that keep virtual reservations near real use ------------------------
def test_children_default_to_the_system_allocator_and_few_arenas(monkeypatch):
    for key in scheduled_refresh.CHILD_ENV_DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    env = scheduled_refresh._child_env()
    assert env["ARROW_DEFAULT_MEMORY_POOL"] == "system" and env["MALLOC_ARENA_MAX"] == "2"
    monkeypatch.setenv("MALLOC_ARENA_MAX", "8")                     # an explicit choice wins
    assert scheduled_refresh._child_env()["MALLOC_ARENA_MAX"] == "8"


# -- the deployed commit is visible, so "is the fix live?" has an answer -----------------------------
def test_the_deployed_commit_is_reported_when_the_platform_provides_it(monkeypatch):
    from sports_aggregator.cfb.data_status import deployed_build
    monkeypatch.setenv("RENDER_GIT_COMMIT", "eee4332a1b2c3d4e5f60718293a4b5c6d7e8f901")
    monkeypatch.setenv("RENDER_GIT_BRANCH", "main")
    assert deployed_build() == {"commit": "eee4332", "branch": "main"}
    monkeypatch.delenv("RENDER_GIT_COMMIT")
    assert deployed_build() is None


def test_the_status_page_and_api_show_the_build(monkeypatch, tmp_path):
    from app import create_app
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc1234def")
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "CFB_DATABASE_PATH": str(tmp_path / "cfb.sqlite3")})
    client = app.test_client()
    assert "deployed build <code>abc1234</code>" in client.get("/college-football/data-status/").get_data(as_text=True)
    assert client.get("/api/v1/cfb/status").get_json()["deploy"]["commit"] == "abc1234"
    monkeypatch.delenv("RENDER_GIT_COMMIT")
    assert "deployed build" not in client.get("/college-football/data-status/").get_data(as_text=True)
    assert client.get("/api/v1/cfb/status").get_json()["deploy"] is None


# -- the NFL "Recent refreshes" table: one row per run, unfinished runs labelled honestly ------------
def _hist(segment, status, minutes_ago, **extra):
    started = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    row = {"segment": segment, "status": status, "started_at": started, "seconds": None, **extra}
    if status in ("success", "failed"):
        row["finished_at"] = started
    return row


def test_a_finished_run_appears_once_not_as_running_and_success():
    rows = [_hist("content", "success", 60, seconds=140.4), _hist("content", "running", 60)]
    shown = refresh_status.display_rows(rows, now=NOW)
    assert [(r["segment"], r["status"]) for r in shown] == [("content", "success")]


def test_a_young_unfinished_run_is_in_progress_and_an_old_one_is_interrupted():
    shown = refresh_status.display_rows([_hist("core-stats", "running", 9), _hist("core-depth", "running", 600)], now=NOW)
    assert (shown[0]["status"], shown[0]["note"]) == ("running", "In progress")
    assert shown[1]["status"] == "interrupted" and "killed" in shown[1]["note"]


def test_display_rows_does_not_modify_its_input():
    rows = [_hist("core-stats", "running", 600)]
    refresh_status.display_rows(rows, now=NOW)
    assert rows[0]["status"] == "running" and "note" not in rows[0]


def test_the_table_template_never_prints_none_or_completed_for_an_unfinished_run():
    html = open("templates/nfl_data_status.html", encoding="utf-8").read()
    assert "run.seconds is not none" in html and "run.note" in html
    assert "run.seconds is defined" not in html


# -- the container's own memory limit and OOM-kill count, readable without the Render dashboard ---------
def _cgroup_v2(tmp_path, *, limit="536870912", current="301989888", peak="480000000", oom=3):
    (tmp_path / "memory.max").write_text(limit)
    (tmp_path / "memory.current").write_text(current)
    (tmp_path / "memory.peak").write_text(peak)
    (tmp_path / "memory.events").write_text(f"low 0\nhigh 0\nmax 12\noom 3\noom_kill {oom}\n")
    return tmp_path


def test_host_resources_reads_a_cgroup_v2_container(tmp_path):
    from sports_aggregator.cfb.data_status import host_resources
    info = host_resources(_cgroup_v2(tmp_path))
    assert (info["memory_limit_mb"], info["memory_used_mb"], info["memory_peak_mb"], info["oom_kills"]) == (512, 288, 458, 3)


def test_an_unlimited_container_reports_no_limit(tmp_path):
    from sports_aggregator.cfb.data_status import host_resources
    info = host_resources(_cgroup_v2(tmp_path, limit="max"))
    assert info["memory_limit_mb"] is None and info["memory_used_mb"] == 288


def test_host_resources_reads_a_cgroup_v1_container(tmp_path):
    from sports_aggregator.cfb.data_status import host_resources
    legacy = tmp_path / "memory"
    legacy.mkdir()
    (legacy / "memory.limit_in_bytes").write_text("2147483648")
    (legacy / "memory.usage_in_bytes").write_text("1073741824")
    (legacy / "memory.oom_control").write_text("oom_kill_disable 0\nunder_oom 0\noom_kill 2\n")
    info = host_resources(tmp_path)
    assert (info["memory_limit_mb"], info["memory_used_mb"], info["oom_kills"]) == (2048, 1024, 2)


def test_no_cgroup_means_no_report(tmp_path):
    from sports_aggregator.cfb.data_status import host_resources
    assert host_resources(tmp_path / "missing") is None
    assert host_resources(tmp_path) is None                       # a directory with nothing readable in it


def test_the_status_page_and_api_show_memory_and_kills(monkeypatch, tmp_path):
    from app import create_app
    from sports_aggregator.cfb import data_status
    monkeypatch.setattr(data_status, "host_resources",
                        lambda root=None: {"memory_limit_mb": 512, "memory_used_mb": 288, "memory_peak_mb": 458,
                                           "oom_kills": 3, "cpus": 1})
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "CFB_DATABASE_PATH": str(tmp_path / "cfb.sqlite3")})
    client = app.test_client()
    page = client.get("/college-football/data-status/").get_data(as_text=True)
    assert "memory 288 of 512 MB" in page and "3 killed for memory" in page


# -- the supervisor: a refresh that dies without a result is recorded, with how it died -------------------
from sports_aggregator.nfl import run_segment


def _running_row(pid, started="2026-10-06T12:00:00+00:00", segment="core-depth"):
    return {"segment": segment, "season": 2026, "pid": pid, "started_at": started, "status": "running"}


def test_exit_descriptions_name_the_signal_or_the_code():
    assert run_segment.describe_exit(-11) == "killed by SIGSEGV"
    assert run_segment.describe_exit(-999).startswith("killed by signal")
    assert run_segment.describe_exit(1) == "exit code 1"


def test_a_child_that_died_without_a_result_is_recorded_as_crashed(nfl_state):
    refresh_cli._append_history(_running_row(4321))
    wrote = run_segment.record_if_unreported(["core-depth", "--season", "2026"], 4321, -11,
                                             supervised_at=NOW)
    assert wrote is True
    row = _history(nfl_state)[-1]
    assert (row["status"], row["exit"], row["segment"], row["season"]) == ("crashed", "killed by SIGSEGV", "core-depth", 2026)
    assert row["started_at"] == "2026-10-06T12:00:00+00:00" and row["pid"] == 4321      # pairs with its running row


def test_a_child_that_reported_its_own_outcome_is_left_alone(nfl_state):
    refresh_cli._append_history(_running_row(4321))
    refresh_cli._append_history({**_running_row(4321), "status": "failed", "error": "boom"})
    assert run_segment.record_if_unreported(["core-depth", "--season", "2026"], 4321, 1, supervised_at=NOW) is False
    assert [r["status"] for r in _history(nfl_state)] == ["running", "failed"]


def test_a_clean_exit_records_nothing(nfl_state):
    assert run_segment.record_if_unreported(["weather", "--season", "2026"], 99, 0, supervised_at=NOW) is False
    assert not (nfl_state / "nfl_refresh_history.jsonl").exists()


def test_a_child_that_died_before_even_starting_is_still_recorded(nfl_state):
    run_segment.record_if_unreported(["core-stats", "--season", "2026"], 77, 3, supervised_at=NOW)
    row = _history(nfl_state)[-1]
    assert row["status"] == "crashed" and row["exit"] == "exit code 3" and row["reported_start"] is False


def test_the_supervisor_runs_the_cli_and_records_a_crash(nfl_state, monkeypatch):
    refresh_cli._append_history(_running_row(555, segment="core-stats"))

    class DeadChild:
        pid = 555
        def wait(self): return -11
        def send_signal(self, signum): ...

    seen = {}
    monkeypatch.setattr(run_segment.subprocess, "Popen", lambda cmd, **k: seen.setdefault("cmd", cmd) and DeadChild())
    assert run_segment.main(["core-stats", "--season", "2026"]) != 0
    assert seen["cmd"][1:4] == ["-m", "sports_aggregator.nfl.refresh_cli", "core-stats"]
    assert _history(nfl_state)[-1]["status"] == "crashed"


def test_the_web_hook_launches_refreshes_through_the_supervisor(tmp_path):
    from unittest.mock import patch
    from app import create_app
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False, "CFB_REFRESH_TOKEN": "t",
                      "CFB_DEFAULT_SEASON": 2026, "NFL_DATABASE_PATH": str(tmp_path / "nfl.sqlite3"),
                      "CFB_DATABASE_PATH": str(tmp_path / "cfb.sqlite3")})
    with patch("app.subprocess.Popen") as popen:
        response = app.test_client().post("/internal/nfl-refresh?segment=core-depth",
                                          headers={"Authorization": "Bearer t"})
    assert response.status_code == 202
    assert popen.call_args.args[0][1:3] == ["-m", "sports_aggregator.nfl.run_segment"]


def test_a_crash_shows_on_the_tile_and_in_the_table(nfl_state):
    rows = [{**_running_row(9), "started_at": (NOW - timedelta(minutes=30)).isoformat()},
            {**_running_row(9), "started_at": (NOW - timedelta(minutes=30)).isoformat(), "status": "crashed",
             "exit": "killed by SIGSEGV", "seconds": 12.0, "finished_at": (NOW - timedelta(minutes=29)).isoformat()}]
    tile = refresh_status.segment_health(rows, ["core-depth"], now=NOW, relative=lambda v: "ago")[0]
    assert tile["status"] == "crashed" and "killed by SIGSEGV" in tile["error"]
    shown = refresh_status.display_rows(list(reversed(rows)), now=NOW)
    assert len(shown) == 1 and shown[0]["note"] == "Crashed: killed by SIGSEGV"


def test_faulthandler_is_armed_for_the_run_and_disarmed_after(nfl_state, monkeypatch):
    import faulthandler
    calls = []
    monkeypatch.setattr(faulthandler, "dump_traceback_later", lambda *a, **k: calls.append(("arm", a, k)))
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", lambda: calls.append(("cancel",)))
    monkeypatch.setattr(refresh_cli, "_sync_weather", lambda season, **k: None)
    assert refresh_cli.main(["weather", "--season", "2026"]) == 0
    assert calls[0][0] == "arm" and calls[0][1] == (refresh_cli.FAULT_DUMP_SECONDS,) and calls[0][2] == {"repeat": True}
    assert calls[-1] == ("cancel",)
