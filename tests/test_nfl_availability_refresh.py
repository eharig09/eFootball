from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sports_aggregator.nfl import refresh_cli


NOW = datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)  # noon Eastern


def _game(*, day="2026-09-27", time="13:00", completed=0):
    return {
        "game_id": "2026_03_CAR_CLE", "game_date": day, "game_time": time,
        "away_team": "CAR", "home_team": "CLE", "completed": completed,
    }


def test_final_four_hours_use_fifteen_minute_injury_cadence():
    plan = refresh_cli.availability_refresh_plan(
        [_game()], cache_modified_at=NOW.timestamp() - 16 * 60, now=NOW)

    assert plan["due"] is True
    assert plan["interval_minutes"] == 15
    assert plan["hours_to_kickoff"] == 1.0


def test_recent_snapshot_is_not_fetched_again_inside_cadence():
    plan = refresh_cli.availability_refresh_plan(
        [_game()], cache_modified_at=NOW.timestamp() - 10 * 60, now=NOW)

    assert plan["due"] is False
    assert plan["interval_minutes"] == 15


def test_games_inside_two_days_use_hourly_cadence():
    plan = refresh_cli.availability_refresh_plan(
        [_game(day="2026-09-28", time="13:00")],
        cache_modified_at=NOW.timestamp() - 61 * 60, now=NOW)

    assert plan["due"] is True
    assert plan["interval_minutes"] == 60


def test_no_extra_fetch_outside_the_game_window():
    plan = refresh_cli.availability_refresh_plan(
        [_game(day="2026-10-01")], cache_modified_at=None, now=NOW)

    assert plan == {"due": False, "reason": "no game within 48 hours"}


def test_availability_segment_dispatches_without_heavy_core_work():
    calls = []
    with patch.object(refresh_cli, "_sync_availability",
                      lambda season: calls.append(season)):
        assert refresh_cli.main(["availability", "--season", "2026"]) == 0
    assert calls == [2026]


def test_render_schedule_checks_availability_every_fifteen_minutes():
    render = Path("render.yaml").read_text(encoding="utf-8")
    assert "name: nfl-availability-refresh-trigger" in render
    assert 'schedule: "*/15 * * * *"' in render
    assert "value: availability" in render
