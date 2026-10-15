"""New engine picks are judged against the best tracked line; picks frozen before that keep the consensus grading."""
from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sports_aggregator import best_lines
from sports_aggregator.cfb import engine_picks
from sports_aggregator.cfb import two_engine_live as tel
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


def best(home=None, away=None):
    """A frozen best_lines block: spreads are already oriented to the side."""
    out = {"books": 3}
    if home is not None:
        out["home"] = {"spread": home, "book": "fanduel", "name": "FanDuel", "price": -110}
    if away is not None:
        out["away"] = {"spread": away, "book": "betrivers", "name": "BetRivers", "price": -110}
    return out


class Fixture(unittest.TestCase):
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

    def game(self, game_id, *, home_points, away_points, season=2026):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,start_time_tbd,completed,neutral_site,
                conference_game,home_team_id,home_team,home_points,away_team_id,away_team,away_points,updated_at)
                VALUES(?,?,1,'regular','2026-01-01T00:00:00+00:00',0,1,0,0,1,'Home',?,2,'Away',?,?)""",
                      (game_id, season, home_points, away_points, NOW))
            c.commit()

    def history(self, game_id, packet, market_spread):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_two_engine_manifest_history(manifest_version,game_id,recorded_at,state,selected_side,
                selected_team,market_spread,packet_json) VALUES(?,?,?,?,?,?,?,?)""",
                      (tel.MANIFEST_VERSION, game_id, NOW, packet.get("state", "engine_a_only"), "home", "Home",
                       market_spread, json.dumps(packet)))
            c.commit()

    def manifest(self, game_id, packet, season=2026):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO cfb_two_engine_manifest(manifest_version,game_id,season,week,kickoff,frozen_at,state,
                selected_side,selected_team,packet_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (tel.MANIFEST_VERSION, game_id, season, 1, NOW, NOW, packet.get("state", "engine_a_only"),
                       "home", "Home", json.dumps(packet)))
            c.commit()

    def consensus_line(self, game_id, spread):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS game_lines (game_id INTEGER NOT NULL, season INTEGER NOT NULL,
                provider TEXT NOT NULL, spread REAL, spread_open REAL, over_under REAL, over_under_open REAL,
                home_moneyline REAL, away_moneyline REAL, formatted_spread TEXT, fetched_at TEXT,
                PRIMARY KEY(game_id,provider))""")
            c.execute("INSERT OR REPLACE INTO game_lines(game_id,season,provider,spread,fetched_at) VALUES(?,?,?,?,?)",
                      (game_id, 2026, "Bovada", spread, NOW))
            c.commit()


LEG = {"qualified": True, "selected_side": "home", "selected_team": "Home"}


class PickSpreadTests(unittest.TestCase):
    def test_the_best_line_wins_over_consensus(self):
        self.assertEqual(tel._pick_spread({"best_lines": best(home=-5.5, away=5.0)}, "home", -6.0), (-5.5, True))
        self.assertEqual(tel._pick_spread({"best_lines": best(home=-5.5, away=5.0)}, "away", -6.0), (5.0, True))

    def test_without_a_best_line_consensus_is_used_as_before(self):
        self.assertEqual(tel._pick_spread({}, "home", -6.0), (-6.0, False))
        self.assertEqual(tel._pick_spread({}, "away", -6.0), (6.0, False))
        self.assertEqual(tel._pick_spread({"best_lines": None}, "home", -6.0), (-6.0, False))

    def test_a_best_line_missing_that_side_falls_back(self):
        self.assertEqual(tel._pick_spread({"best_lines": best(home=-5.5)}, "away", -6.0), (6.0, False))

    def test_nothing_known_is_nothing_to_grade(self):
        self.assertEqual(tel._pick_spread({}, "home", None), (None, False))


class FirstAppearanceTests(Fixture):
    def test_a_new_pick_is_graded_at_the_best_line_and_counted(self):
        self.game(1, home_points=27, away_points=21)               # home wins by 6
        self.history(1, {"engine_a": LEG, "engine_b": {"qualified": False}, "best_lines": best(home=-5.5, away=5.0)},
                     market_spread=-6.0)
        record = tel.season_record_first_appearance(self.repository, 2026)
        self.assertEqual(record["engine_a"]["record"], "1-0")        # at -5.5 the home side covers; at -6.0 it pushes
        self.assertEqual(record["engine_a"]["at_best_line"], 1)

    def test_an_old_packet_is_still_graded_at_the_consensus_line(self):
        self.game(1, home_points=27, away_points=21)
        self.history(1, {"engine_a": LEG, "engine_b": {"qualified": False}}, market_spread=-6.0)
        record = tel.season_record_first_appearance(self.repository, 2026)
        self.assertEqual(record["engine_a"]["record"], "0-0-1")     # a push at -6.0
        self.assertEqual(record["engine_a"]["at_best_line"], 0)

    def test_a_pick_with_a_best_line_is_graded_even_when_no_consensus_was_recorded(self):
        self.game(1, home_points=27, away_points=21)
        self.history(1, {"engine_a": LEG, "engine_b": {"qualified": False}, "best_lines": best(home=-5.5)}, market_spread=None)
        self.assertEqual(tel.season_record_first_appearance(self.repository, 2026)["engine_a"]["record"], "1-0")

    def test_the_away_side_uses_the_away_number(self):
        self.game(1, home_points=27, away_points=21)               # away loses by 6, so +5.0 does not cover
        leg = {"qualified": True, "selected_side": "away", "selected_team": "Away"}
        self.history(1, {"engine_a": leg, "engine_b": {"qualified": False}, "best_lines": best(home=-5.5, away=5.0)}, -6.0)
        self.assertEqual(tel.season_record_first_appearance(self.repository, 2026)["engine_a"]["record"], "0-1")


class ClosingRecordTests(Fixture):
    def test_the_closing_record_grades_new_picks_at_the_best_line(self):
        self.game(1, home_points=27, away_points=21)
        self.consensus_line(1, -6.0)
        self.manifest(1, {"engine_a": LEG, "engine_b": {"qualified": False}, "best_lines": best(home=-5.5, away=5.0)})
        record = tel.record_summary(self.repository, 2026)
        self.assertEqual((record["engine_a"]["record"], record["engine_a"]["at_best_line"]), ("1-0", 1))

    def test_the_closing_record_keeps_consensus_for_an_old_packet(self):
        self.game(1, home_points=27, away_points=21)
        self.consensus_line(1, -6.0)
        self.manifest(1, {"engine_a": LEG, "engine_b": {"qualified": False}})
        record = tel.record_summary(self.repository, 2026)
        self.assertEqual((record["engine_a"]["record"], record["engine_a"]["at_best_line"]), ("0-0-1", 0))

    def test_old_and_new_picks_are_counted_separately_in_one_record(self):
        self.game(1, home_points=27, away_points=21)
        self.game(2, home_points=27, away_points=21)
        self.consensus_line(1, -6.0)
        self.consensus_line(2, -6.0)
        self.manifest(1, {"engine_a": LEG, "engine_b": {"qualified": False}, "best_lines": best(home=-5.5)})
        self.manifest(2, {"engine_a": LEG, "engine_b": {"qualified": False}})
        record = tel.record_summary(self.repository, 2026)["engine_a"]
        self.assertEqual((record["n"], record["at_best_line"], record["record"]), (2, 1, "1-0-1"))


class PacketTests(unittest.TestCase):
    def test_a_moved_best_line_refreshes_the_packet_but_a_new_timestamp_alone_does_not(self):
        base = {"state": "none", "best_lines": {"home": {"spread": 5.5, "book": "draftkings"}, "as_of": "t1"}}
        later = {"state": "none", "best_lines": {"home": {"spread": 5.5, "book": "draftkings"}, "as_of": "t2"}}
        moved = {"state": "none", "best_lines": {"home": {"spread": 6.0, "book": "draftkings"}, "as_of": "t2"}}
        self.assertEqual(tel._packet_signature(base), tel._packet_signature(later))
        self.assertNotEqual(tel._packet_signature(base), tel._packet_signature(moved))

    def test_a_failure_finding_the_best_line_never_breaks_a_classification(self):
        class Repository:
            def _reader(self):
                raise RuntimeError("database is locked")
        self.assertIsNone(tel._best_lines_for(Repository(), {"game_id": 1}))


class PicksTableTests(unittest.TestCase):
    GAME = {"game_id": 1, "completed": 1, "home_points": 27, "away_points": 21}

    def row(self, packet, side="home"):
        leg = {"selected_side": side, "selected_team": "Home" if side == "home" else "Away", "qualified": True}
        return engine_picks._engine_row(dict(self.GAME), {"engine_a": leg, **packet}, "engine_a")

    def test_the_line_shown_and_graded_is_the_best_one_with_its_book(self):
        row = self.row({"market_spread": -6.0, "best_lines": best(home=-5.5, away=5.0)})
        self.assertEqual((row["line"], row["line_book"], row["result"]), (-5.5, "FanDuel", "Win"))

    def test_an_old_packet_shows_and_grades_the_consensus_line(self):
        row = self.row({"market_spread": -6.0})
        self.assertEqual((row["line"], row["line_book"], row["result"]), (-6.0, None, "Push"))

    def test_the_away_pick_shows_the_away_number(self):
        row = self.row({"market_spread": -6.0, "best_lines": best(home=-5.5, away=5.0)}, side="away")
        self.assertEqual((row["line"], row["line_book"], row["result"]), (5.0, "BetRivers", "Loss"))


if __name__ == "__main__":
    unittest.main()
