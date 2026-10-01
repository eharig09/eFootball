import unittest

from sports_aggregator.cfb import player_panels
from sports_aggregator.cfb.game_panels import player_matchup_panels, unit_grade_rows, unit_matchup_panels
from sports_aggregator.cfb.matchups import game_matchup_report


def _unit(label, away_attack, away_counter, home_attack, home_counter):
    side = lambda attack, counter: {"attack_label": f"{label} offense", "attack": {"grade": attack, "players": 6, "usage": 400.0},
                                    "counter_label": f"{label} defense", "counter": {"grade": counter, "players": 20, "usage": 2000.0},
                                    "edge": {"side": "OFFENSE", "margin": abs(attack - counter)}}
    return {"label": label, "away_attacks": side(away_attack, away_counter), "home_attacks": side(home_attack, home_counter)}


class UnitPanelTests(unittest.TestCase):
    def setUp(self):
        self.report = game_matchup_report([_unit("Rushing", 77.8, 67.0, 82.5, 74.9), _unit("Passing", 61.0, 58.0, 59.0, 66.0)], "Pitt", "VT")
        self.identities = {"Pitt": {"color": "#003594", "logo_url": "/pitt.png"}, "VT": {"color": "#861f41", "logo_url": None}}

    def test_one_panel_per_attacking_team_with_signed_gap_and_side(self):
        panels = unit_matchup_panels(self.report, "Pitt", "VT", self.identities)
        self.assertEqual([panel["team"] for panel in panels], ["Pitt", "VT"])
        pitt = {row["unit"]: row for row in panels[0]["rows"]}
        self.assertEqual(pitt["Rushing"]["side"], "attack")
        self.assertAlmostEqual(pitt["Rushing"]["gap"], 10.8, places=1)
        vt = {row["unit"]: row for row in panels[1]["rows"]}
        self.assertEqual(vt["Passing"]["side"], "defend")                    # 59.0 attacking a 66.0 defense
        self.assertLessEqual(max(row["bar"] for panel in panels for row in panel["rows"]), 100)

    def test_rows_follow_the_fixed_unit_order(self):
        order = [row["unit"] for row in unit_matchup_panels(self.report, "Pitt", "VT", self.identities)[0]["rows"]]
        self.assertEqual(order, ["Rushing", "Passing"])

    def test_unit_grades_mark_the_better_side(self):
        rows = unit_grade_rows([{"label": "Coverage", "away_grade": 64.8, "home_grade": 62.1, "away_returning_share": .455, "home_returning_share": .782}])
        self.assertEqual((rows[0]["lead"], rows[0]["away"]["returning"]), ("away", 45.5))


class PlayerPanelGroupingTests(unittest.TestCase):
    def test_pairings_are_grouped_by_the_attackers_school_best_first(self):
        item = lambda school, name, interest: {"label": "Receiver vs coverage unit", "why": "x", "interest": interest,
                                               "attacker": {"school": school, "player_name": name, "position": "WR", "cfbd_player_id": name},
                                               "defender": {"player_name": "Secondary", "members": [{"player_name": "A", "grade": 80.0}]}}
        panels = player_matchup_panels([item("Pitt", "Low", 10), item("VT", "Mid", 20), item("Pitt", "High", 40)], "Pitt", "VT", 2026,
                                       lambda pid, season: f"/p/{pid}", {})
        self.assertEqual([row["player"] for row in panels[0]["rows"]], ["High", "Low"])
        self.assertEqual(panels[1]["rows"][0]["player_url"], "/p/Mid")
        self.assertEqual(panels[0]["rows"][0]["against_detail"], "A 80.0")


class QualifyingTests(unittest.TestCase):
    def test_every_position_group_has_a_label(self):
        from sports_aggregator.cfb.identity_links import POSITION_GROUP
        for group in set(POSITION_GROUP.values()):
            self.assertIn(group, player_panels.GROUP_LABELS)


if __name__ == "__main__":
    unittest.main()
