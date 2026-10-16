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


class MoneylineBestTests(unittest.TestCase):
    def rows(self, *specs):
        return [{"book": b, "name": bl.book_name(b), **spec} for b, spec in specs]

    def test_the_best_moneyline_is_the_highest_price_for_each_side(self):
        best = bl.best_of(self.rows(
            ("draftkings", {"home_moneyline": 140, "away_moneyline": -165}),
            ("fanduel", {"home_moneyline": 145, "away_moneyline": -170}),
            ("betmgm", {"home_moneyline": 135, "away_moneyline": -160})))
        self.assertEqual((best["home_ml"]["price"], best["home_ml"]["book"]), (145.0, "fanduel"))     # +145 pays most
        self.assertEqual((best["away_ml"]["price"], best["away_ml"]["book"]), (-160.0, "betmgm"))     # -160 beats -170

    def test_a_favorite_and_an_underdog_compare_correctly_across_the_sign(self):
        best = bl.best_of(self.rows(("draftkings", {"home_moneyline": -105, "away_moneyline": -115}),
                                    ("fanduel", {"home_moneyline": 100, "away_moneyline": -120})))
        self.assertEqual(best["home_ml"]["book"], "fanduel")           # +100 beats -105

    def test_side_moneyline_reads_the_price_and_the_book(self):
        best = bl.best_of(self.rows(("fanduel", {"home_moneyline": 145, "away_moneyline": -170})))
        self.assertEqual(bl.side_moneyline(best, "home"), (145.0, "FanDuel"))
        self.assertEqual(bl.side_moneyline(best, "away"), (-170.0, "FanDuel"))
        self.assertEqual(bl.side_moneyline(best, None), (None, None))
        self.assertEqual(bl.side_moneyline(None, "home"), (None, None))

    def test_a_book_with_no_moneyline_does_not_win_anything(self):
        best = bl.best_of(self.rows(("draftkings", {"home_spread": 5.5}), ("fanduel", {"home_moneyline": 130, "away_moneyline": -150})))
        self.assertEqual(best["home_ml"]["book"], "fanduel")

    def test_moneyline_only_rows_still_give_a_best(self):
        self.assertIsNotNone(bl.best_of(self.rows(("fanduel", {"home_moneyline": 130, "away_moneyline": -150}))))

    def test_the_moneyline_does_not_change_the_pick_signature(self):
        base = self.rows(("draftkings", {"home_spread": 5.5, "total": 48.5, "home_price": -110, "away_price": -110,
                                         "over_price": -110, "under_price": -110}))
        moved = [dict(base[0], home_moneyline=150, away_moneyline=-180)]
        self.assertEqual(bl.signature(bl.best_of(base)), bl.signature(bl.best_of(moved)))

    def test_the_display_marks_the_best_moneylines(self):
        c = make_connection()
        self.addCleanup(c.close)
        c.execute("ALTER TABLE odds_api_lines ADD COLUMN home_moneyline REAL")
        c.execute("ALTER TABLE odds_api_lines ADD COLUMN away_moneyline REAL")
        add(c, "draftkings", "2026-10-15T10:00:00", 5.5)
        add(c, "fanduel", "2026-10-15T10:05:00", 5.5)
        c.execute("UPDATE odds_api_lines SET home_moneyline=140, away_moneyline=-165 WHERE bookmaker='draftkings'")
        c.execute("UPDATE odds_api_lines SET home_moneyline=145, away_moneyline=-170 WHERE bookmaker='fanduel'")
        rows = {r["book"]: r for r in bl.display_rows(c, "g1")["rows"]}
        self.assertTrue(rows["fanduel"]["best_home_ml"])
        self.assertTrue(rows["draftkings"]["best_away_ml"])
        self.assertFalse(rows["draftkings"]["best_home_ml"])

    def test_the_fallbacks_carry_moneylines(self):
        c = make_connection()
        self.addCleanup(c.close)
        c.execute("ALTER TABLE game_lines ADD COLUMN home_moneyline REAL")
        c.execute("ALTER TABLE game_lines ADD COLUMN away_moneyline REAL")
        c.execute("INSERT INTO game_lines (game_id, provider, spread, spread_open, over_under, over_under_open, fetched_at, "
                  "home_moneyline, away_moneyline) VALUES (1,'DraftKings',-5.0,-4.0,48.5,48.0,'t',-210,175)")
        row = bl.cfbd_rows(c, 1)[0]
        self.assertEqual((row["home_moneyline"], row["away_moneyline"]), (-210, 175))

    def test_an_older_table_without_the_moneyline_columns_reads_as_none(self):
        c = make_connection()                                   # its tables carry no moneyline columns at all
        self.addCleanup(c.close)
        add(c, "draftkings", "2026-10-15T10:00:00", 5.5)
        c.execute("INSERT INTO game_lines (game_id, provider, spread, over_under, fetched_at) VALUES (1,'ESPN Bet',-4.5,48.0,'t')")
        self.assertEqual(bl.tracker_rows(c, "g1")[0]["home_moneyline"], None)
        self.assertEqual(bl.cfbd_rows(c, 1)[0]["home_moneyline"], None)


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


