import csv
import os
import tempfile
import unittest

from sports_aggregator.cfb.draft_backtest import _pearson, consensus_backtest
from sports_aggregator.cfb.models import Player, Team
from sports_aggregator.cfb.prospects import import_board
from sports_aggregator.cfb.repository import CFBRepository


class PearsonTests(unittest.TestCase):
    def test_perfectly_matching_order_is_a_correlation_of_one(self):
        self.assertAlmostEqual(_pearson([1, 2, 3, 4], [1, 2, 3, 4]), 1.0)

    def test_perfectly_inverted_order_is_a_correlation_of_negative_one(self):
        self.assertAlmostEqual(_pearson([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)

    def test_fewer_than_two_points_has_no_defined_correlation(self):
        self.assertIsNone(_pearson([1], [1]))
        self.assertIsNone(_pearson([], []))

    def test_a_constant_series_has_no_defined_correlation(self):
        self.assertIsNone(_pearson([1, 1, 1], [1, 2, 3]))


class ConsensusBacktestTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.repository = CFBRepository(self.db_path)
        self.repository.replace_teams([Team.from_cfbd(payload) for payload in (
            {"id": 1, "school": "Texas", "mascot": "Longhorns", "abbreviation": "TEX",
             "alternateNames": [], "conference": "SEC", "classification": "fbs",
             "color": "#BF5700", "logos": []},
            {"id": 2, "school": "Ohio State", "mascot": "Buckeyes", "abbreviation": "OSU",
             "alternateNames": [], "conference": "Big Ten", "classification": "fbs",
             "color": "#BB0000", "logos": []},
        )])
        self.repository.replace_players(2025, (
            Player("qb1", 2025, "Top", "Quarterback", "Texas", "QB", 10, 76, 220, 4),
            Player("wr1", 2025, "Rising", "Receiver", "Ohio State", "WR", 11, 73, 195, 3),
            Player("bust1", 2025, "Overrated", "Prospect", "Texas", "LB", 5, 74, 235, 4),
            Player("undrafted1", 2025, "Never", "Drafted", "Ohio State", "CB", 21, 71, 185, 3),
        ))

    def tearDown(self):
        os.unlink(self.db_path)

    def _import_board(self, rows: list[tuple[str, str, str, str]]) -> None:
        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        try:
            with open(path, "w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["Rank", "Player", "School", "Position"])
                writer.writerows(rows)
            import_board(self.repository, path, draft_year=2026, source="test",
                        roster_season=2025)
        finally:
            os.unlink(path)

    def _seed_draft_picks(self, picks: list[dict]) -> None:
        self.repository.replace_draft_picks(2026, picks)

    def test_an_empty_board_backtests_to_nothing_without_raising(self):
        result = consensus_backtest(self.repository, draft_year=2026)
        self.assertEqual(result["matched"], 0)
        self.assertIsNone(result["rank_correlation"])
        self.assertEqual(result["top_n_hit_rate"]["rate"], None)

    def test_matched_rows_compute_rank_error_against_the_real_pick(self):
        self._import_board([
            ("1", "Top Quarterback", "Texas", "QB"),
            ("2", "Rising Receiver", "Ohio State", "WR"),
        ])
        self._seed_draft_picks([
            {"overall": 1, "round": 1, "pick": 1, "collegeAthleteId": "qb1",
             "collegeTeam": "Texas", "nflTeam": "AAA", "name": "Top Quarterback",
             "position": "QB"},
            # The board had him #2; he actually went pick 40 -- oversold.
            {"overall": 40, "round": 2, "pick": 8, "collegeAthleteId": "wr1",
             "collegeTeam": "Ohio State", "nflTeam": "BBB", "name": "Rising Receiver",
             "position": "WR"},
        ])
        result = consensus_backtest(self.repository, draft_year=2026, source="test")
        self.assertEqual(result["matched"], 2)
        by_name = {row["player_name"]: row for row in
                   result["board_oversold"] + result["board_undersold"]}
        self.assertEqual(by_name["Top Quarterback"]["rank_error"], 0)
        self.assertEqual(by_name["Rising Receiver"]["rank_error"], 2 - 40)

    def test_unresolved_and_undrafted_board_rows_are_reported_not_dropped(self):
        self._import_board([
            ("1", "Top Quarterback", "Texas", "QB"),
            ("2", "Overrated Prospect", "Texas", "LB"),
            ("3", "Nobody On A Roster", "Texas", "WR"),
        ])
        self._seed_draft_picks([
            {"overall": 1, "round": 1, "pick": 1, "collegeAthleteId": "qb1",
             "collegeTeam": "Texas", "nflTeam": "AAA", "name": "Top Quarterback",
             "position": "QB"},
        ])
        result = consensus_backtest(self.repository, draft_year=2026, source="test")
        self.assertEqual(result["matched"], 1)
        # "Overrated Prospect" resolved to a roster player (bust1) but was never
        # in draft_picks: undrafted, not unresolved.
        self.assertEqual(result["undrafted_or_unmatched"], 1)
        # "Nobody On A Roster" never matched a roster player at all.
        self.assertEqual(result["unresolved"], 1)

    def test_top_n_hit_rate_counts_first_round_picks_within_the_board_window(self):
        self._import_board([
            ("1", "Top Quarterback", "Texas", "QB"),
            ("2", "Rising Receiver", "Ohio State", "WR"),
        ])
        self._seed_draft_picks([
            {"overall": 5, "round": 1, "pick": 5, "collegeAthleteId": "qb1",
             "collegeTeam": "Texas", "nflTeam": "AAA", "name": "Top Quarterback",
             "position": "QB"},
            {"overall": 90, "round": 3, "pick": 30, "collegeAthleteId": "wr1",
             "collegeTeam": "Ohio State", "nflTeam": "BBB", "name": "Rising Receiver",
             "position": "WR"},
        ])
        result = consensus_backtest(self.repository, draft_year=2026, source="test", top_n=2)
        self.assertEqual(result["top_n_hit_rate"], {"n": 2, "round_one": 1, "rate": 0.5})


if __name__ == "__main__":
    unittest.main()
