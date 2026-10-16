"""The odds sweep rides on the app's refresh runs, ahead of the pick freeze, and can never fail a run."""

import os
from pathlib import Path

import pytest

from sports_aggregator import odds_api_tracker as tracker
from sports_aggregator import tracked_refresh as tr
from sports_aggregator.nfl import refresh_cli
from sports_aggregator.refresh_health import classify_step_rows


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "k")


def step(monkeypatch, result=None, error=None):
    def fake_auto(league, **_kwargs):
        assert league == "cfb"
        if error:
            raise error
        return result
    monkeypatch.setattr(tracker, "auto", fake_auto)
    return tr._refresh_odds_lines(2026, root=Path("."), log=None)


# ------------------------------------------------------------------ the college step

def test_no_key_is_listed_as_not_configured_not_as_a_failure(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "")
    row = tr._refresh_odds_lines(2026, root=Path("."), log=None)
    assert (row["step"], row["status"], row["optional"]) == ("odds-lines", "skipped", True)
    required, degraded, skipped = classify_step_rows([row], segment="core")
    assert not required and not degraded
    assert skipped and skipped[0]["category"] == "not_configured"


def test_a_sweep_that_stored_lines_reports_what_it_did(monkeypatch, key):
    row = step(monkeypatch, {"status": "ok", "mode": "refresh", "rows_stored": 12, "changed_games": 5,
                             "credits_remaining": 380})
    assert row["status"] == "success"
    assert "refresh: 12 new line rows across 5 games; 380 credits left" == row["message"]


@pytest.mark.parametrize("outcome", ["not-due", "no-games"])
def test_an_expected_refusal_is_a_quiet_success(monkeypatch, key, outcome):
    row = step(monkeypatch, {"status": outcome, "message": "nothing to do"})
    assert row["status"] == "success"
    required, degraded, _ = classify_step_rows([row], segment="core")
    assert not required and not degraded


def test_a_spent_budget_is_not_an_issue(monkeypatch, key):
    row = step(monkeypatch, {"status": "budget", "message": "monthly cap"})
    assert row["status"] == "skipped"
    required, degraded, skipped = classify_step_rows([row], segment="core")
    assert not required and not degraded and not skipped


def test_an_api_error_degrades_the_run_but_never_fails_it(monkeypatch, key):
    row = step(monkeypatch, {"status": "error", "message": "odds request failed: 503"})
    required, degraded, _ = classify_step_rows([row], segment="core")
    assert row["status"] == "failed" and not required and len(degraded) == 1


def test_an_unexpected_exception_is_contained(monkeypatch, key):
    row = step(monkeypatch, error=RuntimeError("database is locked"))
    required, degraded, _ = classify_step_rows([row], segment="projections")
    assert row["status"] == "failed" and not required and len(degraded) == 1
    assert "database is locked" in row["message"]


# ------------------------------------------------------------------ where it sits in the segments

@pytest.fixture
def ordered(monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "_run_cfbd_split", lambda *a, **k: calls.append("cfbd") or {"step": "cfbd", "status": "success"})
    monkeypatch.setattr(tr, "_run_low_memory_phase", lambda *a, **k: calls.append("phase") or [])
    monkeypatch.setattr(tr, "_refresh_odds_lines",
                        lambda *a, **k: calls.append("odds") or {"step": "odds-lines", "status": "success"})
    monkeypatch.setattr(tr, "_refresh_two_engine_manifest",
                        lambda *a, **k: calls.append("manifest") or {"step": "two-engine-manifest", "status": "success"})
    return calls


@pytest.mark.parametrize("segment", ["core", "projections"])
def test_the_sweep_comes_right_before_the_picks_freeze(ordered, segment):
    results = tr._segment_results(segment, 2026, root=Path("."), log=None, heartbeat=lambda: None)
    assert ordered.index("odds") + 1 == ordered.index("manifest")
    assert [r["step"] for r in results][-2:] == ["odds-lines", "two-engine-manifest"]


@pytest.mark.parametrize("segment", ["rosters", "content", "analytics"])
def test_other_segments_do_not_sweep(ordered, segment):
    tr._segment_results(segment, 2026, root=Path("."), log=None, heartbeat=lambda: None)
    assert "odds" not in ordered


# ------------------------------------------------------------------ the test run itself is sealed off

