import unittest

from sports_aggregator.nfl.play_story import (
    biggest_swings, drive_chart, play_table, win_probability,
)


def _play(play_id, posteam, defteam, drive, result, home_wp, wpa=0.0, qtr=1, **extra):
    base = {"play_id": play_id, "posteam": posteam, "defteam": defteam, "drive": drive,
            "drive_result": result, "home_wp": home_wp, "wpa": wpa, "qtr": qtr, "clock": "10:00",
            "down": 1, "ydstogo": 10, "yard_line": "CHI 25", "description": f"play {play_id}",
            "is_pass": 1, "is_rush": 0, "yards_gained": 5, "epa": 0.1, "is_touchdown": 0, "is_turnover": 0}
    base.update(extra)
    return base


def _game():
    return [
        _play(1, "DAL", "PHI", 1, "Touchdown", 0.50),
        _play(2, "DAL", "PHI", 1, "Touchdown", 0.30, wpa=-0.20, yards_gained=40, is_touchdown=1),
        _play(3, "PHI", "DAL", 2, "Punt", 0.40, qtr=2),
        _play(4, "PHI", "DAL", 2, "Punt", 0.70, wpa=0.30, qtr=2, is_pass=0, is_rush=1, yards_gained=12),
        _play(5, "DAL", "PHI", 3, "Turnover", 0.60, qtr=3, is_turnover=1),
    ]


class WinProbabilityTests(unittest.TestCase):
    def test_line_is_split_at_fifty_percent_with_the_leader_colored(self):
        chart = win_probability(_game(), home="PHI", away="DAL")
        sides = [segment["side"] for segment in chart["segments"]]
        self.assertEqual(sides[0], "home")            # starts at exactly 50% -> home
        self.assertIn("away", sides)
        self.assertEqual(sides[-1], "home")
        for first, second in zip(chart["segments"], chart["segments"][1:]):
            self.assertNotEqual(first["side"], second["side"])
            # adjacent runs share the interpolated crossing point on the midline
            self.assertEqual(first["points"].split()[-1], second["points"].split()[0])

    def test_quarter_ticks_and_scoring_markers(self):
        chart = win_probability(_game(), home="PHI", away="DAL")
        self.assertEqual([q["label"] for q in chart["quarters"]], ["Q1", "Q2", "Q3"])
        kinds = [(m["kind"], m["team"]) for m in chart["markers"]]
        self.assertEqual(kinds, [("TD", "DAL")])

    def test_too_little_data_means_no_chart(self):
        self.assertFalse(win_probability([_play(1, "DAL", "PHI", 1, None, 0.5)], "PHI", "DAL")["has_data"])
        self.assertFalse(win_probability([_play(1, "DAL", "PHI", 1, None, None),
                                          _play(2, "DAL", "PHI", 1, None, None)], "PHI", "DAL")["has_data"])


class SwingTests(unittest.TestCase):
    def test_largest_absolute_swings_are_credited_to_the_team_that_gained(self):
        plays = _game()
        chart = win_probability(plays, "PHI", "DAL")
        swings = biggest_swings(plays, chart, limit=2)
        self.assertEqual([s["rank"] for s in swings], [1, 2])
        self.assertEqual((swings[0]["team"], swings[0]["pct"]), ("PHI", 30))   # offense gained
        self.assertEqual((swings[1]["team"], swings[1]["pct"]), ("PHI", 20))   # offense lost -> defense gained
        self.assertIn("x", swings[0])


class DriveChartTests(unittest.TestCase):
    def test_drives_group_by_team_with_results_and_totals(self):
        chart = drive_chart(_game(), away="DAL", home="PHI")
        dal, phi = chart["rows"]
        self.assertEqual([(d["label"], d["plays"], d["yards"]) for d in dal["drives"]],
                         [("TD", 2, 45), ("TO", 1, 5)])
        self.assertEqual(dal["totals"], {"td": 1, "fg": 0, "to": 1})
        self.assertEqual(phi["drives"][0]["label"], "Punt")


class PlayTableTests(unittest.TestCase):
    def test_kinds_and_context_tags(self):
        plays = _game()
        plays[3].update({"offense_group": "12", "offense_formation": "SHOTGUN", "motion": 1, "play_action": 1,
                         "defense_package": "Nickel", "box": "Heavy", "coverage": "COVER_3",
                         "man_zone": "ZONE_COVERAGE", "blitzers": 1, "was_pressure": 1})
        rows = play_table(plays)
        self.assertIn("scoring", rows[1]["kinds"].split())        # touchdown
        self.assertNotIn("scoring", rows[0]["kinds"].split())     # only the drive's final play is marked
        self.assertIn("explosive", rows[1]["kinds"].split())      # 40-yard pass
        self.assertIn("explosive", rows[3]["kinds"].split())      # 12-yard rush
        self.assertIn("turnover", rows[4]["kinds"].split())
        called, defense, hot = rows[3]["called"], rows[3]["defense"], rows[3]["hot"]
        self.assertEqual(called, ["12P", "Shotgun", "Play action", "Motion"])
        self.assertEqual(defense, ["Nickel", "Heavy box", "Cover 3 Zone"])
        self.assertEqual(hot, ["Blitz", "Pressure"])
        self.assertAlmostEqual(rows[3]["wpa"], 30.0)
        self.assertTrue(rows[3]["big_wpa"])

    def test_missing_enhanced_fields_yield_empty_tags(self):
        row = play_table(_game())[0]
        self.assertEqual((row["called"], row["defense"], row["hot"]), ([], [], []))


if __name__ == "__main__":
    unittest.main()
