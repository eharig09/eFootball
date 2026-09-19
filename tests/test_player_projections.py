"""Leakage and conservation tests for team-to-player projection allocation."""
from __future__ import annotations

from contextlib import closing
import os
import sqlite3
import tempfile
import unittest

from sports_aggregator.cfb.player_projections import project_team_players
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


class PlayerProjectionFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle); os.unlink(self.path)
        forget_initialized_schemas(); self.repository = CFBRepository(self.path)
        self.repository.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany("""INSERT INTO players VALUES(
              2026,?,?,?,?,?,?,?,?,?,?)""", (
                ("qb1", "First", "Quarterback", "first quarterback", "Alpha", "QB", 1, 74, 210, 3),
                ("qb2", "Backup", "Quarterback", "backup quarterback", "Alpha", "QB", 2, 73, 205, 2),
                ("rb1", "First", "Runner", "first runner", "Alpha", "RB", 3, 70, 210, 3),
                ("wr1", "First", "Receiver", "first receiver", "Alpha", "WR", 4, 72, 195, 3),
            ))
            connection.commit()

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path): os.unlink(path)

    def game(self, game_id: int, date: str, *, completed: int = 1):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
              start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
              away_team_id,away_team,updated_at) VALUES(?,2026,?,'regular',?,0,?,0,0,
              1,'Alpha',2,'Beta',?)""", (game_id, game_id, date, completed, NOW))
            connection.commit()

    def stat(self, game_id, player_id, player, category, stat_type, value,
             numeric=None):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO game_player_box_stats VALUES(
              ?,1,'Alpha',NULL,'home',NULL,?,?,?,?,?,?)""",
              (game_id, category, stat_type, player_id, player, str(value),
               value if numeric is None and isinstance(value, (int, float)) else numeric))
            connection.commit()

    def seed(self, game_id, date, *, qb1_attempts=30, qb2_attempts=10):
        self.game(game_id, date)
        for pid, name, attempts, yards, tds in (
            ("qb1", "First Quarterback", qb1_attempts, 240, 2),
            ("qb2", "Backup Quarterback", qb2_attempts, 60, 0),
        ):
            self.stat(game_id, pid, name, "passing", "C/ATT", f"{attempts * 2 // 3}/{attempts}")
            self.stat(game_id, pid, name, "passing", "YDS", yards)
            self.stat(game_id, pid, name, "passing", "TD", tds)
        self.stat(game_id, "rb1", "First Runner", "rushing", "CAR", 20)
        self.stat(game_id, "rb1", "First Runner", "rushing", "YDS", 100)
        self.stat(game_id, "rb1", "First Runner", "rushing", "TD", 1)
        self.stat(game_id, "wr1", "First Receiver", "receiving", "REC", 8)
        self.stat(game_id, "wr1", "First Receiver", "receiving", "YDS", 120)
        self.stat(game_id, "wr1", "First Receiver", "receiving", "TD", 1)

    @staticmethod
    def team_projection():
        return {"dropbacks": 40.0, "pass_yards": 300.0, "rush_attempts": 30.0,
                "rush_yards": 150.0, "expected_touchdowns": 3.0}


class AllocationTests(PlayerProjectionFixture):
    def test_active_player_allocations_conserve_team_totals(self):
        self.seed(1, "2026-09-01")
        result = project_team_players(self.repository, "Alpha", 2026,
                                      before_date="2026-09-08",
                                      team_projection=self.team_projection())
        self.assertEqual(result["games"], 1)
        self.assertAlmostEqual(sum(row["expected_dropbacks"] for row in result["players"]), 40.0)
        self.assertAlmostEqual(sum(row["expected_rush_attempts"] for row in result["players"]), 30.0)
        self.assertAlmostEqual(sum(row["expected_receiving_yards"] for row in result["players"]), 300.0)
        self.assertFalse(result["target_projection_available"])

    def test_future_game_never_enters_usage_share(self):
        self.seed(1, "2026-09-01", qb1_attempts=30, qb2_attempts=10)
        self.seed(2, "2026-09-15", qb1_attempts=0, qb2_attempts=40)
        result = project_team_players(self.repository, "Alpha", 2026,
                                      before_date="2026-09-08",
                                      team_projection=self.team_projection())
        by_id = {row["player_id"]: row for row in result["players"]}
        self.assertAlmostEqual(by_id["qb1"]["expected_dropbacks"], 30.0)
        self.assertAlmostEqual(by_id["qb2"]["expected_dropbacks"], 10.0)

    def test_confirmed_out_share_remains_explicitly_unallocated(self):
        self.seed(1, "2026-09-01")
        from sports_aggregator.cfb.external import initialize
        initialize(self.repository)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""INSERT INTO player_availability(
              availability_id,season,team_id,team,player_id,player_name,normalized_name,
              position,status,reported_at,source) VALUES(
              'a1',2026,1,'Alpha','qb1','First Quarterback','first quarterback',
              'QB','out','2026-09-05','test')""")
            connection.commit()
        result = project_team_players(self.repository, "Alpha", 2026,
                                      before_date="2026-09-08",
                                      team_projection=self.team_projection())
        by_id = {row["player_id"]: row for row in result["players"]}
        self.assertEqual(by_id["qb1"]["expected_dropbacks"], 0.0)
        self.assertAlmostEqual(result["unallocated"]["dropbacks"], 30.0)


if __name__ == "__main__":
    unittest.main()
