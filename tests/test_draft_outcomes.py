import csv
import os
import tempfile
import unittest

from sports_aggregator.cfb.draft_outcomes import (
    draft_position_bucket, import_outcomes, outcome_summary, read_outcomes,
)
from sports_aggregator.cfb.repository import CFBRepository


#: A minimal row shaped exactly like the real export's fixed-width layout:
#: Rnd,Pick,Tm,Player,Pos,Age,To,AP1,PB,St,wAV,DrAV,G,
#: Cmp,Att,Yds,TD,Int,Att,Yds,TD,Rec,Yds,TD,Solo,Int,Sk,College/Univ,<link>,Year
_HEADER = (
    "Rnd,Pick,Tm,Player,Pos,Age,To,AP1,PB,St,wAV,DrAV,G,"
    "Cmp,Att,Yds,TD,Int,Att,Yds,TD,Rec,Yds,TD,Solo,Int,Sk,College/Univ,Unnamed: 28_level_1,Year"
).split(",")


def _row(**overrides) -> list[str]:
    defaults = {
        "Rnd": "1", "Pick": "1", "Tm": "STL", "Player": "Sam Bradford", "Pos": "QB",
        "Age": "22", "To": "2018", "AP1": "0", "PB": "0", "St": "5", "wAV": "44", "DrAV": "25",
        "G": "83", "Cmp": "1855", "Att": "2967", "Yds": "19449", "TD": "103", "Int": "61",
        "College/Univ": "Oklahoma", "Unnamed: 28_level_1": "College Stats", "Year": "2010",
    }
    defaults.update(overrides)
    return [defaults.get(name, "") for name in _HEADER]


def _write_csv(path: str, rows: list[list[str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_HEADER)
        writer.writerows(rows)


class PositionBucketTests(unittest.TestCase):
    def test_specific_codes_map_to_the_draft_vocabulary(self):
        self.assertEqual(draft_position_bucket("DE"), "Defensive Edge")
        self.assertEqual(draft_position_bucket("t"), "Offensive Tackle")
        self.assertEqual(draft_position_bucket("HB"), "Running Back")

    def test_generic_era_codes_are_kept_as_their_own_unspecified_bucket(self):
        # PFR's own "DB", "OL", and "DL" codes cannot be resolved to a specific
        # position without guessing, so they must not collapse into CB/S or T/G.
        self.assertEqual(draft_position_bucket("DB"), "Defensive Back (unspecified)")
        self.assertEqual(draft_position_bucket("OL"), "Offensive Line (unspecified)")
        self.assertEqual(draft_position_bucket("DL"), "Defensive Line (unspecified)")

    def test_unknown_code_falls_back_to_a_title_cased_label(self):
        self.assertEqual(draft_position_bucket(""), "Unknown")
        self.assertEqual(draft_position_bucket(None), "Unknown")


class ReadOutcomesTests(unittest.TestCase):
    def test_identity_and_outcome_columns_are_read_from_their_fixed_positions(self):
        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        try:
            row = _row(
                Rnd="3", Pick="70", Tm="CIN", Player="Distinct Player", Pos="CB",
                Age="21", To="2024", AP1="2", PB="4", St="9", wAV="55", DrAV="30",
                G="150", **{"College/Univ": "Distinct State", "Year": "2015"},
            )
            _write_csv(path, [row])
            entry = read_outcomes(path)[0]
            self.assertEqual(entry["round"], 3)
            self.assertEqual(entry["pick"], 70)
            self.assertEqual(entry["nfl_team"], "CIN")
            self.assertEqual(entry["draft_age"], 21)
            self.assertEqual(entry["last_season"], 2024)
            self.assertEqual(entry["all_pro_seasons"], 2)
            self.assertEqual(entry["pro_bowls"], 4)
            self.assertEqual(entry["seasons_started"], 9)
            self.assertEqual(entry["career_av"], 55)
            self.assertEqual(entry["team_av"], 30)
            self.assertEqual(entry["games"], 150)
            self.assertEqual(entry["college"], "Distinct State")
            self.assertEqual(entry["draft_year"], 2015)
        finally:
            os.unlink(path)

    def test_rows_missing_a_round_pick_or_year_are_skipped(self):
        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        try:
            _write_csv(path, [
                _row(),
                _row(Rnd="", Pick="2", Player="No Round"),
                _row(Rnd="1", Pick="3", Year="", Player="No Year"),
            ])
            entries = read_outcomes(path)
            self.assertEqual([entry["player_name"] for entry in entries], ["Sam Bradford"])
        finally:
            os.unlink(path)


class ImportAndSummaryTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.repository = CFBRepository(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _import(self, rows: list[list[str]]) -> dict:
        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        try:
            _write_csv(path, rows)
            return import_outcomes(self.repository, path)
        finally:
            os.unlink(path)

    def test_import_reports_row_and_year_counts(self):
        counts = self._import([
            _row(Rnd="1", Pick="1", Year="2010"),
            _row(Rnd="1", Pick="1", Year="2011", Player="Someone Else"),
        ])
        self.assertEqual(counts["rows"], 2)
        self.assertEqual(counts["draft_years"], [2010, 2011])

    def test_reimporting_a_year_replaces_it_rather_than_appending(self):
        self._import([_row(Rnd="1", Pick="1", Year="2010", Player="First Version")])
        self._import([_row(Rnd="1", Pick="1", Year="2010", Player="Corrected Version")])
        with self.repository._reader() as connection:
            names = [row["player_name"] for row in connection.execute(
                "SELECT player_name FROM historical_draft_outcomes WHERE draft_year=2010")]
        self.assertEqual(names, ["Corrected Version"])

    def test_an_empty_store_summarizes_to_nothing_without_raising(self):
        self.assertEqual(outcome_summary(self.repository), [])

    def test_summary_computes_starter_and_pro_bowl_rates_per_bucket(self):
        self._import([
            _row(Rnd="1", Pick="1", Year="2010", Pos="CB", Player="Starter One",
                 St="5", PB="1", wAV="40"),
            _row(Rnd="1", Pick="2", Year="2011", Pos="CB", Player="Bust One",
                 St="0", PB="0", wAV="2"),
            _row(Rnd="6", Pick="180", Year="2012", Pos="CB", Player="Day 3 Corner",
                 St="1", PB="0", wAV="10"),
        ])
        summary = {(row["position"], row["round_bucket"]): row
                   for row in outcome_summary(self.repository, min_sample=1)}
        round1_cb = summary[("Cornerback", "Round 1")]
        self.assertEqual(round1_cb["n"], 2)
        self.assertAlmostEqual(round1_cb["starter_rate"], 0.5)
        self.assertAlmostEqual(round1_cb["pro_bowl_rate"], 0.5)
        self.assertEqual(round1_cb["median_career_av"], 40)
        day3_cb = summary[("Cornerback", "Rounds 4-7")]
        self.assertEqual(day3_cb["n"], 1)

    def test_thin_buckets_are_flagged_not_hidden(self):
        self._import([_row(Rnd="1", Pick="1", Year="2010", Pos="LS", Player="Rare Pick")])
        summary = outcome_summary(self.repository, min_sample=5)
        row = next(item for item in summary if item["position"] == "Long Snapper")
        self.assertEqual(row["n"], 1)
        self.assertFalse(row["reliable"])


if __name__ == "__main__":
    unittest.main()