class TableProvidersTests(unittest.TestCase):
    """The Book by book table: CFBD's rows plus the tracked books, for display only."""

    def cfbd(self):
        return [
            {"provider": "Bovada", "spread": 3.5, "spread_open": 3.0, "over_under": 47.5, "over_under_open": 47.0,
             "formatted_spread": "Away -3.5", "spread_move": 0.5, "total_move": 0.5, "home_moneyline": 150,
             "away_moneyline": -175, "fetched_at": "2026-10-15T10:10:00"},
            {"provider": "DraftKings", "spread": 3.5, "spread_open": 2.5, "over_under": 47.5, "over_under_open": 49.5,
             "formatted_spread": "Away -3.5", "spread_move": 1.0, "total_move": -2.0, "home_moneyline": 150,
             "away_moneyline": -180, "fetched_at": "2026-10-15T10:10:00"},
        ]

    def tracked(self):
        def row(book, spread, total, captured, opened_spread=None, opened_total=None):
            return {"book": book, "name": bl.book_name(book), "source": "odds-api", "home_spread": spread, "total": total,
                    "captured_at": captured, "updated": captured, "opened_spread": opened_spread if opened_spread is not None else spread,
                    "opened_total": opened_total if opened_total is not None else total}
        return [row("draftkings", 3.0, 46.5, "2026-10-15T15:40:00+00:00"),
                row("fanduel", 3.5, 47.0, "2026-10-15T15:40:00+00:00", opened_spread=4.0, opened_total=47.5),
                {"book": "draftkings", "name": "DraftKings", "source": "cfbd", "home_spread": 9.9, "total": 99.0}]

    def merged(self):
        return {p["provider"]: p for p in bl.table_providers(self.cfbd(), self.tracked(), "Old Dominion", "Georgia Southern")}

    def test_new_books_join_and_untracked_ones_are_kept_as_they_were(self):
        merged = self.merged()
        self.assertEqual(set(merged), {"Bovada", "DraftKings", "FanDuel"})
        self.assertEqual(merged["Bovada"], self.cfbd()[0])

    def test_the_trackers_fresher_draftkings_replaces_cfbds_but_keeps_its_opener_and_moneylines(self):
        draftkings = self.merged()["DraftKings"]
        self.assertEqual((draftkings["spread"], draftkings["over_under"]), (3.0, 46.5))
        self.assertEqual(draftkings["spread_move"], 0.5)           # 3.0 now against CFBD's true open of 2.5
        self.assertEqual(draftkings["total_move"], -3.0)           # 46.5 against 49.5
        self.assertEqual((draftkings["home_moneyline"], draftkings["away_moneyline"]), (150, -180))
        self.assertEqual(draftkings["fetched_at"], "2026-10-15T15:40:00+00:00")

    def test_a_tracker_only_book_moves_from_the_first_line_the_tracker_saw(self):
        fanduel = self.merged()["FanDuel"]
        self.assertEqual((fanduel["spread_move"], fanduel["total_move"]), (-0.5, -0.5))
        self.assertIsNone(fanduel["home_moneyline"])

    def test_the_favorite_is_named_the_way_cfbd_formats_it(self):
        merged = self.merged()
        self.assertEqual(merged["FanDuel"]["formatted_spread"], "Georgia Southern -3.5")      # home is the dog
        flipped = bl.table_providers([], [{"book": "fanduel", "name": "FanDuel", "source": "odds-api", "home_spread": -6.0,
                                           "total": 50.0, "captured_at": "t", "opened_spread": -6.0, "opened_total": 50.0}],
                                     "Old Dominion", "Georgia Southern")
        self.assertEqual(flipped[0]["formatted_spread"], "Old Dominion -6.0")
        self.assertEqual(bl._formatted_spread(0.0, "A", "B"), "Pick'em")
        self.assertIsNone(bl._formatted_spread(None, "A", "B"))

    def test_the_original_provider_list_is_never_changed(self):
        original = self.cfbd()
        snapshot = [dict(p) for p in original]
        bl.table_providers(original, self.tracked(), "Old Dominion", "Georgia Southern")
        self.assertEqual(original, snapshot)

    def test_the_trackers_moneyline_fills_the_table_and_replaces_cfbds_when_it_has_one(self):
        providers = self.cfbd()
        tracked = [{"book": "draftkings", "name": "DraftKings", "source": "odds-api", "home_spread": 3.0, "total": 46.5,
                    "captured_at": "t", "opened_spread": 3.0, "opened_total": 46.5, "home_moneyline": 140, "away_moneyline": -165},
                   {"book": "fanduel", "name": "FanDuel", "source": "odds-api", "home_spread": 3.5, "total": 47.0,
                    "captured_at": "t", "opened_spread": 3.5, "opened_total": 47.0, "home_moneyline": 150, "away_moneyline": -170}]
        merged = {p["provider"]: p for p in bl.table_providers(providers, tracked, "Old Dominion", "Georgia Southern")}
        self.assertEqual((merged["DraftKings"]["home_moneyline"], merged["DraftKings"]["away_moneyline"]), (140, -165))
        self.assertEqual((merged["FanDuel"]["home_moneyline"], merged["FanDuel"]["away_moneyline"]), (150, -170))

    def test_cfbds_moneyline_is_kept_when_the_tracker_has_none(self):
        tracked = [{"book": "draftkings", "name": "DraftKings", "source": "odds-api", "home_spread": 3.0, "total": 46.5,
                    "captured_at": "t", "opened_spread": 3.0, "opened_total": 46.5}]
        merged = {p["provider"]: p for p in bl.table_providers(self.cfbd(), tracked, "Home", "Away")}
        self.assertEqual((merged["DraftKings"]["home_moneyline"], merged["DraftKings"]["away_moneyline"]), (150, -180))

    def test_no_tracked_rows_means_the_table_is_exactly_as_before(self):
        self.assertEqual(bl.table_providers(self.cfbd(), [], "Home", "Away"), self.cfbd())
        self.assertEqual(bl.table_providers(self.cfbd(), [{"book": "draftkings", "source": "cfbd"}], "Home", "Away"), self.cfbd())

    def test_the_rows_come_out_in_a_stable_alphabetical_order(self):
        order = [p["provider"] for p in bl.table_providers(self.cfbd(), self.tracked(), "Home", "Away")]
        self.assertEqual(order, sorted(order, key=str.casefold))


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
