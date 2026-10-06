"""One process syncs every team's roster; a failing team neither stops the rest nor goes unretried."""
from __future__ import annotations

import sqlite3

import pytest

from sports_aggregator import scheduled_refresh
from sports_aggregator.cfb import dataset_cli
from sports_aggregator.cfb.cfbd import CFBDRequestError
from sports_aggregator.cfb.models import Team
from sports_aggregator.cfb.repository import CFBRepository

TEAMS = ("Alabama", "Boise State", "Clemson")


def _player(i, team):
    return {"id": f"{team}-{i}", "firstName": "A", "lastName": f"P{i}", "team": team, "position": "QB",
            "jersey": i, "height": 74, "weight": 200, "year": 2}


class FakeClient:
    """Stands in for CFBDClient: `plan[team]` is a list of outcomes consumed per call."""
    configured = True

    def __init__(self, plan, *args, **kwargs):
        self.plan, self.calls = plan, []

    def get(self, path, params, *, cache_ttl_seconds, force):
        team = params["team"]
        self.calls.append(team)
        outcome = self.plan[team].pop(0) if self.plan[team] else "ok"
        if outcome == "429":
            raise CFBDRequestError("CFBD /roster returned HTTP 429")
        return [_player(1, team), _player(2, team)]


@pytest.fixture()
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "cfb.sqlite3")
    repository = CFBRepository(path)
    repository.replace_teams(tuple(
        Team(i + 1, name, "Mascot", name[:3].upper(), "SEC", None, "fbs", None, None, (), (name,), None, None)
        for i, name in enumerate(TEAMS)))
    monkeypatch.setenv("CFB_DATABASE_PATH", path)
    monkeypatch.setenv("CFBD_RAW_CACHE_PATH", str(tmp_path / "raw"))
    return path


def _install(monkeypatch, plan):
    client = FakeClient(plan)
    monkeypatch.setattr(dataset_cli, "CFBDClient", lambda *a, **k: client)
    return client


def _stored(path):
    with sqlite3.connect(path) as connection:
        return dict(connection.execute("SELECT team, COUNT(*) FROM players GROUP BY team"))


def test_every_team_is_synced_in_one_pass(database, monkeypatch):
    client = _install(monkeypatch, {t: [] for t in TEAMS})
    lines = []
    stored, failed = dataset_cli.sync_all_team_rosters(2026, emit=lines.append, sleep=lambda s: None)
    assert failed == [] and stored == 6
    assert client.calls == list(TEAMS)
    assert _stored(database) == {t: 2 for t in TEAMS}
    assert lines[0].startswith("players team=Alabama: success (2) in ")


def test_a_rate_limited_team_is_retried_once_after_the_cooldown(database, monkeypatch):
    client = _install(monkeypatch, {"Alabama": [], "Boise State": ["429"], "Clemson": []})
    naps, lines = [], []
    stored, failed = dataset_cli.sync_all_team_rosters(2026, retry_after=45, emit=lines.append, sleep=naps.append)
    assert failed == [] and naps == [45]
    assert client.calls == ["Alabama", "Boise State", "Clemson", "Boise State"]
    assert _stored(database) == {t: 2 for t in TEAMS}
    assert any("1 of 3 teams failed; waiting 45s" in line for line in lines)


def test_a_team_that_keeps_failing_is_reported_and_the_others_still_stored(database, monkeypatch):
    _install(monkeypatch, {"Alabama": [], "Boise State": ["429", "429"], "Clemson": []})
    stored, failed = dataset_cli.sync_all_team_rosters(2026, emit=lambda l: None, sleep=lambda s: None)
    assert failed == ["Boise State"] and stored == 4
    assert _stored(database) == {"Alabama": 2, "Clemson": 2}


def test_the_command_exits_nonzero_and_names_the_failed_teams(database, monkeypatch, capsys):
    _install(monkeypatch, {"Alabama": [], "Boise State": ["429", "429"], "Clemson": []})
    monkeypatch.setattr(dataset_cli.time, "sleep", lambda s: None)
    code = dataset_cli.main(["players", "--year", "2026", "--all-teams"])
    out = capsys.readouterr().out
    assert code == 1 and "players: failed teams: Boise State" in out


def test_the_command_exits_zero_when_every_team_succeeds(database, monkeypatch, capsys):
    _install(monkeypatch, {t: [] for t in TEAMS})
    assert dataset_cli.main(["players", "--year", "2026", "--all-teams"]) == 0
    assert "players: success (6)" in capsys.readouterr().out


def test_all_teams_is_only_for_players(database):
    with pytest.raises(SystemExit):
        dataset_cli.main(["games", "--year", "2026", "--all-teams"])


def test_the_single_team_path_still_works(database, monkeypatch, capsys):
    _install(monkeypatch, {t: [] for t in TEAMS})
    assert dataset_cli.main(["players", "--year", "2026", "--team", "Clemson"]) == 0
    assert _stored(database) == {"Clemson": 2}


# -- the scheduler launches one command, not one per team ----------------------------------------
def test_the_scheduler_syncs_rosters_with_a_single_command(monkeypatch, tmp_path):
    commands = []

    def fake(command, *, timeout, log, memory_mb=None):
        commands.append(command)
        return "success", "players: success (15000) in 20.0s", 20.0

    monkeypatch.setattr(scheduled_refresh, "_run_command", fake)
    with (tmp_path / "log.txt").open("w") as log:
        result = scheduled_refresh._run_cfbd_split(2026, root=tmp_path, timeout=60, log=log, datasets=["players"])
    assert result["status"] == "success"
    assert commands == [["sports_aggregator.cfb.dataset_cli", "players", "--year", "2026", "--all-teams"]]


def test_a_failed_roster_step_fails_the_sync_and_names_the_cause(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduled_refresh, "_run_command", lambda *a, **k: (
        "failed", "players: failed teams: Boise State (13800 players stored) in 300.0s", 300.0))
    with (tmp_path / "log.txt").open("w") as log:
        result = scheduled_refresh._run_cfbd_split(2026, root=tmp_path, timeout=60, log=log, datasets=["players"])
    assert result["status"] == "failed" and "players" in result["message"]
