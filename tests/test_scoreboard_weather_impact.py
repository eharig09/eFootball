import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.weather import flags_for_games


class NFLWeatherFlagTests(unittest.TestCase):
    def test_latest_flags_only_and_never_for_indoor_games(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = NFLRepository(Path(folder) / "nfl.sqlite3")
            repository.initialize()
            wind = json.dumps([{"flag": "HIGH_WIND", "detail": "20 mph sustained wind"}])
            with closing(sqlite3.connect(repository.path)) as connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(nfl_game_weather)")]
                def put(game_id, generated, flags, indoor):
                    values = {"game_id": game_id, "forecast_generated_at": generated,
                              "flags_json": flags, "indoor": indoor}
                    row = {name: values.get(name) for name in columns}
                    for name in columns:
                        if row[name] is None and name not in values:
                            row[name] = 0 if name != "weather_code" else None
                    connection.execute(
                        f"INSERT INTO nfl_game_weather ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                        [row[name] for name in columns])
                put("g1", "2026-10-01T00:00:00+00:00", wind, 0)
                put("g1", "2026-10-05T00:00:00+00:00", "[]", 0)      # newest forecast is clear
                put("g2", "2026-10-05T00:00:00+00:00", wind, 0)
                put("g3", "2026-10-05T00:00:00+00:00", wind, 1)      # indoor
                connection.commit()
            flags = flags_for_games(repository, ["g1", "g2", "g3", "g4"])
            self.assertEqual(list(flags), ["g2"])
            self.assertEqual(flags["g2"][0]["flag"], "HIGH_WIND")
            self.assertEqual(flags_for_games(repository, []), {})


if __name__ == "__main__":
    unittest.main()
