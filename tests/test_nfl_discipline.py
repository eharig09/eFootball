import tempfile
import unittest
from pathlib import Path

from sports_aggregator.nfl import penalties, situational_tendencies as tendencies
from sports_aggregator.nfl.plays import PENALTY_COLUMNS, build_penalty_rows
from sports_aggregator.nfl.repository import NFLRepository


def _play(play_id, team, posteam, defteam, kind, yards, *, play_type="no_play", epa=-1.0, first=0, player="P.One", **extra):
    return {"game_id": "2026_01_AAA_BBB", "play_id": play_id, "season": 2026, "week": 1, "season_type": "REG",
            "penalty": 1, "penalty_team": team, "posteam": posteam, "defteam": defteam, "penalty_type": kind,
            "penalty_yards": yards, "play_type": play_type, "epa": epa, "first_down_penalty": first,
            "penalty_player_id": "00-1", "penalty_player_name": player, "down": 1, "ydstogo": 10, "qtr": 1, **extra}


class PenaltyRowTests(unittest.TestCase):
    def test_declined_and_unattributable_flags_are_dropped(self):
        rows = build_penalty_rows([
            _play(1, "AAA", "AAA", "BBB", "False Start", 5),
            _play(2, "AAA", "AAA", "BBB", "Holding", 0),                 # declined: no yardage
            _play(3, "ZZZ", "AAA", "BBB", "Holding", 10),                # flagged team is not in the game
            {**_play(4, "AAA", "AAA", "BBB", "Holding", 10), "penalty": 0},
        ])
        self.assertEqual(len(rows), 1)
        row = dict(zip(PENALTY_COLUMNS, rows[0]))
        self.assertEqual((row["team"], row["opponent"], row["penalty_type"], row["yards"]), ("AAA", "BBB", "False Start", 5))

    def test_epa_is_from_the_flagged_teams_side_and_only_on_no_play_flags(self):
        rows = [dict(zip(PENALTY_COLUMNS, row)) for row in build_penalty_rows([
            _play(1, "AAA", "AAA", "BBB", "False Start", 5, epa=-0.8),                   # offense flagged: its own epa
            _play(2, "BBB", "AAA", "BBB", "Defensive Holding", 5, epa=1.0, first=1),     # defense flagged: opposite sign
            _play(3, "BBB", "AAA", "BBB", "Defensive Holding", 5, play_type="pass", epa=2.0),
        ])]
        self.assertEqual([round(row["epa_team"], 2) if row["epa_team"] is not None else None for row in rows], [-0.8, -1.0, None])
        self.assertEqual(rows[1]["auto_first_down"], 1)


class PenaltyProfileTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.repository = NFLRepository(Path(self._directory.name) / "nfl.sqlite3")
        self.repository.initialize()
        plays = [_play(1, "AAA", "AAA", "BBB", "False Start", 5), _play(2, "AAA", "AAA", "BBB", "False Start", 5, player="P.Two"),
                 _play(3, "AAA", "AAA", "BBB", "Offensive Holding", 10), _play(4, "BBB", "BBB", "AAA", "Delay of Game", 5)]
        self.repository.replace_penalties(2026, build_penalty_rows(plays))

    def tearDown(self):
        self._directory.cleanup()

    def test_committed_and_drawn_are_the_same_flags_seen_from_both_benches(self):
        league = penalties._build(self.repository, 2026, None)
        aaa, bbb = league["AAA"], league["BBB"]
        self.assertEqual((aaa["committed"]["flags"], aaa["drawn"]["flags"]), (3, 1))
        self.assertEqual((bbb["committed"]["flags"], bbb["drawn"]["flags"]), (1, 3))
        self.assertEqual(aaa["committed"]["yards"], bbb["drawn"]["yards"])
        self.assertEqual(aaa["committed"]["presnap"], 2)                 # two false starts
        self.assertEqual(aaa["committed"]["types"][0]["type"], "False Start")
        self.assertEqual(aaa["net_yards"], -(bbb["net_yards"]))

    def test_ranks_put_the_best_team_first(self):
        league = penalties._build(self.repository, 2026, None)
        self.assertEqual(league["BBB"]["ranks"]["committed"]["flags"], 1)    # fewer flags committed is better
        self.assertEqual(league["BBB"]["ranks"]["drawn"]["flags"], 1)        # more flags drawn is better

    def test_before_week_excludes_later_flags(self):
        self.assertEqual(penalties._build(self.repository, 2026, 1), {})


class BandTests(unittest.TestCase):
    def test_first_down_bands_follow_goal_to_go_and_ten_yards(self):
        self.assertEqual(tendencies.band(1, 5, 1), "short")
        self.assertEqual(tendencies.band(1, 10, 0), "medium")
        self.assertEqual(tendencies.band(1, 15, 0), "long")

    def test_later_down_bands(self):
        self.assertEqual([tendencies.band(3, yards, 0) for yards in (1, 3, 4, 6, 7, 15)],
                         ["short", "short", "medium", "medium", "long", "long"])
        self.assertEqual([tendencies.band(4, yards, 0) for yards in (1, 2, 3, 5, 6)],
                         ["short", "short", "medium", "medium", "long"])


if __name__ == "__main__":
    unittest.main()
