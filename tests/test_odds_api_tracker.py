"""The Odds API tracker: parsing, change-only storage, the schedule, and above all the credit guards."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sports_aggregator import odds_api_tracker as tracker


def book(key, home_spread, total=48.5, home_price=-110, update="2026-10-15T15:00:00Z", ml=None):
    markets = [
        {"key": "spreads", "outcomes": [{"name": "Home U", "price": home_price, "point": home_spread},
                                        {"name": "Away U", "price": -110, "point": -home_spread}]},
        {"key": "totals", "outcomes": [{"name": "Over", "price": -105, "point": total},
                                       {"name": "Under", "price": -115, "point": total}]}]
    if ml is not None:
        markets.append({"key": "h2h", "outcomes": [{"name": "Home U", "price": ml[0]}, {"name": "Away U", "price": ml[1]}]})
    return {"key": key, "last_update": update, "markets": markets}


def event(event_id="e1", books=None):
    return {"id": event_id, "commence_time": "2026-10-17T16:00:00Z", "home_team": "Home U", "away_team": "Away U",
            "bookmakers": books if books is not None else [book("draftkings", 5.5), book("fanduel", 5.0)]}


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload, self.headers = payload, headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class FakeSession:
    """Records every request so a test can assert that nothing paid was called."""

    def __init__(self, events, odds, remaining=480, used=20):
        self.events, self.odds = events, odds
        self.headers = {"x-requests-remaining": str(remaining), "x-requests-used": str(used), "x-requests-last": "2"}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        self.params = params
        if url.endswith("/events"):
            return FakeResponse(self.events, {"x-requests-remaining": "480", "x-requests-last": "0"})
        return FakeResponse(self.odds, self.headers)

    @property
    def paid_calls(self):
        return [c for c in self.calls if c.endswith("/odds")]


class ParseTests(unittest.TestCase):
    def test_only_the_tracked_books_are_kept(self):
        rows = tracker.parse_event(event(books=[book("draftkings", 5.5), book("bovada", 5.0), book("betonlineag", 6.0),
                                                book("betrivers", 5.0), book("espnbet", 5.5), book("betmgm", 5.5)]))
        self.assertEqual({r["bookmaker"] for r in rows}, {"draftkings", "betrivers", "espnbet", "betmgm"})

    def test_spread_and_total_come_from_the_home_side_and_over_under(self):
        row = tracker.parse_event(event(books=[book("fanduel", -3.5, total=51.5, home_price=-112)]))[0]
        self.assertEqual((row["home_spread"], row["home_spread_price"], row["away_spread_price"]), (-3.5, -112, -110))
        self.assertEqual((row["total"], row["over_price"], row["under_price"]), (51.5, -105, -115))

    def test_a_book_with_no_usable_market_is_skipped(self):
        empty = {"key": "draftkings", "last_update": "x", "markets": []}
        self.assertEqual(tracker.parse_event(event(books=[empty])), [])


class MoneylineTests(unittest.TestCase):
    def test_the_moneyline_is_requested_and_the_cost_reflects_three_markets(self):
        self.assertIn("h2h", tracker.MARKETS.split(","))
        self.assertEqual(tracker.CALL_COST, len(tracker.MARKETS.split(",")) * len(tracker.REGIONS.split(",")))

    def test_both_moneylines_are_parsed_by_team(self):
        row = tracker.parse_event(event(books=[book("fanduel", -3.5, ml=(-170, 145))]))[0]
        self.assertEqual((row["home_moneyline"], row["away_moneyline"]), (-170, 145))

    def test_a_book_with_no_moneyline_market_has_none(self):
        row = tracker.parse_event(event(books=[book("fanduel", -3.5)]))[0]
        self.assertEqual((row["home_moneyline"], row["away_moneyline"]), (None, None))

    def test_a_book_that_only_posts_a_moneyline_is_still_kept(self):
        only = {"key": "betrivers", "last_update": "x", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Home U", "price": -200}, {"name": "Away U", "price": 170}]}]}
        rows = tracker.parse_event(event(books=[only]))
        self.assertEqual((rows[0]["home_moneyline"], rows[0]["home_spread"]), (-200, None))

    def test_a_moneyline_for_unknown_teams_is_ignored(self):
        wrong = {"key": "betmgm", "last_update": "x", "markets": [
            {"key": "spreads", "outcomes": [{"name": "Home U", "price": -110, "point": 3.0}, {"name": "Away U", "price": -110, "point": -3.0}]},
            {"key": "h2h", "outcomes": [{"name": "Somebody Else", "price": -200}, {"name": "Another", "price": 170}]}]}
        self.assertEqual(tracker.parse_event(event(books=[wrong]))[0]["home_moneyline"], None)


class MigrationTests(unittest.TestCase):
    def test_a_table_made_before_moneylines_gains_the_columns_and_keeps_its_rows(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE odds_api_lines (line_id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,
                league TEXT NOT NULL, event_id TEXT NOT NULL, commence_time TEXT NOT NULL, game_id TEXT,
                home_team TEXT NOT NULL, away_team TEXT NOT NULL, bookmaker TEXT NOT NULL, home_spread REAL,
                home_spread_price REAL, away_spread_price REAL, total REAL, over_price REAL, under_price REAL,
                book_updated_at TEXT);
            INSERT INTO odds_api_lines (captured_at,league,event_id,commence_time,home_team,away_team,bookmaker,home_spread,total)
                VALUES ('2026-10-14T10:00:00+00:00','cfb','e1','2026-10-17T16:00:00Z','Home U','Away U','fanduel',3.0,48.5);""")
        connection.row_factory = sqlite3.Row
        tracker.ensure_lines_schema(connection)
        tracker.ensure_lines_schema(connection)                       # idempotent
        columns = {r[1] for r in connection.execute("PRAGMA table_info(odds_api_lines)")}
        self.assertTrue({"home_moneyline", "away_moneyline"} <= columns)
        self.assertEqual(connection.execute("SELECT home_spread FROM odds_api_lines").fetchone()[0], 3.0)

    def test_the_first_sweep_after_the_upgrade_stores_the_moneyline_as_a_move(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        legacy = tracker.LINES_SCHEMA.replace("home_moneyline REAL, away_moneyline REAL", "").replace("book_updated_at TEXT,", "book_updated_at TEXT")
        connection.executescript(legacy)
        connection.execute(
            "INSERT INTO odds_api_lines (captured_at,league,event_id,commence_time,game_id,home_team,away_team,bookmaker,"
            "home_spread,home_spread_price,away_spread_price,total,over_price,under_price) "
            "VALUES ('2026-10-14T10:00:00+00:00','cfb','e1','2026-10-17T16:00:00Z','g1','Home U','Away U','draftkings',"
            "5.5,-110,-110,48.5,-105,-115)")
        now = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)
        stored, _ = tracker.store_snapshot(connection, "cfb", now, [event(books=[book("draftkings", 5.5, ml=(150, -180))])],
                                            lambda e: "g1")
        self.assertEqual(stored, 1)
        latest = connection.execute("SELECT home_moneyline, away_moneyline FROM odds_api_lines ORDER BY line_id DESC").fetchone()
        self.assertEqual((latest[0], latest[1]), (150, -180))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.now = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)

    def store(self, events, resolve=lambda e: "g1", when=None):
        return tracker.store_snapshot(self.connection, "cfb", when or self.now, events, resolve)

    def test_rows_are_appended_only_when_a_number_moves(self):
        self.assertEqual(self.store([event()])[0], 2)
        self.assertEqual(self.store([event()], when=self.now + timedelta(hours=3))[0], 0)       # nothing moved
        moved = event(books=[book("draftkings", 6.0), book("fanduel", 5.0)])
        self.assertEqual(self.store([moved], when=self.now + timedelta(hours=6))[0], 1)         # one book moved
        rows = self.connection.execute(
            "SELECT home_spread FROM odds_api_lines WHERE bookmaker='draftkings' ORDER BY line_id").fetchall()
        self.assertEqual([r[0] for r in rows], [5.5, 6.0])

    def test_a_moneyline_change_alone_counts_as_a_move(self):
        self.store([event(books=[book("fanduel", 5.5, ml=(150, -180))])])
        self.assertEqual(self.store([event(books=[book("fanduel", 5.5, ml=(150, -180))])], when=self.now + timedelta(hours=1))[0], 0)
        stored, _ = self.store([event(books=[book("fanduel", 5.5, ml=(145, -175))])], when=self.now + timedelta(hours=2))
        self.assertEqual(stored, 1)

    def test_a_price_change_alone_counts_as_a_move(self):
        self.store([event(books=[book("fanduel", 5.5, home_price=-110)])])
        stored, _ = self.store([event(books=[book("fanduel", 5.5, home_price=-115)])], when=self.now + timedelta(hours=1))
        self.assertEqual(stored, 1)

    def test_the_game_id_is_filled_in_once_a_match_exists(self):
        self.store([event()], resolve=lambda e: None)
        self.assertIsNone(self.connection.execute("SELECT game_id FROM odds_api_lines LIMIT 1").fetchone()[0])
        self.store([event()], resolve=lambda e: "401", when=self.now + timedelta(hours=3))
        self.assertEqual({r[0] for r in self.connection.execute("SELECT game_id FROM odds_api_lines")}, {"401"})


