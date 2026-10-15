"""The NFL offense-versus-defense card groups rows by category, whatever their source."""

import unittest

from sports_aggregator.nfl.defense import MATCHUP_SPECS, matchup_defense_cards


def _teams(keys, count=12):
    return [{"team": f"T{index}", **{key: float(index) for key in keys}} for index in range(count)]


class _Repository:
    def __init__(self, traditional, efficiency, ngs):
        self._traditional, self._efficiency, self._ngs = traditional, efficiency, ngs

    def league_team_summary(self, season, before_week=None):
        return self._traditional

    def league_defensive_summary(self, season, before_week=None):
        return self._traditional

    def league_efficiency(self, season, before_week=None):
        return self._efficiency

    def league_ngs_summary(self, season, before_week=None, defense=False):
        return self._ngs


class _Pff:
    def __init__(self, rows):
        self.rows = rows

    def league_unit_grades(self, season):
        return self.rows


def _keys(pool):
    return {key for _label, _source, specs in MATCHUP_SPECS for spec in specs
            if spec[6] == pool for key in (spec[1], spec[2])}


class MatchupCategoryTests(unittest.TestCase):
    def cards(self):
        repository = _Repository(_teams(_keys("traditional")), _teams(_keys("efficiency")),
                                 _teams(_keys("ngs")))
        return matchup_defense_cards(repository, _Pff(_teams(_keys("pff"))), 2026, 8, "T0", "T9",
                                     baseline_season=2026, pff_season=2026)

    def test_categories_come_in_reading_order(self):
        for card in self.cards():
            self.assertEqual([section["label"] for section in card["sections"]],
                             ["Points", "Efficiency", "Passing", "Rushing", "Mistakes"])

    def test_one_category_mixes_box_score_ngs_and_pff_rows(self):
        passing = next(section for section in self.cards()[0]["sections"] if section["label"] == "Passing")
        labels = [row["label"] for row in passing["rows"]]
        self.assertIn("Passing yards / game", labels)       # box score
        self.assertIn("CPOE", labels)                       # Next Gen Stats
        self.assertIn("Routes vs coverage", labels)         # PFF

    def test_every_row_appears_once(self):
        labels = [row["label"] for section in self.cards()[0]["sections"] for row in section["rows"]]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(len(labels), sum(len(specs) for _l, _s, specs in MATCHUP_SPECS))

    def test_a_row_missing_from_one_pool_is_dropped_not_the_category(self):
        repository = _Repository(_teams(_keys("traditional")), _teams(_keys("efficiency")), [])
        cards = matchup_defense_cards(repository, _Pff([]), 2026, 8, "T0", "T9",
                                      baseline_season=2026, pff_season=None)
        passing = next(section for section in cards[0]["sections"] if section["label"] == "Passing")
        labels = [row["label"] for row in passing["rows"]]
        self.assertNotIn("CPOE", labels)
        self.assertNotIn("Routes vs coverage", labels)
        self.assertIn("Dropback EPA", labels)


if __name__ == "__main__":
    unittest.main()
