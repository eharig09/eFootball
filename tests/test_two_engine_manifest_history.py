"""Mutable-until-kickoff manifest + first-appearance/closing history tracking.

classify_game() itself needs a lot of live machinery (projections, research,
narrative context, frozen lens scales) that isn't worth faking here -- these
tests exercise the pieces that don't require it directly: the SQL/grading
logic in season_record_first_appearance()/manifest_history_for_game() (which
read cfb_two_engine_manifest_history and games directly, seeded by hand) and
the pure _leg_signature() comparison freeze_week() uses to decide whether a
reclassification counts as a change worth logging. End-to-end freeze_week()
behavior (idempotent no-op on an unchanged reclassification; a real change
both updates the current row and appends history) was verified manually
against the real local database this session.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import two_engine_live as tel
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


class ManifestHistoryFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        tel._initialize_manifest(self.repository)

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def game(self, game_id, *, home, away, season, week, start_date,
             completed=1, home_points=None, away_points=None):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,
                start_time_tbd,completed,neutral_site,conference_game,home_team_id,home_team,
                home_points,away_team_id,away_team,away_points,updated_at)
                VALUES(?,?,?,'regular',?,0,?,0,0,1,?,?,2,?,?,?)""",
                (game_id, season, week, start_date, completed, home, home_points,
                 away, away_points, NOW))
            c.commit()

    def manifest_row(self, game_id, *, season, state, selected_side, selected_team,
                      packet=None):
        payload = json.dumps(packet or {"state": state, "selected_side": selected_side,
                                        "selected_team": selected_team})
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_two_engine_manifest(
                manifest_version,game_id,season,week,kickoff,frozen_at,state,
                selected_side,selected_team,packet_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (tel.MANIFEST_VERSION, game_id, season, 1, NOW, NOW, state,
                 selected_side, selected_team, payload))
            c.commit()

    def history_row(self, game_id, *, recorded_at, market_spread,
                    engine_a=None, engine_b=None, state="pending"):
        engine_a = engine_a or {"qualified": False}
        engine_b = engine_b or {"qualified": False}
        selected_side = engine_a.get("selected_side") if engine_a.get("qualified") else (
            engine_b.get("selected_side") if engine_b.get("qualified") else None)
        selected_team = engine_a.get("selected_team") if engine_a.get("qualified") else (
            engine_b.get("selected_team") if engine_b.get("qualified") else None)
        packet = {"state": state, "engine_a": engine_a, "engine_b": engine_b}
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_two_engine_manifest_history(
                manifest_version,game_id,recorded_at,state,selected_side,
                selected_team,market_spread,packet_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (tel.MANIFEST_VERSION, game_id, recorded_at, state, selected_side,
                 selected_team, market_spread, json.dumps(packet)))
            c.commit()


class LegSignatureTests(unittest.TestCase):
    def test_identical_packets_have_identical_signatures(self):
        packet = {
            "state": "engine_a_only",
            "engine_a": {"qualified": True, "selected_team": "Michigan"},
            "engine_b": {"qualified": False},
        }
        self.assertEqual(tel._leg_signature(packet), tel._leg_signature(dict(packet)))

    def test_engine_b_flipping_alone_counts_as_a_change(self):
        base = {
            "state": "agreement",
            "engine_a": {"qualified": True, "selected_team": "Michigan"},
            "engine_b": {"qualified": True, "selected_team": "Michigan"},
        }
        flipped = {
            "state": "conflict",
            "engine_a": {"qualified": True, "selected_team": "Michigan"},
            "engine_b": {"qualified": True, "selected_team": "Ohio State"},
        }
        self.assertNotEqual(tel._leg_signature(base), tel._leg_signature(flipped))

    def test_losing_qualification_counts_as_a_change(self):
        qualified = {
            "state": "engine_a_only",
            "engine_a": {"qualified": True, "selected_team": "Michigan"},
            "engine_b": {"qualified": False},
        }
        unqualified = {
            "state": "none",
            "engine_a": {"qualified": False},
            "engine_b": {"qualified": False},
        }
        self.assertNotEqual(tel._leg_signature(qualified), tel._leg_signature(unqualified))


class ManifestHistoryForGameTests(ManifestHistoryFixture):
    def test_returns_ordered_timeline(self):
        self.history_row(1, recorded_at="2026-09-01T00:00:00+00:00", market_spread=-3.0)
        self.history_row(1, recorded_at="2026-09-03T00:00:00+00:00", market_spread=-1.5,
                         engine_b={"qualified": True, "selected_side": "away",
                                   "selected_team": "Ohio State"},
                         state="engine_b_only")
        history = tel.manifest_history_for_game(self.repository, 1)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["recorded_at"], "2026-09-01T00:00:00+00:00")
        self.assertEqual(history[1]["state"], "engine_b_only")
        self.assertEqual(history[1]["market_spread"], -1.5)

    def test_empty_for_a_game_with_no_history(self):
        self.assertEqual(tel.manifest_history_for_game(self.repository, 999), [])