class ScheduleTests(unittest.TestCase):
    def at(self, year, month, day, hour, minute=0):
        return datetime(year, month, day, hour, minute, tzinfo=tracker.EASTERN).astimezone(timezone.utc)

    def test_a_college_slot_is_due_just_after_it_and_fires_once(self):
        now = self.at(2026, 10, 17, 10, 20)                    # Saturday 10:20 ET, slot at 10:00
        slot = tracker.slot_due(now, "cfb", None)
        self.assertEqual(slot, self.at(2026, 10, 17, 10, 0))
        self.assertIsNone(tracker.slot_due(now, "cfb", slot + timedelta(minutes=2)))           # already done

    def test_nothing_is_due_in_the_quiet_hours(self):
        self.assertIsNone(tracker.slot_due(self.at(2026, 10, 18, 3, 0), "cfb", None))          # Sunday 03:00
        self.assertIsNone(tracker.slot_due(self.at(2026, 10, 14, 2, 0), "nfl", None))          # Wednesday 02:00

    def test_a_slot_is_not_made_up_long_after_it_passed(self):
        self.assertIsNone(tracker.slot_due(self.at(2026, 10, 17, 13, 30), "cfb", None))        # 3.5h after the 10:00 slot

    def test_nfl_has_its_own_slots(self):
        self.assertIsNotNone(tracker.slot_due(self.at(2026, 10, 18, 11, 40), "nfl", None))     # Sunday 11:30
        self.assertIsNone(tracker.slot_due(self.at(2026, 10, 18, 11, 40), "cfb", None))

    def test_the_weekly_load_stays_well_inside_the_free_quota(self):
        weekly = sum(len(slots) for slots in tracker.SLOTS.values()) * tracker.CALL_COST
        self.assertLess(weekly * 4.5, tracker.MONTHLY_CAP)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.ledger = sqlite3.connect(":memory:")
        self.ledger.row_factory = sqlite3.Row
        self.ledger.executescript(tracker.LEDGER_SCHEMA)
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def call(self, when, cost=2, remaining=None, used=None, status="ok"):
        self.ledger.execute("INSERT INTO odds_api_calls (called_at,league,cost,remaining,used,status) VALUES (?,?,?,?,?,?)",
                            (tracker._iso(when), "cfb", cost, remaining, used, status))

    def test_an_empty_ledger_allows_a_call(self):
        self.assertTrue(tracker.budget_allows(self.ledger, self.now)[0])

    def test_the_monthly_cap_stops_calls(self):
        for _ in range(tracker.MONTHLY_CAP // 2):
            self.call(datetime(2026, 10, 3, tzinfo=timezone.utc))
        ok, reason = tracker.budget_allows(self.ledger, self.now)
        self.assertFalse(ok)
        self.assertIn("monthly cap", reason)

    def test_last_months_calls_do_not_count(self):
        for _ in range(300):
            self.call(datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc))
        self.assertTrue(tracker.budget_allows(self.ledger, self.now)[0])

    def test_the_apis_own_remaining_count_is_a_second_guard(self):
        self.call(datetime(2026, 10, 5, tzinfo=timezone.utc), remaining=tracker.MIN_REMAINING + 1)
        ok, reason = tracker.budget_allows(self.ledger, self.now)
        self.assertFalse(ok)
        self.assertIn("reserve", reason)

    def test_credits_spent_outside_the_tool_are_noticed_through_the_apis_used_count(self):
        self.call(datetime(2026, 10, 5, tzinfo=timezone.utc), cost=2, used=tracker.MONTHLY_CAP - 1, remaining=300)
        self.assertFalse(tracker.budget_allows(self.ledger, self.now)[0])

    def test_refusals_are_not_counted_as_spend(self):
        for _ in range(500):
            self.call(self.now, status="budget", cost=0)
        self.assertEqual(tracker.month_used(self.ledger, self.now), 0)


