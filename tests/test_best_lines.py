"""The best available number across the tracked books: selection, ties, orientation, and the frozen-packet form."""

import sqlite3
import unittest

from sports_aggregator import best_lines as bl


def make_connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE odds_api_lines (
            line_id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, league TEXT, event_id TEXT, commence_time TEXT,
            game_id TEXT, home_team TEXT, away_team TEXT, bookmaker TEXT, home_spread REAL, home_spread_price REAL,
            away_spread_price REAL, total REAL, over_price REAL, under_price REAL, book_updated_at TEXT);
        CREATE TABLE game_lines (game_id INTEGER, provider TEXT, spread REAL, spread_open REAL, over_under REAL,
            over_under_open REAL, fetched_at TEXT);""")
    return connection


def add(connection, book, captured, spread, home_price=-110, away_price=-110, total=48.5, over=-110, under=-110, game="g1"):
    connection.execute(
        "INSERT INTO odds_api_lines (captured_at,league,event_id,commence_time,game_id,home_team,away_team,bookmaker,"
        "home_spread,home_spread_price,away_spread_price,total,over_price,under_price,book_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (captured, "cfb", "e1", "2026-10-17T16:00:00Z", game, "Home", "Away", book, spread, home_price, away_price,
         total, over, under, captured))


class TrackerRowsTests(unittest.TestCase):
    def setUp(self):
        self.c = make_connection()
        self.addCleanup(self.c.close)

    def test_the_latest_row_per_book_and_its_opener(self):
        add(self.c, "draftkings", "2026-10-14T10:00:00", 6.0)
        add(self.c, "draftkings", "2026-10-15T10:00:00", 5.5)
        row = bl.tracker_rows(self.c, "g1")[0]
        self.assertEqual((row["book"], row["home_spread"], row["opened_spread"]), ("draftkings", 5.5, 6.0))

    def test_untracked_books_are_ignored(self):
        add(self.c, "bovada", "2026-10-15T10:00:00", 4.0)
        add(self.c, "betonlineag", "2026-10-15T10:00:00", 4.0)
        add(self.c, "fanduel", "2026-10-15T10:00:00", 5.5)
        self.assertEqual([r["book"] for r in bl.tracker_rows(self.c, "g1")], ["fanduel"])

    def test_as_of_limits_what_was_known(self):
        add(self.c, "fanduel", "2026-10-14T10:00:00", 6.0)
        add(self.c, "fanduel", "2026-10-15T10:00:00", 5.0)
        self.assertEqual(bl.tracker_rows(self.c, "g1", as_of="2026-10-14T23:00:00")[0]["home_spread"], 6.0)

    def test_a_database_without_the_table_has_no_rows_not_an_error(self):
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        self.assertEqual(bl.tracker_rows(empty, "g1"), [])
        self.assertIsNone(bl.snapshot(empty, "g1"))


class BestOfTests(unittest.TestCase):
    def rows(self, *specs):
        return [{"book": b, "name": bl.book_name(b), **spec} for b, spec in specs]

    def test_the_home_best_is_the_highest_spread_and_the_away_best_the_lowest(self):
        best = bl.best_of(self.rows(
            ("draftkings", {"home_spread": 5.5, "home_price": -110, "away_price": -110}),
            ("betrivers", {"home_spread": 5.0, "home_price": -113, "away_price": -110}),
            ("betmgm", {"home_spread": 6.0, "home_price": -108, "away_price": -110})))
        self.assertEqual((best["home"]["spread"], best["home"]["book"]), (6.0, "betmgm"))     # home +6 beats +5.5, +5
        self.assertEqual((best["away"]["spread"], best["away"]["book"]), (-5.0, "betrivers"))  # away lays 5, not 6

    def test_a_tie_on_the_number_goes_to_the_better_price(self):
        best = bl.best_of(self.rows(
            ("draftkings", {"home_spread": 5.5, "home_price": -110, "away_price": -110}),
            ("fanduel", {"home_spread": 5.5, "home_price": -105, "away_price": -115}),
            ("betmgm", {"home_spread": 5.5, "home_price": -108, "away_price": -105})))
        self.assertEqual(best["home"]["book"], "fanduel")
        self.assertEqual(best["away"]["book"], "betmgm")

    def test_a_missing_price_loses_a_tie(self):
        best = bl.best_of(self.rows(
            ("draftkings", {"home_spread": 5.5, "home_price": None, "away_price": None}),
            ("fanduel", {"home_spread": 5.5, "home_price": -120, "away_price": -120})))
        self.assertEqual(best["home"]["book"], "fanduel")

    def test_over_wants_the_lowest_total_and_under_the_highest(self):
        best = bl.best_of(self.rows(
            ("draftkings", {"total": 48.5, "over_price": -110, "under_price": -110}),
            ("fanduel", {"total": 47.5, "over_price": -115, "under_price": -105}),
            ("betmgm", {"total": 49.0, "over_price": -105, "under_price": -115})))
        self.assertEqual((best["over"]["total"], best["over"]["book"]), (47.5, "fanduel"))
        self.assertEqual((best["under"]["total"], best["under"]["book"]), (49.0, "betmgm"))

    def test_a_book_with_only_a_total_still_counts_for_the_total(self):
        best = bl.best_of(self.rows(("draftkings", {"home_spread": None, "total": 50.0, "over_price": -110, "under_price": -110})))
        self.assertNotIn("home", best)
        self.assertEqual(best["over"]["total"], 50.0)

    def test_no_rows_means_no_best(self):
        self.assertIsNone(bl.best_of([]))

    def test_book_count_and_average(self):
        best = bl.best_of(self.rows(("draftkings", {"home_spread": 5.0}), ("fanduel", {"home_spread": 6.0})))
        self.assertEqual((best["books"], best["average_home_spread"]), (2, 5.5))


class SnapshotAndGradingHelpersTests(unittest.TestCase):
    def setUp(self):
        self.c = make_connection()
        self.addCleanup(self.c.close)
        add(self.c, "draftkings", "2026-10-15T10:00:00", 5.5)
        add(self.c, "betrivers", "2026-10-15T10:05:00", 5.0, home_price=-113)

    def test_the_snapshot_is_json_safe_and_records_its_moment(self):
        import json
        snap = bl.snapshot(self.c, "g1")
        json.dumps(snap)
        self.assertEqual(snap["as_of"], "2026-10-15T10:05:00")
        self.assertEqual(snap["home"]["spread"], 5.5)

    def test_side_spread_reads_the_side_and_the_book(self):
        snap = bl.snapshot(self.c, "g1")
        self.assertEqual(bl.side_spread(snap, "home"), (5.5, "DraftKings"))
        self.assertEqual(bl.side_spread(snap, "away"), (-5.0, "BetRivers"))
        self.assertEqual(bl.side_spread(snap, None), (None, None))
        self.assertEqual(bl.side_spread(None, "home"), (None, None))

    def test_the_signature_changes_with_a_number_or_a_book_but_not_a_timestamp(self):
        first = bl.signature(bl.snapshot(self.c, "g1"))
        add(self.c, "betmgm", "2026-10-15T12:00:00", 5.5, home_price=-110, total=48.5)     # same numbers, later time
        later = bl.snapshot(self.c, "g1")
        self.assertEqual(bl.signature(later)[1], first[1])
        add(self.c, "betmgm", "2026-10-15T13:00:00", 6.5)
        self.assertNotEqual(bl.signature(bl.snapshot(self.c, "g1")), first)

    def test_a_late_as_of_for_a_new_game_returns_nothing(self):
        self.assertIsNone(bl.snapshot(self.c, "g1", as_of="2020-01-01T00:00:00"))


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.c = make_connection()
        self.addCleanup(self.c.close)

    def test_cfbd_names_map_to_the_tracked_books_and_others_are_dropped(self):
        for provider, spread in (("DraftKings", -5.0), ("Draft Kings", -5.5), ("ESPN Bet", -4.5), ("Bovada", -3.0),
                                 ("William Hill (New Jersey)", -6.0)):
            self.c.execute("INSERT INTO game_lines VALUES (1, ?, ?, ?, 48.5, 49.0, ?)", (provider, spread, spread - 1, "2026-10-15T00:00:00"))
        rows = {r["book"]: r for r in bl.cfbd_rows(self.c, 1)}
        self.assertEqual(set(rows), {"draftkings", "espnbet"})
        self.assertEqual(rows["draftkings"]["home_spread"], -5.5)           # later row for the same book wins
        self.assertIsNone(rows["draftkings"]["home_price"])

    def test_the_trackers_reading_beats_the_fallback_for_the_same_book(self):
        add(self.c, "draftkings", "2026-10-15T10:00:00", 5.5)
        self.c.execute("INSERT INTO game_lines VALUES (1, 'DraftKings', 4.0, 4.0, 48.5, 48.5, '2026-10-14T00:00:00')")
        self.c.execute("INSERT INTO game_lines VALUES (1, 'ESPN Bet', 6.0, 6.0, 48.5, 48.5, '2026-10-14T00:00:00')")
        add(self.c, "draftkings", "2026-10-15T10:00:00", 5.5, game="1")
        merged = bl.merge_fallback(bl.tracker_rows(self.c, "1"), bl.cfbd_rows(self.c, 1))
        by_book = {r["book"]: r for r in merged}
        self.assertEqual(by_book["draftkings"]["source"], "odds-api")
        self.assertEqual(by_book["espnbet"]["source"], "cfbd")

    def test_a_game_with_only_cfbd_lines_still_has_a_best(self):
        self.c.execute("INSERT INTO game_lines VALUES (1, 'DraftKings', -6.0, -6.0, 50.0, 50.0, '2026-10-14T00:00:00')")
        snap = bl.snapshot(self.c, 1, fallback=bl.cfbd_rows(self.c, 1))
        self.assertEqual(snap["home"]["spread"], -6.0)


class DisplayTests(unittest.TestCase):
    def test_the_best_numbers_are_flagged_and_movement_is_shown(self):
        c = make_connection()
        self.addCleanup(c.close)
        add(c, "draftkings", "2026-10-14T10:00:00", 6.0, total=49.5)
        add(c, "draftkings", "2026-10-15T10:00:00", 5.5, total=48.5)
        add(c, "fanduel", "2026-10-15T10:00:00", 5.0, total=48.5, over=-105, under=-115)
        view = bl.display_rows(c, "g1")
        rows = {r["book"]: r for r in view["rows"]}
        self.assertTrue(rows["draftkings"]["best_home"])           # +5.5 beats +5.0 for the home side
        self.assertTrue(rows["fanduel"]["best_away"])              # away lays 5.0, less than 5.5
        self.assertTrue(rows["fanduel"]["best_over"])              # same total, better over price
        self.assertEqual(rows["draftkings"]["spread_move"], -0.5)
        self.assertEqual(rows["draftkings"]["total_move"], -1.0)
        self.assertEqual([r["book"] for r in view["rows"]], ["draftkings", "fanduel"])      # a stable, familiar order
        self.assertTrue(view["has_data"])

    def test_no_data_is_an_empty_display_not_an_error(self):
        c = make_connection()
        self.addCleanup(c.close)
        self.assertFalse(bl.display_rows(c, "nope")["has_data"])


if __name__ == "__main__":
    unittest.main()