class SeasonRecordFirstAppearanceTests(ManifestHistoryFixture):
    def test_grades_using_the_first_qualifying_snapshots_own_market_spread(self):
        # Michigan (home) wins by 10. Engine A first qualified for home at
        # spread -3 (an easy cover); the line then moved to -12 by closing,
        # which would have been a LOSS at the closing number -- first-
        # appearance grading must use -3 (the historical snapshot), not
        # whatever the closing line ended up being.
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2026-09-05T00:00:00+00:00",
                  completed=1, home_points=31, away_points=21)
        self.history_row(
            1, recorded_at="2026-09-01T00:00:00+00:00", market_spread=-3.0,
            engine_a={"qualified": True, "selected_side": "home", "selected_team": "Michigan"},
            state="engine_a_only",
        )
        self.history_row(
            1, recorded_at="2026-09-04T00:00:00+00:00", market_spread=-12.0,
            engine_a={"qualified": True, "selected_side": "home", "selected_team": "Michigan"},
            state="engine_a_only",
        )
        result = tel.season_record_first_appearance(self.repository, 2026)
        # home_margin=10, side_spread=-3 (home favored by 3) -> edge = 10-3 = 7 > 0 -> win.
        self.assertEqual(result["engine_a"]["record"], "1-0")
        self.assertEqual(result["engine_a"]["n"], 1)

    def test_only_grades_a_leg_once_even_with_multiple_history_rows(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2026-09-05T00:00:00+00:00",
                  completed=1, home_points=20, away_points=17)
        for i in range(3):
            self.history_row(
                1, recorded_at=f"2026-09-0{i+1}T00:00:00+00:00", market_spread=-3.0 - i,
                engine_a={"qualified": True, "selected_side": "home", "selected_team": "Michigan"},
                state="engine_a_only",
            )
        result = tel.season_record_first_appearance(self.repository, 2026)
        self.assertEqual(result["engine_a"]["n"], 1)

    def test_a_leg_that_never_qualified_is_not_graded(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2026-09-05T00:00:00+00:00",
                  completed=1, home_points=20, away_points=17)
        self.history_row(1, recorded_at="2026-09-01T00:00:00+00:00", market_spread=-3.0)
        result = tel.season_record_first_appearance(self.repository, 2026)
        self.assertEqual(result["engine_a"]["n"], 0)
        self.assertEqual(result["engine_b"]["n"], 0)

    def test_incomplete_games_are_excluded(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2026-09-05T00:00:00+00:00", completed=0)
        self.history_row(
            1, recorded_at="2026-09-01T00:00:00+00:00", market_spread=-3.0,
            engine_a={"qualified": True, "selected_side": "home", "selected_team": "Michigan"},
            state="engine_a_only",
        )
        result = tel.season_record_first_appearance(self.repository, 2026)
        self.assertEqual(result["engine_a"]["n"], 0)

    def test_engine_a_and_engine_b_first_appearances_are_independent(self):
        # Engine A qualifies immediately; Engine B only qualifies later, at a
        # different (and different-side) line. Each leg's own first
        # appearance must be found independently.
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2026-09-05T00:00:00+00:00",
                  completed=1, home_points=14, away_points=21)
        self.history_row(
            1, recorded_at="2026-09-01T00:00:00+00:00", market_spread=-3.0,
            engine_a={"qualified": True, "selected_side": "home", "selected_team": "Michigan"},
            state="engine_a_only",
        )
        self.history_row(
            1, recorded_at="2026-09-03T00:00:00+00:00", market_spread=1.0,
            engine_a={"qualified": True, "selected_side": "home", "selected_team": "Michigan"},
            engine_b={"qualified": True, "selected_side": "away", "selected_team": "Ohio State"},
            state="agreement",
        )
        result = tel.season_record_first_appearance(self.repository, 2026)
        self.assertEqual(result["engine_a"]["n"], 1)
        self.assertEqual(result["engine_b"]["n"], 1)
        # Engine A graded at spread -3 (its own first appearance): home_margin=-7,
        # side_spread=-3 -> edge=-10 -> loss.
        self.assertEqual(result["engine_a"]["record"], "0-1")
        # Engine B graded at spread +1 (its own first appearance, away side):
        # margin=7 (away perspective), side_spread=-1 -> edge=6 -> win.
        self.assertEqual(result["engine_b"]["record"], "1-0")


class DisplayPacketIsFinalTests(ManifestHistoryFixture):
    def test_manifest_row_for_an_upcoming_game_is_not_final(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2099-01-01T00:00:00+00:00", completed=0)
        self.manifest_row(1, season=2026, state="engine_a_only",
                          selected_side="home", selected_team="Michigan")
        with closing(sqlite3.connect(self.path)) as c:
            game = dict(zip(
                [d[0] for d in c.execute("SELECT * FROM games WHERE game_id=1").description],
                c.execute("SELECT * FROM games WHERE game_id=1").fetchone(),
            ))
        packet = tel.display_packet(self.repository, game)
        self.assertEqual(packet["source"], "frozen_manifest")
        self.assertFalse(packet["is_final"])

    def test_manifest_row_for_a_completed_game_is_final(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2026-09-05T00:00:00+00:00",
                  completed=1, home_points=20, away_points=17)
        self.manifest_row(1, season=2026, state="engine_a_only",
                          selected_side="home", selected_team="Michigan")
        with closing(sqlite3.connect(self.path)) as c:
            game = dict(zip(
                [d[0] for d in c.execute("SELECT * FROM games WHERE game_id=1").description],
                c.execute("SELECT * FROM games WHERE game_id=1").fetchone(),
            ))
        packet = tel.display_packet(self.repository, game)
        self.assertEqual(packet["source"], "frozen_manifest")
        self.assertTrue(packet["is_final"])

    def test_completed_game_with_no_manifest_row_is_the_unfrozen_stub(self):
        self.game(1, home="Michigan", away="Ohio State", season=2026, week=1,
                  start_date="2026-09-05T00:00:00+00:00",
                  completed=1, home_points=20, away_points=17)
        with closing(sqlite3.connect(self.path)) as c:
            game = dict(zip(
                [d[0] for d in c.execute("SELECT * FROM games WHERE game_id=1").description],
                c.execute("SELECT * FROM games WHERE game_id=1").fetchone(),
            ))
        packet = tel.display_packet(self.repository, game)
        self.assertEqual(packet["state"], "unfrozen_completed")
        self.assertTrue(packet["is_final"])


if __name__ == "__main__":
    unittest.main()
