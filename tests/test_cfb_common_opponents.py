import unittest

from sports_aggregator.cfb.page_visuals import common_opponent_rows


def game(game_id, start, home, away, home_points, away_points, completed=True, neutral=False):
    return {"game_id": game_id, "start_date": start, "home_team_id": home[0], "home_team": home[1],
            "away_team_id": away[0], "away_team": away[1], "home_points": home_points,
            "away_points": away_points, "completed": completed, "neutral_site": neutral,
            "date_label": start[:10]}


A, H, X, Y, Z = (1, "A"), (2, "H"), (3, "X"), (4, "Y"), (5, "Z")
THIS = "2026-10-10T16:00:00Z"


class CommonOpponentTests(unittest.TestCase):
    def test_only_shared_opponents_with_each_sides_result_and_edge(self):
        away = [game(1, "2026-09-05T16:00:00Z", A, X, 30, 20), game(2, "2026-09-12T16:00:00Z", Y, A, 10, 13)]
        home = [game(3, "2026-09-06T16:00:00Z", X, H, 27, 24), game(4, "2026-09-13T16:00:00Z", H, Z, 40, 0)]
        rows = common_opponent_rows(away, home, away_id=1, home_id=2, before_date=THIS)
        self.assertEqual([row["opponent"] for row in rows], ["X"])
        row = rows[0]
        self.assertEqual(row["away"][0]["score"], "30-20")
        self.assertEqual(row["home"][0]["result"], "L")
        self.assertEqual(row["edge"], -3 - 10)           # home margin minus away margin

    def test_unplayed_games_the_meeting_itself_and_later_games_are_ignored(self):
        away = [game(1, "2026-09-05T16:00:00Z", A, X, None, None, completed=False),
                game(2, THIS, A, H, None, None, completed=False),
                game(5, "2026-10-17T16:00:00Z", A, Y, 30, 0)]
        home = [game(3, "2026-09-06T16:00:00Z", H, X, 24, 20), game(6, "2026-10-17T16:00:00Z", H, Y, 9, 3)]
        self.assertEqual(common_opponent_rows(away, home, away_id=1, home_id=2, before_date=THIS), [])

    def test_a_repeat_opponent_averages_the_margin(self):
        away = [game(1, "2026-09-05T16:00:00Z", A, X, 10, 0), game(2, "2026-09-19T16:00:00Z", A, X, 20, 0)]
        home = [game(3, "2026-09-06T16:00:00Z", H, X, 7, 0)]
        row = common_opponent_rows(away, home, away_id=1, home_id=2, before_date=THIS)[0]
        self.assertEqual(len(row["away"]), 2)
        self.assertEqual(row["away_margin"], 15)
        self.assertEqual(row["edge"], 7 - 15)



class CommonOpponentStatsTests(unittest.TestCase):
    def test_stats_are_attached_from_each_sides_point_of_view(self):
        away = [game(1, "2026-09-05T16:00:00Z", A, X, 30, 20)]
        home = [game(3, "2026-09-06T16:00:00Z", X, H, 27, 24)]
        stats = {
            (1, "A"): {"pass_yards": 300, "rush_yards": 100, "epa_per_play": 0.2, "success_rate": .5, "giveaways": 1},
            (1, "X"): {"pass_yards": 150, "rush_yards": 90, "epa_per_play": -0.1, "success_rate": .4, "giveaways": 2},
            (3, "H"): {"pass_yards": 200, "rush_yards": 120, "epa_per_play": 0.0, "success_rate": .45, "giveaways": 0},
            (3, "X"): {"pass_yards": 250, "rush_yards": 80, "epa_per_play": 0.1, "success_rate": .5, "giveaways": 1},
        }
        row = common_opponent_rows(away, home, away_id=1, home_id=2, before_date=THIS, stats=stats)[0]
        pass_line = next(l for l in row["away"][0]["stat_lines"] if l["label"] == "Pass yds")
        self.assertEqual((pass_line["for"], pass_line["against"]), (300, 150))
        home_pass = next(l for l in row["home"][0]["stat_lines"] if l["label"] == "Pass yds")
        self.assertEqual((home_pass["for"], home_pass["against"]), (200, 250))
        self.assertAlmostEqual(row["epa_edge"], (0.0 - 0.1) - (0.2 - -0.1))

    def test_a_game_without_charted_stats_still_shows_the_result(self):
        away = [game(1, "2026-09-05T16:00:00Z", A, X, 30, 20)]
        home = [game(3, "2026-09-06T16:00:00Z", X, H, 27, 24)]
        row = common_opponent_rows(away, home, away_id=1, home_id=2, before_date=THIS)[0]
        self.assertIsNone(row["away"][0]["stat_lines"])
        self.assertIsNone(row["epa_edge"])


if __name__ == "__main__":
    unittest.main()
