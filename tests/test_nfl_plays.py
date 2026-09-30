import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sports_aggregator.nfl.plays import (
    PLAY_COLUMNS, box_label, build_play_rows, coverage_shell, defense_package,
    offense_personnel_group,
)
from sports_aggregator.nfl.repository import NFLRepository


def _play(**overrides):
    base = {"game_id": "2025_01_DAL_PHI", "play_id": 100, "season": 2025, "week": 1, "qtr": 1,
            "time": "14:56", "posteam": "DAL", "defteam": "PHI", "home_team": "PHI", "away_team": "DAL",
            "down": 1, "ydstogo": 10, "pass": 1, "rush": 0, "epa": 0.3, "wp": 0.45, "home_wp": 0.55,
            "yards_gained": 7, "desc": "D.Prescott pass short left to C.Lamb for 7 yards."}
    base.update(overrides)
    return base


class ClassifierTests(unittest.TestCase):
    def test_offense_personnel_is_backs_then_tight_ends(self):
        self.assertEqual(offense_personnel_group("1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR"), "11")
        self.assertEqual(offense_personnel_group("1 C, 2 G, 1 QB, 1 RB, 2 T, 2 TE, 2 WR"), "12")
        self.assertEqual(offense_personnel_group("1 C, 1 FB, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 2 WR"), "21")
        self.assertEqual(offense_personnel_group("1 C, 2 G, 1 QB, 2 T, 1 WR, 5 TE"), "05")

    def test_non_standard_lines_are_other_and_blank_is_none(self):
        self.assertEqual(offense_personnel_group("2 C, 1 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR"), "11")  # still 5 OL
        self.assertEqual(offense_personnel_group("1 C, 2 G, 1 QB, 1 RB, 3 T, 1 TE, 2 WR"), "Other")  # 6 OL
        self.assertEqual(offense_personnel_group("1 C, 2 G, 2 QB, 2 T, 3 WR"), "Other")             # 2 QB
        self.assertIsNone(offense_personnel_group(None))

    def test_defense_package_counts_defensive_backs(self):
        self.assertEqual(defense_package("4 DL, 3 LB, 4 DB"), "Base")
        self.assertEqual(defense_package("2 CB, 2 DT, 1 FS, 1 ILB, 1 MLB, 1 NT, 2 OLB, 1 SS"), "Base")
        self.assertEqual(defense_package("4 DL, 2 LB, 5 DB"), "Nickel")
        self.assertEqual(defense_package("3 CB, 1 FS, 1 SS, 2 DT, 2 DE, 2 LB"), "Nickel")
        self.assertEqual(defense_package("4 DL, 1 LB, 6 DB"), "Dime")
        self.assertIsNone(defense_package(""))

    def test_shells_and_box(self):
        self.assertEqual(coverage_shell("COVER_3"), "1-High")
        self.assertEqual(coverage_shell("COVER_6"), "2-High")
        self.assertIsNone(coverage_shell("COMBO"))
        self.assertIsNone(coverage_shell(None))
        self.assertEqual((box_label(5), box_label(6), box_label(8), box_label(0)), ("Light", "Base", "Heavy", None))


