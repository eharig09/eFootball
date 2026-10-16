"""Odds sweeps that ride on the app's refresh runs: the refresh-mode gates, auto, and the pending-freeze marker."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sports_aggregator import odds_api_tracker as tracker


def event(event_id="e1", kickoff="2026-10-17T16:00:00Z"):
    book = {"key": "draftkings", "last_update": "2026-10-15T15:00:00Z", "markets": [
        {"key": "spreads", "outcomes": [{"name": "Home U", "price": -110, "point": 5.5},
                                        {"name": "Away U", "price": -110, "point": -5.5}]},
        {"key": "totals", "outcomes": [{"name": "Over", "price": -105, "point": 48.5},
                                       {"name": "Under", "price": -115, "point": 48.5}]}]}
    return {"id": event_id, "commence_time": kickoff, "home_team": "Home U", "away_team": "Away U", "bookmakers": [book]}


class Response:
    def __init__(self, payload, headers=None):
        self.payload, self.headers = payload, headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class Session:
    def __init__(self, events, odds):
        self.events, self.odds, self.calls = events, odds, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/events"):
            return Response(self.events)
        return Response(self.odds, {"x-requests-remaining": "400", "x-requests-used": "100", "x-requests-last": "2"})

    @property
    def paid(self):
        return [c for c in self.calls if c.endswith("/odds")]


class RefreshModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.cfb = root / "cfb.sqlite3"
        with closing(sqlite3.connect(self.cfb)) as c:
            c.executescript("""
                CREATE TABLE teams (team_id INTEGER PRIMARY KEY, school TEXT, mascot TEXT);
                CREATE TABLE games (game_id INTEGER PRIMARY KEY, home_team_id INTEGER, away_team_id INTEGER, start_date TEXT);
                INSERT INTO teams VALUES (1,'Home U','Owls'),(2,'Away U','Bears');
                INSERT INTO games VALUES (900,1,2,'2026-10-17T16:00:00+00:00');""")
        patcher = patch.dict("os.environ", {"CFB_DATABASE_PATH": str(self.cfb), "NFL_DATABASE_PATH": str(root / "nfl.sqlite3"),
                                             "ODDS_API_LEDGER_PATH": str(root / "ledger.sqlite3"), "ODDS_API_KEY": "k"})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Friday 18:00 ET: no slot is due (Friday's is at noon, 6 hours earlier), but a game is under 24 hours away
        self.now = datetime(2026, 10, 16, 18, 0, tzinfo=tracker.EASTERN).astimezone(timezone.utc)
        self.events = [{"id": "e1", "commence_time": "2026-10-17T16:00:00Z"}]

    def session(self, events=None):
        return Session(self.events if events is None else events, [event()])

    def ledger(self, when, league="cfb", status="ok", cost=2, used=None, remaining=None):
        with closing(sqlite3.connect(tracker.ledger_path())) as ledger:
            ledger.executescript(tracker.LEDGER_SCHEMA)
            ledger.execute("INSERT INTO odds_api_calls (called_at,league,cost,used,remaining,status) VALUES (?,?,?,?,?,?)",
                           (tracker._iso(when), league, cost, used, remaining, status))
            ledger.commit()

    def test_a_refresh_run_sweeps_without_a_slot_and_says_which_mode_it_used(self):
        session = self.session()
        result = tracker.auto("cfb", now=self.now, session=session)
        self.assertEqual((result["status"], result["mode"]), ("ok", "refresh"))
        self.assertEqual(len(session.paid), 1)
        self.assertEqual(result["changed_games"], 1)

    def test_a_due_slot_wins_over_refresh_mode(self):
        saturday = datetime(2026, 10, 17, 10, 10, tzinfo=tracker.EASTERN).astimezone(timezone.utc)
        self.assertEqual(tracker.auto("cfb", now=saturday, session=self.session())["mode"], "slot")

    def test_a_recent_look_blocks_the_next_refresh_for_the_minimum_gap(self):
        self.ledger(self.now - timedelta(hours=tracker.REFRESH_MIN_GAP_HOURS - 1))
        session = self.session()
        self.assertEqual(tracker.auto("cfb", now=self.now, session=session)["status"], "not-due")
        self.assertEqual(session.calls, [])

    def test_the_gap_has_passed_after_the_minimum(self):
        self.ledger(self.now - timedelta(hours=tracker.REFRESH_MIN_GAP_HOURS + 1))
        self.assertEqual(tracker.auto("cfb", now=self.now, session=self.session())["status"], "ok")

    def test_a_free_check_that_found_no_games_also_counts_as_a_look(self):
        self.ledger(self.now - timedelta(minutes=20), status="no-games", cost=0)
        session = self.session()
        self.assertEqual(tracker.auto("cfb", now=self.now, session=session)["status"], "not-due")
        self.assertEqual(session.calls, [])

    def test_the_daily_cap_counts_every_sweep_slots_included(self):
        for hours in (4, 8, 12):                       # three sweeps already today, the latest well past the gap
            self.ledger(self.now - timedelta(hours=hours))
        session = self.session()
        result = tracker.auto("cfb", now=self.now, session=session)
        self.assertEqual(result["status"], "not-due")
        self.assertIn("cap", result["message"])
        self.assertEqual(session.calls, [])

    def test_refresh_mode_has_a_smaller_budget_than_the_slots(self):
        self.ledger(self.now - timedelta(days=2), cost=tracker.REFRESH_BUDGET_CAP)
        session = self.session()
        self.assertEqual(tracker.auto("cfb", now=self.now, session=session)["status"], "budget")
        self.assertEqual(session.paid, [])
        # a slot sweep may still use the rest of the month
        saturday = datetime(2026, 10, 17, 10, 10, tzinfo=tracker.EASTERN).astimezone(timezone.utc)
        self.assertEqual(tracker.sweep("cfb", now=saturday, session=self.session(), mode="slot")["status"], "ok")

    def test_refresh_mode_only_looks_ahead_a_couple_of_days(self):
        far = [{"id": "e9", "commence_time": (self.now + timedelta(days=5)).isoformat().replace("+00:00", "Z")}]
        session = self.session(events=far)
        self.assertEqual(tracker.auto("cfb", now=self.now, session=session)["status"], "no-games")
        self.assertEqual(session.paid, [])

    def test_no_key_is_skipped_without_touching_the_ledger(self):
        with patch.dict("os.environ", {"ODDS_API_KEY": ""}):
            self.assertEqual(tracker.auto("cfb", now=self.now, session=self.session())["status"], "skipped")

    def test_the_caps_are_ordered_so_refresh_runs_can_never_use_the_whole_free_quota(self):
        # The arithmetic of how many sweeps fit is beside the point: the caps are what bound the spend, so what matters
        # is that the refresh share sits below the hard cap, which sits below the plan with a reserve to spare.
        self.assertLess(tracker.REFRESH_BUDGET_CAP, tracker.MONTHLY_CAP)
        self.assertLessEqual(tracker.MONTHLY_CAP + tracker.MIN_REMAINING, 500)
        self.assertGreater(tracker.MONTHLY_CAP - tracker.REFRESH_BUDGET_CAP, 10 * tracker.CALL_COST)   # room left for the slots


class PendingFreezeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        patcher = patch.dict("os.environ", {"CFB_DATABASE_PATH": str(root / "cfb.sqlite3"),
                                             "NFL_DATABASE_PATH": str(root / "nfl.sqlite3")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.nfl = root / "nfl.sqlite3"
        self.now = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
        with closing(sqlite3.connect(self.nfl)) as c:
            c.executescript(tracker.LINES_SCHEMA)
            c.commit()

    def line(self, captured, game_id="g1", kickoff="2026-10-18T17:00:00Z"):
        with closing(sqlite3.connect(self.nfl)) as c:
            c.execute("INSERT INTO odds_api_lines (captured_at,league,event_id,commence_time,game_id,home_team,away_team,"
                      "bookmaker,home_spread,total) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (captured, "nfl", "e" + game_id, kickoff, game_id, "H", "A", "draftkings", -3.0, 44.5))
            c.commit()

    def test_new_lines_are_pending_until_the_picks_are_frozen(self):
        self.line("2026-10-15T15:00:00+00:00")
        ids, high = tracker.pending_freeze("nfl", now=self.now)
        self.assertEqual((ids, high), (["g1"], "2026-10-15T15:00:00+00:00"))
        tracker.mark_frozen("nfl", high)
        self.assertEqual(tracker.pending_freeze("nfl", now=self.now)[0], [])

    def test_a_later_line_is_pending_again(self):
        self.line("2026-10-15T15:00:00+00:00")
        tracker.mark_frozen("nfl", tracker.pending_freeze("nfl", now=self.now)[1])
        self.line("2026-10-15T17:00:00+00:00")
        self.assertEqual(tracker.pending_freeze("nfl", now=self.now)[0], ["g1"])

    def test_lines_that_arrive_during_a_freeze_are_not_lost(self):
        self.line("2026-10-15T15:00:00+00:00")
        _ids, high = tracker.pending_freeze("nfl", now=self.now)        # the freeze starts here ...
        self.line("2026-10-15T15:05:00+00:00", game_id="g2")             # ... a sweep lands while it runs ...
        tracker.mark_frozen("nfl", high)                                 # ... and only the earlier mark is recorded
        self.assertEqual(tracker.pending_freeze("nfl", now=self.now)[0], ["g2"])

    def test_games_that_have_already_started_or_are_far_off_are_not_pending(self):
        self.line("2026-10-15T15:00:00+00:00", game_id="past", kickoff="2026-10-15T12:00:00Z")
        self.line("2026-10-15T15:00:00+00:00", game_id="far", kickoff="2026-11-30T17:00:00Z")
        self.assertEqual(tracker.pending_freeze("nfl", now=self.now)[0], [])

    def test_no_database_or_no_lines_is_nothing_pending(self):
        self.assertEqual(tracker.pending_freeze("cfb", now=self.now), ([], None))        # no cfb database file at all
        with closing(sqlite3.connect(self.nfl)) as c:
            c.execute("DELETE FROM odds_api_lines")
            c.commit()
        self.assertEqual(tracker.pending_freeze("nfl", now=self.now), ([], None))


if __name__ == "__main__":
    unittest.main()
