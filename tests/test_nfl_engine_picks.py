from contextlib import closing
from datetime import date
import os
from pathlib import Path
import tempfile

from sports_aggregator.nfl import engine_picks
from sports_aggregator.nfl.data_health import season_coverage
from sports_aggregator.nfl.models import Game
from sports_aggregator.nfl.nflverse import NflverseClient
from sports_aggregator.nfl.repository import NFLRepository


def _projection():
    return {
        "version": "test", "season": 2026, "week": 4,
        "games": [{
            "game_id": "2026_04_A_B", "season": 2026, "week": 4,
            "game_date": "2026-10-04", "away_team": "A", "home_team": "B",
            "completed": False, "thin_sample": False,
            "market_anchor": {"margin": 3.0, "total": 44.0,
                              "away_points": 20.5, "home_points": 23.5},
            "football_lab": {"margin": -1.0, "total": 50.0,
                             "away_points": 25.5, "home_points": 24.5},
            "disagreement": {"margin": -4.0, "total": 6.0},
            "historical_uncertainty_scale": {
                "margin_residual_scale": 8.0, "total_residual_scale": 8.0,
            },
        }],
    }


def test_picks_orient_home_margin_to_selected_team_line(monkeypatch):
    monkeypatch.setattr(engine_picks, "live_projection_report", lambda *args, **kwargs: _projection())
    dashboard = engine_picks.build_dashboard(object(), 2026, 4)
    game = dashboard["games"][0]

    assert game["straight_up_pick"] == "A"
    assert game["ats_pick"] == "A +3"
    assert game["total_pick"] == "Over 44"
    assert game["ats_separation"] == "medium"
    assert game["total_separation"] == "high"
    assert dashboard["label_policy"]["separation_is_confidence"] is False


def test_live_schedule_expires_before_larger_stat_assets(tmp_path):
    schedule = tmp_path / "schedules.parquet"
    weekly = tmp_path / "weekly_2026.parquet"
    schedule.write_bytes(b"cached")
    weekly.write_bytes(b"cached")
    os.utime(schedule, (9_000, 9_000))
    os.utime(weekly, (9_000, 9_000))
    client = NflverseClient(tmp_path, clock=lambda: 10_000, today=date(2026, 9, 28))

    assert client._is_fresh(schedule, "schedules", None) is False
    assert client._is_fresh(weekly, "weekly", 2026) is True


def test_completed_game_coverage_exposes_missing_logs():
    with tempfile.TemporaryDirectory() as directory:
        repository = NFLRepository(Path(directory) / "nfl.sqlite3")
        repository.initialize()
        repository.replace_games(2026, [Game(
            game_id="2026_01_A_B", season=2026, season_type="REG", week=1,
            game_date="2026-09-10", game_time="20:00", away_team="A", home_team="B",
            away_score=20, home_score=24, overtime=False,
            division_game=False, stadium=None, roof=None, surface=None,
            temperature=None, wind=None, spread_line=None, total_line=None,
        )])

        empty = season_coverage(repository, 2026)
        assert empty["missing_stats"] == ["2026_01_A_B"]
        assert empty["missing_pbp"] == ["2026_01_A_B"]

        with closing(repository._connect()) as connection:
            connection.execute(
                "INSERT INTO player_weekly_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (2026, 1, "REG", "2026_01_A_B", "p1", "Player", "A", "B", "QB",
                 "passing_yards", 200.0),
            )
            connection.execute(
                "INSERT INTO game_team_efficiency VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (2026, 1, "2026_01_A_B", "A", "B", 1, 0.1, 1, 1, 0.1, 0, 0.0, 1, 0.1, 0),
            )
            connection.commit()

        covered = season_coverage(repository, 2026)
        assert covered["stat_game_logs"] == 1
        assert covered["pbp_game_logs"] == 1
        assert covered["postgame_ready"] == 1
        assert covered["healthy"] is True


def test_schedule_refresh_keeps_only_changed_market_snapshots(tmp_path):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()

    def game(spread, total=44.0, completed=False):
        return Game(
            game_id="2026_04_A_B", season=2026, season_type="REG", week=4,
            game_date="2026-10-04", game_time="13:00", away_team="A", home_team="B",
            away_score=20 if completed else None, home_score=24 if completed else None,
            overtime=False, division_game=False,
            stadium=None, roof=None, surface=None, temperature=None, wind=None,
            spread_line=spread, total_line=total,
        )

    repository.replace_games(2026, [game(2.5)])
    repository.replace_games(2026, [game(2.5)])
    repository.replace_games(2026, [game(3.0)])
    repository.replace_games(2026, [game(3.0, completed=True)])

    history = repository.market_line_history("2026_04_A_B")
    assert [row["spread_line"] for row in history] == [2.5, 3.0, 3.0]
    assert all(row["source"] == "nflverse" for row in history)
    movement = repository.market_line_movement("2026_04_A_B")
    assert movement["spread_change"] == .5
    assert movement["total_change"] == 0.0
    assert movement["is_closing_snapshot"] is True
