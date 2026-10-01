import unittest

from sports_aggregator.cfb.identity_links import (
    draft_pick_belongs, position_group, positions_compatible, transfer_belongs,
)


class PositionTests(unittest.TestCase):
    def test_groups_and_neighbours(self):
        self.assertEqual((position_group("S"), position_group("CB"), position_group("WR"), position_group("xx")), ("DB", "DB", "REC", None))
        self.assertTrue(positions_compatible("S", "CB"))
        self.assertTrue(positions_compatible("WR", "RB"))       # players move between these
        self.assertTrue(positions_compatible("DE", "LB"))
        self.assertTrue(positions_compatible(None, "QB"))        # unknown is not a contradiction
        self.assertFalse(positions_compatible("S", "OL"))
        self.assertFalse(positions_compatible("QB", "DB"))


class TransferTests(unittest.TestCase):
    #  The Ohio State receiver and the Georgia Tech safety share the name "Jeremiah Smith".
    OSU_WR = [{"season": 2024, "team": "Ohio State", "position": "WR"}, {"season": 2025, "team": "Ohio State", "position": "WR"}]
    GT_DB = [{"season": 2019, "team": "Georgia Tech", "position": "DB"}, {"season": 2020, "team": "Georgia Tech", "position": "DB"}]
    PORTAL_ENTRY = {"season": 2023, "origin": "Georgia Tech", "destination": None, "position": "S"}

    def test_an_entry_for_a_namesake_does_not_attach_to_the_wrong_player(self):
        self.assertFalse(transfer_belongs(self.PORTAL_ENTRY, self.OSU_WR))
        self.assertTrue(transfer_belongs(self.PORTAL_ENTRY, self.GT_DB))

    def test_destination_stint_after_the_portal_season_is_evidence(self):
        entry = {"season": 2024, "origin": "Rice", "destination": "Ohio State", "position": "WR"}
        self.assertTrue(transfer_belongs(entry, self.OSU_WR))

    def test_a_stint_that_predates_the_destination_is_not(self):
        entry = {"season": 2026, "origin": "Rice", "destination": "Ohio State", "position": "WR"}
        self.assertFalse(transfer_belongs(entry, [{"season": 2019, "team": "Ohio State", "position": "WR"}]))

    def test_a_player_with_no_stints_cannot_be_contradicted(self):
        self.assertTrue(transfer_belongs(self.PORTAL_ENTRY, []))


class DraftTests(unittest.TestCase):
    def test_an_athlete_id_decides_when_present(self):
        self.assertTrue(draft_pick_belongs({"college_athlete_id": "7"}, "7", []))
        self.assertFalse(draft_pick_belongs({"college_athlete_id": "8", "college_team": "Ohio State", "draft_year": 2026}, "7",
                                            [{"season": 2024, "team": "Ohio State"}]))

    def test_without_an_id_the_college_must_match_his_stints(self):
        stints = [{"season": 2023, "team": "Ohio State"}]
        self.assertTrue(draft_pick_belongs({"college_athlete_id": None, "college_team": "Ohio State", "draft_year": 2024}, "7", stints))
        self.assertFalse(draft_pick_belongs({"college_athlete_id": None, "college_team": "Georgia Tech", "draft_year": 2024}, "7", stints))


if __name__ == "__main__":
    unittest.main()
