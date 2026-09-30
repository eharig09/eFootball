import unittest
from pathlib import Path
import tempfile

from sports_aggregator.nfl.position_groups import (
    position_group_defense, position_group_matchups,
)
from sports_aggregator.nfl.repository import NFLRepository


def _row(game, week, team, defense, player, yards, *, targets=10, position="WR",
         carries=0, rushing_yards=0, rushing_tds=0):
    return {
        "season": 2026, "week": week, "season_type": "REG", "game_id": game,
        "player_id": player.lower(), "player_name": player, "team": team,
        "opponent_team": defense, "position": position, "targets": targets,
        "receptions": targets / 2, "receiving_yards": yards,
        "receiving_tds": 0, "receiving_first_downs": 0,
        "carries": carries, "rushing_yards": rushing_yards, "rushing_tds": rushing_tds,
        "rushing_first_downs": 0,
    }


class _Repository:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def position_group_game_stats(self, season, *, before_week=None):
        self.calls.append((season, before_week))
        return [row for row in self.rows if before_week is None or row["week"] < before_week]


class PositionGroupDefenseTests(unittest.TestCase):
    def setUp(self):
        self.repository = _Repository([
            _row("g1", 1, "AAA", "D1", "Alpha One", 100),
            _row("g2", 2, "AAA", "D2", "Alpha One", 50),
            _row("g3", 1, "BBB", "D2", "Beta One", 40),
            _row("g4", 2, "BBB", "D1", "Beta One", 60),
        ])

    def test_group_total_is_compared_with_league_and_leave_one_matchup_out_quality(self):
        profile = position_group_defense(self.repository, 2026, "D1")
        wr_total = profile["groups"][0]["rows"][0]
        self.assertEqual(profile["games"], 2)
        self.assertAlmostEqual(wr_total["per_game"]["receiving_yards"], 80)
        self.assertAlmostEqual(wr_total["league_per_game"]["receiving_yards"], 62.5)
        self.assertAlmostEqual(wr_total["expected_per_game"]["receiving_yards"], 45)
        self.assertAlmostEqual(wr_total["league_index"], 128)
        self.assertAlmostEqual(wr_total["opponent_quality_index"], 72)
        self.assertAlmostEqual(wr_total["adjusted_allowed_index"], 177.777777, places=5)

    def test_observed_roles_are_labeled_and_matchup_names_use_only_pregame_work(self):
        cards = position_group_matchups(
            self.repository, 2026, 3, "AAA", "D1", baseline_season=2026,
        )
        aaa = cards[0]
        wr1 = aaa["profile"]["groups"][0]["rows"][1]
        self.assertEqual(aaa["offense"], "AAA")
        self.assertEqual(wr1["role"], "WR1")
        self.assertEqual(wr1["player"]["player_name"], "Alpha One")
        self.assertEqual(self.repository.calls[-1], (2026, 3))

    def test_prior_season_baseline_does_not_apply_current_week_cutoff(self):
        position_group_matchups(
            self.repository, 2027, 1, "AAA", "D1", baseline_season=2026,
        )
        self.assertEqual(self.repository.calls[-1], (2026, None))

    def test_qb_rushing_joins_backfield_total_but_not_rb_depth_roles(self):
        repository = _Repository([
            _row("g1", 1, "AAA", "D1", "Lead Back", 20, position="RB",
                 targets=2, carries=12, rushing_yards=55),
            _row("g1", 1, "AAA", "D1", "Mobile QB", 15, position="QB",
                 targets=1, carries=6, rushing_yards=35, rushing_tds=1),
            _row("g2", 2, "AAA", "D2", "Lead Back", 10, position="RB",
                 targets=1, carries=10, rushing_yards=45),
            _row("g2", 2, "AAA", "D2", "Mobile QB", 0, position="QB",
                 carries=4, rushing_yards=20),
        ])
        profile = position_group_defense(repository, 2026, "D1")
        backfield = next(group for group in profile["groups"] if group["key"] == "RB")
        by_role = {row["role"]: row for row in backfield["rows"]}
        self.assertEqual(by_role["RB total"]["per_game"]["rushing_yards"], 90)
        self.assertEqual(by_role["QB rush"]["per_game"]["rushing_yards"], 35)
        self.assertEqual(by_role["QB rush"]["per_game"]["receiving_yards"], 0)
        self.assertEqual(by_role["RB1"]["per_game"]["rushing_yards"], 55)
        self.assertNotEqual((by_role["RB1"].get("player") or {}).get("player_name"), "Mobile QB")


class PositionGroupRepositoryTests(unittest.TestCase):
    def test_player_game_pivot_filters_positions_and_honors_pregame_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = NFLRepository(Path(directory) / "nfl.sqlite3")
            repository.initialize()
            repository.replace_weekly_stats(2026, [
                {"season": 2026, "week": 1, "season_type": "REG", "game_id": "g1",
                 "player_id": "wr1", "player_display_name": "Wide One", "team": "AAA",
                 "opponent_team": "BBB", "position": "WR", "targets": 8,
                 "receptions": 5, "receiving_yards": 70},
                {"season": 2026, "week": 1, "season_type": "REG", "game_id": "g1",
                 "player_id": "qb1", "player_display_name": "Quarter Back", "team": "AAA",
                 "opponent_team": "BBB", "position": "QB", "rushing_yards": 20},
                {"season": 2026, "week": 2, "season_type": "REG", "game_id": "g2",
                 "player_id": "wr1", "player_display_name": "Wide One", "team": "AAA",
                 "opponent_team": "CCC", "position": "WR", "targets": 6,
                 "receptions": 4, "receiving_yards": 55},
            ])
            rows = repository.position_group_game_stats(2026, before_week=2)
            self.assertEqual(len(rows), 2)
            by_player = {row["player_name"]: row for row in rows}
            self.assertEqual(by_player["Wide One"]["targets"], 8)
            self.assertEqual(by_player["Wide One"]["receiving_yards"], 70)
            self.assertEqual(by_player["Quarter Back"]["rushing_yards"], 20)


if __name__ == "__main__":
    unittest.main()
