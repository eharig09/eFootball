"""The NFL scoreboard shows the market line and the engine's projected score with the result."""
from __future__ import annotations

import sqlite3

import pytest

from sports_aggregator.nfl import scoreboard_projection as sp
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.views import matchup_cards


@pytest.fixture()
def repo(tmp_path):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    return repository


def _insert_forecast(repo, game_id, generated_at, away, home):
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            """INSERT INTO nfl_engine_forecasts(game_id,season,week,generated_at,model_version,away_team,
               home_team,away_points,home_points,model_margin,model_total) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (game_id, 2026, 4, generated_at, "v", "AAA", "BBB", away, home, home - away, home + away))


def _game(**over):
    base = {"game_id": "g1", "week": 4, "game_date": "2026-10-04", "game_time": "13:00", "completed": 0,
            "away_team": "AAA", "home_team": "BBB", "away_score": None, "home_score": None,
            "spread_line": None, "total_line": None}
    base.update(over)
    return base


def _cards(games, projections=None):
    ids = {"AAA": {"name": "Away", "logo_url": None, "color": "#111"}, "BBB": {"name": "Home", "logo_url": None, "color": "#222"}}
    return matchup_cards(games, ids, {}, {}, {}, projections=projections)


# --------------------------------------------------------------------------------- the line
def test_a_home_favourite_reads_as_the_home_team_minus_the_spread():
    card = _cards([_game(spread_line=3.5, total_line=44.5)], {})[0]
    assert card["line"] == {"spread": "BBB -3.5", "total": "O/U 44.5"}


def test_an_away_favourite_and_a_pickem():
    assert _cards([_game(spread_line=-7.0)], {})[0]["line"]["spread"] == "AAA -7"
    assert _cards([_game(spread_line=0.0, total_line=40)], {})[0]["line"]["spread"] == "PK"


def test_no_line_means_no_line_row():
    assert _cards([_game()], {})[0]["line"] is None


def test_the_line_stays_on_a_finished_game():
    card = _cards([_game(completed=1, away_score=17, home_score=24, spread_line=3.5)], {})[0]
    assert card["line"]["spread"] == "BBB -3.5" and card["sides"][1]["score"] == 24


# ------------------------------------------------------------------------------ the projection
PROJECTION = {"away": 20.4, "home": 27.2, "margin": 6.8, "total": 47.6, "frozen": False}


def test_each_side_carries_its_projected_points_and_the_card_a_summary():
    card = _cards([_game()], {"g1": PROJECTION})[0]
    assert [s["projected"] for s in card["sides"]] == [20, 27]
    assert card["projection"]["spread"] == "BBB -6.8" and card["projection"]["total"] == 47.6
    assert "Engine projection:" in card["projection"]["title"] and "(pre-game)" not in card["projection"]["title"]


def test_an_away_favoured_projection_names_the_away_team():
    card = _cards([_game()], {"g1": {**PROJECTION, "away": 28.0, "home": 20.0, "margin": -8.0}})[0]
    assert card["projection"]["spread"] == "AAA -8"


def test_a_frozen_projection_is_labelled_pregame_and_sits_beside_the_result():
    card = _cards([_game(completed=1, away_score=14, home_score=31)], {"g1": {**PROJECTION, "frozen": True}})[0]
    assert "(pre-game)" in card["projection"]["title"]
    assert card["sides"][1]["projected"] == 27 and card["sides"][1]["score"] == 31


def test_a_game_without_a_projection_still_gets_its_line():
    card = _cards([_game(spread_line=2.0)], {})[0]
    assert "projection" not in card and card["sides"][0].get("projected") is None and card["line"]


def test_without_projections_the_card_is_what_the_dashboard_always_got():
    card = _cards([_game(spread_line=2.0)])[0]
    assert "line" not in card and "projection" not in card


# ------------------------------------------------------------------------------------ the source
def test_finished_games_use_the_latest_frozen_forecast(repo):
    _insert_forecast(repo, "g1", "2026-10-03T10:00:00+00:00", 20.0, 24.0)
    _insert_forecast(repo, "g1", "2026-10-04T10:00:00+00:00", 21.0, 28.0)      # later issuance wins
    got = sp.week_projections(repo, 2026, 4, [_game(completed=1)])
    assert got["g1"]["home"] == 28.0 and got["g1"]["frozen"] is True


def test_a_finished_game_with_no_ledger_row_has_no_projection(repo):
    assert sp.week_projections(repo, 2026, 4, [_game(completed=1)]) == {}


def test_upcoming_games_use_the_live_forecast(repo, monkeypatch):
    monkeypatch.setattr(sp, "_upcoming", lambda repository, season, week: {"g1": PROJECTION, "other": PROJECTION})
    got = sp.week_projections(repo, 2026, 5, [_game()])
    assert set(got) == {"g1"} and got["g1"]["frozen"] is False


def test_a_forecast_that_cannot_be_built_does_not_take_the_scoreboard_down(repo, monkeypatch):
    def boom(repository, season, week):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(sp, "_upcoming", boom)
    assert sp.week_projections(repo, 2026, 5, [_game()]) == {}


def test_the_ledger_forecast_is_never_used_for_a_game_still_to_be_played(repo, monkeypatch):
    _insert_forecast(repo, "g1", "2026-10-03T10:00:00+00:00", 20.0, 24.0)
    monkeypatch.setattr(sp, "_upcoming", lambda repository, season, week: {})
    assert sp.week_projections(repo, 2026, 4, [_game()]) == {}
