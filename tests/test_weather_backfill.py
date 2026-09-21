"""Tests for the historical weather backfill's own logic -- date batching
and skip-already-backfilled -- not the reused forecast-parsing code, which
sports_aggregator.providers.weather already owns and is tested elsewhere."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.weather_backfill import backfill


def _hourly_payload(date: str, temperature: float = 70.0) -> dict:
    hour = f"{date}T18:00"
    return {
        "hourly": {
            "time": [hour], "temperature_2m": [temperature],
            "precipitation_probability": [None], "precipitation": [0.0],
            "wind_speed_10m": [5.0], "wind_gusts_10m": [8.0],
            "relative_humidity_2m": [50.0], "visibility": [None], "weather_code": [1],
        }
    }


class WeatherBackfillTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.addCleanup(lambda: self._safe_remove())
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        connection = sqlite3.connect(self.path)
        connection.execute(
            """INSERT INTO venues VALUES(1,'Stadium A',NULL,NULL,40.0,-80.0,NULL,NULL,0,NULL,NULL,'now')""")
        connection.execute(
            """INSERT INTO venues VALUES(2,'Stadium B',NULL,NULL,41.0,-81.0,NULL,NULL,0,NULL,NULL,'now')""")
        connection.execute(
            """INSERT INTO venues VALUES(3,'Dome Stadium',NULL,NULL,42.0,-82.0,NULL,NULL,1,NULL,NULL,'now')""")
        for game_id, venue_id, start_date, home in (
            (1, 1, "2022-09-03T18:00:00+00:00", "Alpha"),
            (2, 2, "2022-09-03T18:00:00+00:00", "Gamma"),
            (3, 1, "2022-09-10T18:00:00+00:00", "Alpha"),
            (4, 3, "2022-09-10T18:00:00+00:00", "Delta"),  # dome, excluded
        ):
            connection.execute(
                """INSERT INTO games(game_id,season,week,season_type,start_date,start_time_tbd,
                   completed,neutral_site,conference_game,venue_id,venue,television,
                   home_team_id,home_team,home_conference,home_points,home_pregame_elo,
                   away_team_id,away_team,away_conference,away_points,away_pregame_elo,
                   excitement_index,notes,updated_at)
                   VALUES(?,2022,1,'regular',?,0,1,0,0,?,'V','TV',
                          1,?,'C',24,1500,2,'Away','C',17,1500,NULL,NULL,'now')""",
                (game_id, start_date, venue_id, home),
            )
        connection.commit()
        connection.close()

    def _safe_remove(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_dome_games_are_excluded(self):
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.raise_for_status = lambda: None
        session.get.return_value.json.return_value = [
            _hourly_payload("2022-09-03"), _hourly_payload("2022-09-03"),
        ]
        result = backfill(self.repository, start_season=2022, end_season=2022, session=session)
        self.assertEqual(result["games_considered"], 3)  # not the dome game

    def test_games_on_the_same_date_share_one_call(self):
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.raise_for_status = lambda: None
        session.get.return_value.json.side_effect = [
            [_hourly_payload("2022-09-03"), _hourly_payload("2022-09-03")],  # 2 games, 1 call
            [_hourly_payload("2022-09-10")],  # 1 outdoor game that date
        ]
        result = backfill(self.repository, start_season=2022, end_season=2022, session=session)
        self.assertEqual(session.get.call_count, 2)  # 2 distinct dates, not 3 games
        self.assertEqual(result["stored"], 3)

    def test_rerun_without_force_skips_already_stored_games(self):
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.raise_for_status = lambda: None
        session.get.return_value.json.side_effect = [
            [_hourly_payload("2022-09-03"), _hourly_payload("2022-09-03")],
            [_hourly_payload("2022-09-10")],
        ]
        backfill(self.repository, start_season=2022, end_season=2022, session=session)

        session2 = MagicMock()
        result = backfill(self.repository, start_season=2022, end_season=2022, session=session2)
        session2.get.assert_not_called()
        self.assertEqual(result["games_considered"], 0)

    def test_stored_rows_are_tagged_with_the_archive_source(self):
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.raise_for_status = lambda: None
        session.get.return_value.json.side_effect = [
            [_hourly_payload("2022-09-03"), _hourly_payload("2022-09-03")],
            [_hourly_payload("2022-09-10")],
        ]
        backfill(self.repository, start_season=2022, end_season=2022, session=session)
        with sqlite3.connect(self.path) as connection:
            sources = connection.execute("SELECT DISTINCT source FROM game_weather").fetchall()
        self.assertEqual(sources, [("open-meteo-archive",)])


if __name__ == "__main__":
    unittest.main()