def test_the_suite_never_sees_a_live_key_or_the_real_ledger():
    assert os.environ.get("ODDS_API_KEY") == ""
    assert "instance" not in tracker.ledger_path().parts
    assert tracker.auto("cfb") == {"status": "skipped", "message": "ODDS_API_KEY is not set"}


def test_a_whole_core_segment_run_unpatched_spends_nothing(monkeypatch):
    monkeypatch.setattr(tr, "_run_cfbd_split", lambda *a, **k: {"step": "cfbd", "status": "success"})
    monkeypatch.setattr(tr, "_run_low_memory_phase", lambda *a, **k: [])
    monkeypatch.setattr(tr, "_refresh_two_engine_manifest", lambda *a, **k: {"step": "two-engine-manifest", "status": "success"})
    monkeypatch.setattr(tracker.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    results = tr._segment_results("core", 2026, root=Path("."), log=None, heartbeat=lambda: None)
    odds = next(r for r in results if r["step"] == "odds-lines")
    assert odds["status"] == "skipped"


# ------------------------------------------------------------------ the NFL lane

class NflFixture:
    def __init__(self, monkeypatch, *, pending, freeze_ok=True, key=True, auto_error=None):
        self.calls = []
        monkeypatch.setenv("ODDS_API_KEY", "k" if key else "")
        monkeypatch.setattr(tracker, "auto", lambda league, **k: (_ for _ in ()).throw(auto_error) if auto_error
                            else self.calls.append(("auto", league)) or {"status": "ok"})
        monkeypatch.setattr(tracker, "pending_freeze", lambda league, **k: (pending, "HIGH" if pending else None))
        monkeypatch.setattr(tracker, "mark_frozen", lambda league, high: self.calls.append(("marked", league, high)))
        monkeypatch.setattr(refresh_cli, "_freeze_forecasts",
                            lambda repository, season: self.calls.append(("froze", season)) or freeze_ok)
        monkeypatch.setattr(refresh_cli, "_nfl_repository", lambda: "repo")


def test_the_nfl_lane_sweeps_then_refreezes_when_newer_lines_are_on_file(monkeypatch):
    fixture = NflFixture(monkeypatch, pending=["g1"])
    refresh_cli._sweep_odds_and_freeze(2026)
    assert fixture.calls == [("auto", "nfl"), ("froze", 2026), ("marked", "nfl", "HIGH")]


def test_a_quiet_tick_does_not_refreeze(monkeypatch):
    fixture = NflFixture(monkeypatch, pending=[])
    refresh_cli._sweep_odds_and_freeze(2026)
    assert fixture.calls == [("auto", "nfl")]


def test_a_failed_freeze_is_not_marked_so_it_is_tried_again(monkeypatch):
    fixture = NflFixture(monkeypatch, pending=["g1"], freeze_ok=False)
    refresh_cli._sweep_odds_and_freeze(2026)
    assert ("marked", "nfl", "HIGH") not in fixture.calls


def test_without_a_key_the_nfl_lane_touches_nothing(monkeypatch):
    # odds are not being tracked here: no sweep, no read of the lines table, and above all no re-freeze of real picks
    fixture = NflFixture(monkeypatch, pending=["g1"], key=False)
    refresh_cli._sweep_odds_and_freeze(2026)
    assert fixture.calls == []


def test_a_tracker_failure_never_fails_the_availability_refresh(monkeypatch, capsys):
    NflFixture(monkeypatch, pending=["g1"], auto_error=RuntimeError("network down"))
    refresh_cli._sweep_odds_and_freeze(2026)                   # must not raise
    assert "odds_lines: skipped" in capsys.readouterr().out


def test_the_availability_segment_runs_the_sweep_after_the_injury_sync(monkeypatch):
    order = []
    monkeypatch.setattr(refresh_cli, "_sync_availability", lambda season: order.append("availability"))
    monkeypatch.setattr(refresh_cli, "_sweep_odds_and_freeze", lambda season: order.append("odds"))
    monkeypatch.setattr(refresh_cli, "_append_history", lambda record: None)

    class Args:
        segment, season = "availability", 2026

    from datetime import datetime, timezone
    try:
        refresh_cli._run_segment(Args(), {"status": "running"}, datetime.now(timezone.utc))
    except Exception:
        pass                                                   # the history bookkeeping after the work is not under test
    assert order == ["availability", "odds"]