class BuildRowsTests(unittest.TestCase):
    def one(self, **kwargs):
        rows = build_play_rows(**kwargs)
        self.assertEqual(len(rows), 1)
        return dict(zip(PLAY_COLUMNS, rows[0]))

    def test_row_width_matches_the_column_list(self):
        row = build_play_rows([_play()])[0]
        self.assertEqual(len(row), len(PLAY_COLUMNS))
        self.assertEqual(len(set(PLAY_COLUMNS)), len(PLAY_COLUMNS))

    def test_participation_and_ftn_join_on_game_and_play(self):
        participation = [{"nflverse_game_id": "2025_01_DAL_PHI", "play_id": 100.0,
                          "offense_formation": "SHOTGUN",
                          "offense_personnel": "1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR",
                          "defense_personnel": "4 DL, 2 LB, 5 DB", "defenders_in_box": 6.0,
                          "number_of_pass_rushers": 4.0, "time_to_throw": 2.6, "was_pressure": False,
                          "defense_man_zone_type": "ZONE_COVERAGE", "defense_coverage_type": "COVER_3"}]
        ftn = [{"nflverse_game_id": "2025_01_DAL_PHI", "nflverse_play_id": 100, "is_motion": True,
                "is_play_action": False, "is_rpo": False, "is_screen_pass": True, "n_blitzers": 1}]
        row = self.one(pbp=[_play()], participation=participation, ftn=ftn)
        self.assertEqual((row["offense_group"], row["defense_package"], row["box"]), ("11", "Nickel", "Base"))
        self.assertEqual((row["coverage"], row["shell"], row["man_zone"]), ("COVER_3", "1-High", "ZONE_COVERAGE"))
        self.assertEqual((row["motion"], row["play_action"], row["screen"], row["blitzers"]), (1, 0, 1, 1))
        self.assertEqual(row["was_pressure"], 0)
        self.assertEqual(row["posteam"], "DAL")

    def test_missing_enhanced_data_still_stores_the_play(self):
        row = self.one(pbp=[_play()])
        self.assertIsNone(row["offense_group"])
        self.assertIsNone(row["coverage"])
        self.assertIsNone(row["motion"])
        self.assertEqual(row["epa"], 0.3)

    def test_coverage_is_only_kept_for_pass_plays(self):
        participation = [{"nflverse_game_id": "2025_01_DAL_PHI", "play_id": 100,
                          "defense_coverage_type": "COVER_2", "defense_man_zone_type": "ZONE_COVERAGE"}]
        row = self.one(pbp=[_play(**{"pass": 0, "rush": 1})], participation=participation)
        self.assertIsNone(row["coverage"])
        self.assertEqual(row["is_rush"], 1)

    def test_non_scrimmage_deleted_and_unkeyed_plays_are_dropped(self):
        plays = [_play(play_id=1, **{"pass": 0, "rush": 0}),          # kickoff / punt
                 _play(play_id=2, play_deleted=1),
                 _play(play_id=None),
                 _play(play_id=4, posteam=None),
                 _play(play_id=5)]
        self.assertEqual([row[PLAY_COLUMNS.index("play_id")] for row in build_play_rows(plays)], [5])

    def test_turnover_and_sack_flags(self):
        row = self.one(pbp=[_play(interception=1, sack=0)])
        self.assertEqual((row["is_turnover"], row["is_sack"]), (1, 0))
        row = self.one(pbp=[_play(fumble_lost=1, sack=1)])
        self.assertEqual((row["is_turnover"], row["is_sack"]), (1, 1))


class RepositoryRoundTripTests(unittest.TestCase):
    def test_replace_plays_replaces_only_that_season(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            first = build_play_rows([_play(play_id=1), _play(play_id=2)])
            other = build_play_rows([_play(game_id="2024_01_DAL_PHI", play_id=1, season=2024)])
            self.assertEqual(repository.replace_plays(2025, first), 2)
            repository.replace_plays(2024, other)
            self.assertEqual(repository.replace_plays(2025, build_play_rows([_play(play_id=9)])), 1)
            with closing(repository._connect()) as connection:
                by_season = {row[0]: row[1] for row in connection.execute(
                    "SELECT season,COUNT(*) FROM nfl_plays GROUP BY season")}
            self.assertEqual(by_season, {2024: 1, 2025: 1})


class ParticipantTableTests(unittest.TestCase):
    def test_on_field_lists_live_in_a_side_table_not_in_nfl_plays(self):
        participation = [{"nflverse_game_id": "2025_01_DAL_PHI", "play_id": 1,
                          "offense_personnel": "1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR",
                          "offense_players": "A;B", "offense_positions": "QB;WR",
                          "defense_players": "C;D", "defense_positions": "CB;FS"}]
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            rows = build_play_rows([_play(play_id=1), _play(play_id=2)], participation)
            self.assertEqual(repository.replace_plays(2025, rows), 2)
            with closing(repository._connect()) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(nfl_plays)")}
                stored = [dict(row) for row in connection.execute("SELECT * FROM nfl_play_participants")]
            self.assertNotIn("offense_players", columns)
            self.assertEqual(len(stored), 1)                         # only the play that had participation
            self.assertEqual((stored[0]["play_id"], stored[0]["offense_players"], stored[0]["defense_positions"]),
                             (1, "A;B", "CB;FS"))

    def test_legacy_inline_columns_are_migrated_away(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nfl.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE nfl_plays (season INTEGER, game_id TEXT, play_id INTEGER, game_seconds INTEGER, "
                               "posteam TEXT, defteam TEXT, is_pass INTEGER, is_rush INTEGER, "
                               "offense_players TEXT, offense_positions TEXT, defense_players TEXT, "
                               "defense_positions TEXT)")
            connection.commit()
            connection.close()
            NFLRepository(path).initialize()
            with closing(sqlite3.connect(path)) as check:
                columns = {row[1] for row in check.execute("PRAGMA table_info(nfl_plays)")}
            self.assertNotIn("offense_players", columns)
            self.assertIn("route", columns)                          # new columns were added by migration
            self.assertIn("is_interception", columns)


if __name__ == "__main__":
    unittest.main()
