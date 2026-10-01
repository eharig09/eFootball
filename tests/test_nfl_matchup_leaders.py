import unittest

from sports_aggregator.nfl.matchups import _leaders


class _Repository:
    def __init__(self, rows):
        self.rows = rows

    def team_roster(self, season, team):
        return [{"player_id": row["player_id"]} for row in self.rows]

    def player_leaders_for_metrics(self, season, metrics, *, team, before_week=None):
        return [row for row in self.rows if row["metric"] in set(metrics)]


def _row(player_id, name, metric, value, position="WR"):
    return {"player_id": player_id, "player_name": name, "position": position, "metric": metric, "value": value}


class LeaderTableTests(unittest.TestCase):
    def setUp(self):
        rows = [_row("q1", "Starter QB", "passing_yards", 800, "QB"), _row("q1", "Starter QB", "attempts", 90, "QB"),
                _row("q2", "Backup QB", "passing_yards", 30, "QB")]
        for index in range(9):
            rows += [_row(f"w{index}", f"Receiver {index}", "receiving_yards", 100 - index),
                     _row(f"w{index}", f"Receiver {index}", "receptions", 10), _row(f"w{index}", f"Receiver {index}", "targets", 15)]
        rows += [_row(f"d{i}", f"Rusher {i}", "def_sacks", 6 - i, "DE") for i in range(8)]
        self.tables = {group["key"]: group for group in _leaders(_Repository(rows), 2026, "AAA", 4)}

    def test_depth_follows_the_role(self):
        self.assertEqual(len(self.tables["passing"]["rows"]), 1)            # one quarterback, not two
        self.assertEqual(len(self.tables["receiving"]["rows"]), 6)
        self.assertEqual(len(self.tables["defense"]["rows"]), 5)
        self.assertNotIn("rushing", self.tables)                             # nobody recorded rushing yards

    def test_rows_are_ordered_and_carry_every_column(self):
        receiving = self.tables["receiving"]
        self.assertEqual(receiving["headers"], ["Tgt", "Rec", "Yds", "Y/R", "TD"])
        self.assertEqual(receiving["rows"][0]["player_name"], "Receiver 0")
        first = receiving["rows"][0]["cells"]
        self.assertEqual([cell["value"] for cell in first[:3]], [15, 10, 100])
        self.assertAlmostEqual(first[3]["value"], 10.0)


if __name__ == "__main__":
    unittest.main()