class SweepTests(unittest.TestCase):
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
                                             "ODDS_API_LEDGER_PATH": str(root / "ledger.sqlite3"), "ODDS_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Saturday 10:10 ET: a college slot is due
        self.now = datetime(2026, 10, 17, 10, 10, tzinfo=tracker.EASTERN).astimezone(timezone.utc)
        self.upcoming = [{"id": "e1", "commence_time": "2026-10-17T16:00:00Z"}]
        self.odds = [event()]

    def session(self, **kwargs):
        return FakeSession(kwargs.get("events", self.upcoming), kwargs.get("odds", self.odds))

    def test_a_sweep_asks_for_all_three_markets(self):
        session = self.session()
        tracker.sweep("cfb", now=self.now, session=session)
        self.assertEqual(session.params["markets"], "spreads,totals,h2h")

    def test_a_due_sweep_stores_lines_matches_games_and_records_the_spend(self):
        session = self.session()
        result = tracker.sweep("cfb", now=self.now, session=session)
        self.assertEqual(result["status"], "ok")
        self.assertEqual((result["events"], result["matched_games"], result["rows_stored"]), (1, 1, 2))
        self.assertEqual(len(session.paid_calls), 1)
        with closing(sqlite3.connect(self.cfb)) as c:
            self.assertEqual(c.execute("SELECT DISTINCT game_id FROM odds_api_lines").fetchall(), [("900",)])
        self.assertEqual(tracker.status()["month_used"], 2)

    def test_a_sweep_that_is_not_due_makes_no_request_at_all(self):
        session = self.session()
        result = tracker.sweep("cfb", now=self.now.replace(hour=3), session=session)
        self.assertEqual(result["status"], "not-due")
        self.assertEqual(session.calls, [])

    def test_the_same_slot_never_fires_twice(self):
        tracker.sweep("cfb", now=self.now, session=self.session())
        again = self.session()
        self.assertEqual(tracker.sweep("cfb", now=self.now + timedelta(minutes=30), session=again)["status"], "not-due")
        self.assertEqual(again.paid_calls, [])

    def test_no_games_coming_up_costs_nothing(self):
        session = self.session(events=[])
        result = tracker.sweep("cfb", now=self.now, session=session)
        self.assertEqual(result["status"], "no-games")
        self.assertEqual(session.paid_calls, [])

    def test_a_game_beyond_the_look_ahead_does_not_justify_a_sweep(self):
        far = [{"id": "e9", "commence_time": (self.now + timedelta(days=30)).isoformat().replace("+00:00", "Z")}]
        session = self.session(events=far)
        self.assertEqual(tracker.sweep("cfb", now=self.now, session=session)["status"], "no-games")
        self.assertEqual(session.paid_calls, [])

    def test_a_spent_budget_blocks_even_a_forced_sweep_before_any_request(self):
        with closing(sqlite3.connect(tracker.ledger_path())) as ledger:
            ledger.executescript(tracker.LEDGER_SCHEMA)
            ledger.execute("INSERT INTO odds_api_calls (called_at,league,cost,status) VALUES (?,?,?,?)",
                           (tracker._iso(self.now - timedelta(days=1)), "cfb", tracker.MONTHLY_CAP, "ok"))
            ledger.commit()
        session = self.session()
        result = tracker.sweep("cfb", force=True, now=self.now, session=session)
        self.assertEqual(result["status"], "budget")
        self.assertEqual(session.calls, [])

    def test_force_skips_the_schedule_but_not_the_budget(self):
        session = self.session()
        result = tracker.sweep("cfb", force=True, now=self.now.replace(hour=3), session=session)
        self.assertEqual(result["status"], "ok")

    def test_no_key_means_no_request(self):
        with patch.dict("os.environ", {"ODDS_API_KEY": ""}):
            session = self.session()
            self.assertEqual(tracker.sweep("cfb", now=self.now, session=session)["status"], "skipped")
            self.assertEqual(session.calls, [])

    def test_the_key_never_appears_in_a_returned_error(self):
        class Boom(FakeSession):
            def get(self, url, params=None, timeout=None):
                if url.endswith("/events"):
                    return super().get(url, params, timeout)
                raise RuntimeError(f"401 for {params['apiKey']}")
        session = Boom(self.upcoming, self.odds)
        result = tracker.sweep("cfb", now=self.now, session=session)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("test-key", result["message"])


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE teams (team_id INTEGER PRIMARY KEY, school TEXT, mascot TEXT);
            CREATE TABLE games (game_id INTEGER PRIMARY KEY, home_team_id INTEGER, away_team_id INTEGER, start_date TEXT);
            INSERT INTO teams VALUES (3,'Clemson','Tigers'),(4,'Miami','Hurricanes'),(5,'Miami (OH)','RedHawks'),
                                     (6,'Texas A&M','Aggies');
            INSERT INTO games VALUES (10,3,99,'2026-10-17T16:00:00+00:00');""")
        # the apostrophe and the mangled accent live in the real table
        self.connection.execute("INSERT INTO teams VALUES (1, ?, 'Rainbow Warriors')", ("Hawai'i",))
        self.connection.execute("INSERT INTO teams VALUES (2, ?, 'Spartans')", ("San Jos� State",))
        self.addCleanup(self.connection.close)
        self.lookup = tracker.cfb_team_lookup(self.connection)

    def test_mascots_apostrophes_and_accents_resolve_to_the_school(self):
        for name, expected in (("Hawaii Rainbow Warriors", 1), ("San Jose State Spartans", 2), ("Clemson Tigers", 3),
                               ("Miami Hurricanes", 4), ("Miami (OH) RedHawks", 5), ("Texas A&M Aggies", 6)):
            self.assertEqual(tracker.resolve_cfb_team(name, self.lookup), expected, name)

    def test_an_unknown_team_is_not_guessed(self):
        self.assertIsNone(tracker.resolve_cfb_team("Charleston Southern Buccaneers", self.lookup))

    def test_a_game_against_an_fcs_team_matches_on_the_fbs_side(self):
        found = tracker.cfb_game_id(self.connection, self.lookup, {
            "home_team": "Clemson Tigers", "away_team": "Charleston Southern Buccaneers",
            "commence_time": "2026-10-17T16:00:00Z"})
        self.assertEqual(found, "10")


if __name__ == "__main__":
    unittest.main()
