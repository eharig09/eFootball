"""The scoreboard shows the engine's projected score beside the market line and the result."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sports_aggregator.cfb import scoreboard_projection as sp
from sports_aggregator.cfb import views
from sports_aggregator.cfb.models import Game, Team
from sports_aggregator.cfb.repository import CFBRepository

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def _game(game_id, kickoff, *, completed=False, points=None, home="Michigan", away="Ohio State"):
    return Game.from_cfbd({
        "id": game_id, "season": 2026, "week": 6, "seasonType": "regular",
        "startDate": kickoff.isoformat().replace("+00:00", "Z"), "startTimeTBD": False,
        "completed": completed, "neutralSite": False, "conferenceGame": True, "venue": "Stadium",
        "venueId": 1, "homeId": 1, "homeTeam": home, "homeConference": "Big Ten",
        "homePoints": points[0] if points else None, "awayId": 2, "awayTeam": away,
        "awayConference": "Big Ten", "awayPoints": points[1] if points else None})


@pytest.fixture()
def repo(tmp_path):
    repository = CFBRepository(tmp_path / "cfb.sqlite3")
    repository.replace_teams((
        Team(1, "Michigan", "Wolverines", "MICH", "Big Ten", None, "fbs", None, None, (), ("Michigan",), None, None),
        Team(2, "Ohio State", "Buckeyes", "OSU", "Big Ten", None, "fbs", None, None, (), ("Ohio State",), None, None)))
    return repository


class Recorder:
    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result or {"away": 20.2, "home": 27.4, "total": 47.6, "margin": 7.2}, error

    def __call__(self, repository, game):
        self.calls.append(int(game["game_id"]))
        if self.error and int(game["game_id"]) in self.error:
            raise RuntimeError("no data")
        return self.result


def _rows(repo):
    with sqlite3.connect(repo.path) as connection:
        return {r[0]: r for r in connection.execute(
            "SELECT game_id,away_points,home_points,total,margin,frozen,computed_at FROM cfb_scoreboard_projections")}


def _store(repo, *games):
    repo.replace_games(2026, list(games))


def test_an_upcoming_game_is_projected_and_stored(repo):
    _store(repo, _game(1, NOW + timedelta(days=2)))
    compute = Recorder()
    report = sp.refresh(repo, season=2026, now=NOW, compute=compute)
    assert report["computed"] == 1 and compute.calls == [1]
    row = _rows(repo)[1]
    assert (row[1], row[2], row[5]) == (20.2, 27.4, 0)


def test_a_fresh_projection_is_not_recomputed_until_it_is_stale(repo):
    _store(repo, _game(1, NOW + timedelta(days=2)))
    compute = Recorder()
    sp.refresh(repo, season=2026, now=NOW, compute=compute)
    report = sp.refresh(repo, season=2026, now=NOW + timedelta(hours=6), compute=compute)
    assert report["fresh"] == 1 and compute.calls == [1]
    report = sp.refresh(repo, season=2026, now=NOW + timedelta(hours=13), compute=compute)
    assert report["computed"] == 1 and compute.calls == [1, 1]


def test_once_a_game_has_kicked_off_its_pregame_projection_is_frozen_not_rewritten(repo):
    _store(repo, _game(1, NOW + timedelta(hours=3)))
    compute = Recorder({"away": 20.0, "home": 27.0, "total": 47.0, "margin": 7.0})
    sp.refresh(repo, season=2026, now=NOW, compute=compute)
    later = NOW + timedelta(hours=5)                       # the game has started
    compute.result = {"away": 99.0, "home": 1.0, "total": 100.0, "margin": -98.0}   # hindsight must not leak in
    report = sp.refresh(repo, season=2026, now=later, compute=compute)
    assert report["frozen"] == 1 and compute.calls == [1]
    row = _rows(repo)[1]
    assert row[5] == 1 and (row[1], row[2]) == (20.0, 27.0)
    assert sp.refresh(repo, season=2026, now=later + timedelta(days=1), compute=compute)["computed"] == 0


def test_a_finished_game_with_no_stored_projection_gets_one_as_of_kickoff_and_is_frozen(repo):
    _store(repo, _game(1, NOW - timedelta(days=2), completed=True, points=(24, 17)))
    compute = Recorder()
    report = sp.refresh(repo, season=2026, now=NOW, compute=compute)
    assert report["computed"] == 1 and _rows(repo)[1][5] == 1


def test_games_outside_the_window_are_left_alone(repo):
    _store(repo, _game(1, NOW + timedelta(days=30)), _game(2, NOW - timedelta(days=60), completed=True, points=(7, 3)))
    compute = Recorder()
    assert sp.refresh(repo, season=2026, now=NOW, compute=compute)["considered"] == 0 and compute.calls == []


def test_one_game_failing_does_not_stop_the_others(repo):
    _store(repo, _game(1, NOW + timedelta(days=1)), _game(2, NOW + timedelta(days=2)))
    compute = Recorder(error={1})
    report = sp.refresh(repo, season=2026, now=NOW, compute=compute)
    assert report["computed"] == 1 and report["failures"][0]["game_id"] == 1
    assert set(_rows(repo)) == {2}


def test_a_game_the_engine_cannot_project_is_counted_not_stored(repo):
    _store(repo, _game(1, NOW + timedelta(days=1)))
    report = sp.refresh(repo, season=2026, now=NOW, compute=lambda repository, game: None)
    assert report["unavailable"] == 1 and _rows(repo) == {}


def test_reading_projections_never_creates_the_table(repo):
    assert sp.projections_for_games(repo, [1, 2]) == {}
    with sqlite3.connect(repo.path) as connection:
        names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master")]
    assert "cfb_scoreboard_projections" not in names


def test_stored_projections_are_returned_by_game(repo):
    _store(repo, _game(1, NOW + timedelta(days=1)))
    sp.refresh(repo, season=2026, now=NOW, compute=Recorder())
    got = sp.projections_for_games(repo, [1, 99])
    assert set(got) == {1} and got[1]["home"] == 27.4 and got[1]["frozen"] is False


# ---------------------------------------------------------------------------- the card
def _card(game, projection, line=None):
    games = [{"game_id": 1, "start_date": "2026-10-10T16:00:00Z", "away_team": "Ohio State",
              "home_team": "Michigan", "away_team_id": 2, "home_team_id": 1, "away_conference": "Big Ten",
              "home_conference": "Big Ten", "completed": game.get("completed", 0),
              "away_points": game.get("away"), "home_points": game.get("home")}]
    return views.scoreboard_games(games, {}, {}, timezone_name="America/New_York",
                                  lines={1: line} if line else None,
                                  projections={1: projection} if projection else None)[0]


PROJECTION = {"away": 20.2, "home": 27.4, "total": 47.6, "margin": 7.2, "frozen": False}
LINE = {"spread": -6.5, "total": 48.5, "books": 3}


def test_an_upcoming_card_carries_the_projection_and_the_line():
    card = _card({}, PROJECTION, LINE)
    assert (card["away"]["projected"], card["home"]["projected"]) == (20, 27)
    assert card["home"]["market"] == "-6.5" and card["away"]["market"] == "48.5"
    assert card["projection"]["total"] == 47.6 and "Engine projection" in card["projection"]["title"]
    assert card["away"]["points"] is None


def test_a_finished_card_keeps_the_result_the_line_and_the_pregame_projection():
    card = _card({"completed": 1, "away": 14, "home": 31}, {**PROJECTION, "frozen": True}, LINE)
    assert card["home"]["points"] == 31 and card["home"]["projected"] == 27
    assert card["home"]["market"] == "-6.5"                 # the line no longer disappears at the final
    assert "(pre-game)" in card["projection"]["title"] and card["winner"] == "home"


def test_a_game_without_a_projection_renders_as_before():
    card = _card({}, None, LINE)
    assert card["projection"] is None and "projected" not in card["home"]
