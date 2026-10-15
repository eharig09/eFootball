"""The odds-line tracker's scheduling lane: the web endpoint, the cron trigger, and the status card."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import io
import json
import sqlite3
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from sports_aggregator import odds_api_tracker as tracker
from sports_aggregator import odds_render_trigger as trigger


def _saturday(hour, minute=0):
    # 2026-10-17 is a Saturday
    return datetime(2026, 10, 17, hour, minute, tzinfo=tracker.EASTERN).astimezone(timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CFB_DATABASE_PATH", str(tmp_path / "cfb.sqlite3"))
    monkeypatch.setenv("NFL_DATABASE_PATH", str(tmp_path / "nfl.sqlite3"))
    monkeypatch.setenv("ODDS_API_LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    return tmp_path


def _spend(when, league="cfb", cost=2, used=None, remaining=None):
    with closing(sqlite3.connect(tracker.ledger_path())) as ledger:
        ledger.executescript(tracker.LEDGER_SCHEMA)
        ledger.execute("INSERT INTO odds_api_calls (called_at,league,cost,used,remaining,status) VALUES (?,?,?,?,?,'ok')",
                       (tracker._iso(when), league, cost, used, remaining))
        ledger.commit()


# ------------------------------------------------------------------ due_leagues

def test_due_leagues_is_empty_without_a_key(env, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "")
    assert tracker.due_leagues(now=_saturday(11, 10)) == []


def test_due_leagues_names_the_league_whose_slot_is_due(env):
    assert tracker.due_leagues(now=_saturday(11, 10)) == ["cfb"]


def test_due_leagues_is_empty_in_the_quiet_hours(env):
    assert tracker.due_leagues(now=_saturday(2, 0)) == []


def test_due_leagues_forgets_a_slot_once_it_has_been_swept(env):
    _spend(_saturday(11, 12))
    assert tracker.due_leagues(now=_saturday(11, 40)) == []


def test_due_leagues_is_empty_when_the_budget_is_spent(env):
    _spend(_saturday(9, 0) - timedelta(days=1), cost=tracker.MONTHLY_CAP)
    assert tracker.due_leagues(now=_saturday(11, 10)) == []


def test_due_leagues_makes_no_request(env):
    with patch.object(tracker.requests, "get", side_effect=AssertionError("no network allowed")):
        tracker.due_leagues(now=_saturday(11, 10))


# ------------------------------------------------------------------ the web endpoint

def _app(tmp_path):
    from app import create_app
    return create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False, "CFB_REFRESH_TOKEN": "t",
                       "CFB_DEFAULT_SEASON": 2026, "CFB_DATABASE_PATH": str(tmp_path / "cfb.sqlite3"),
                       "NFL_DATABASE_PATH": str(tmp_path / "nfl.sqlite3")})


def _post(app, **headers):
    return app.test_client().post("/internal/odds-lines-refresh", headers=headers or {"Authorization": "Bearer t"})


def test_the_endpoint_requires_the_refresh_token(env):
    assert _post(_app(env), Authorization="Bearer wrong").status_code == 401


def test_the_endpoint_skips_without_a_key_and_starts_nothing(env, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "")
    with patch("app.subprocess.Popen") as popen:
        response = _post(_app(env))
    assert (response.status_code, response.get_json()["reason"]) == (200, "odds_api_key_not_set")
    popen.assert_not_called()


def test_the_endpoint_skips_when_no_slot_is_due_and_starts_nothing(env):
    with patch("app.subprocess.Popen") as popen, patch.object(tracker, "due_leagues", return_value=[]):
        response = _post(_app(env))
    assert (response.status_code, response.get_json()["reason"]) == (200, "no_slot_due")
    popen.assert_not_called()


def test_the_endpoint_starts_the_tracker_when_a_slot_is_due(env):
    with patch("app.subprocess.Popen") as popen, patch.object(tracker, "due_leagues", return_value=["cfb"]):
        response = _post(_app(env))
    assert response.status_code == 202
    assert response.get_json() == {"status": "accepted", "leagues": ["cfb"]}
    assert popen.call_args.args[0][1:] == ["-m", "sports_aggregator.odds_api_tracker", "run-due"]


# ------------------------------------------------------------------ the cron trigger

class _Response:
    def __init__(self, status=200, payload=None):
        self.status, self.payload = status, payload or {"status": "skipped", "reason": "no_slot_due"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_the_trigger_derives_its_url_from_the_refresh_url(monkeypatch):
    monkeypatch.delenv("ODDS_LINES_REFRESH_URL", raising=False)
    monkeypatch.setenv("CFB_REFRESH_URL", "https://example.onrender.com/internal/cfb-refresh?profile=auto")
    assert trigger.endpoint_url() == "https://example.onrender.com/internal/odds-lines-refresh"


def test_the_trigger_posts_with_the_bearer_token(monkeypatch):
    monkeypatch.setenv("CFB_REFRESH_URL", "https://example.onrender.com/internal/cfb-refresh")
    monkeypatch.setenv("CFB_REFRESH_TOKEN", "secret")
    seen = {}

    def opener(request, timeout=None):
        seen["request"] = request
        return _Response()

    result = trigger.trigger(opener=opener, sleeper=lambda _s: None)
    assert result["status"] == "triggered"
    assert seen["request"].get_method() == "POST"
    assert seen["request"].get_header("Authorization") == "Bearer secret"
    assert "secret" not in json.dumps(result)


def test_the_trigger_retries_a_busy_service_then_succeeds(monkeypatch):
    monkeypatch.setenv("CFB_REFRESH_URL", "https://example.onrender.com/internal/cfb-refresh")
    monkeypatch.setenv("CFB_REFRESH_TOKEN", "secret")
    attempts = []

    def opener(request, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise HTTPError(request.full_url, 503, "busy", {}, io.BytesIO(b"restarting"))
        return _Response()

    assert trigger.trigger(opener=opener, sleeper=lambda _s: None)["attempt"] == 3


def test_the_trigger_does_not_retry_a_rejected_token(monkeypatch):
    monkeypatch.setenv("CFB_REFRESH_URL", "https://example.onrender.com/internal/cfb-refresh")
    monkeypatch.setenv("CFB_REFRESH_TOKEN", "secret")
    attempts = []

    def opener(request, timeout=None):
        attempts.append(1)
        raise HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b""))

    with pytest.raises(RuntimeError):
        trigger.trigger(opener=opener, sleeper=lambda _s: None)
    assert len(attempts) == 1


def test_the_trigger_needs_a_token(monkeypatch):
    monkeypatch.setenv("CFB_REFRESH_URL", "https://example.onrender.com/internal/cfb-refresh")
    monkeypatch.setenv("CFB_REFRESH_TOKEN", "")
    with pytest.raises(RuntimeError):
        trigger.trigger(opener=lambda *a, **k: _Response(), sleeper=lambda _s: None)


# ------------------------------------------------------------------ the status card

def test_the_status_card_says_why_when_no_key_is_set(env, monkeypatch):
    from sports_aggregator.cfb import data_status
    monkeypatch.setenv("ODDS_API_KEY", "")
    assert data_status._odds_tracker() == {"available": False, "reason": "ODDS_API_KEY is not set, so no lines are being tracked."}


def test_the_status_card_reports_credits_and_recent_sweeps(env):
    from sports_aggregator.cfb import data_status
    _spend(datetime.now(timezone.utc), used=12, remaining=488)
    card = data_status._odds_tracker()
    assert card["available"] is True
    assert (card["month_used"], card["monthly_cap"], card["api_remaining"]) == (2, tracker.MONTHLY_CAP, 488)
    assert card["recent_calls"][0]["status"] == "ok"
    assert card["recent_calls"][0]["relative"]
