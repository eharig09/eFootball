"""The market-free total anchor: snapshots, the pull/shift rule, and agreement between dataset and live paths."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import numpy as np

from sports_aggregator.cfb import matchup_research as mr
from sports_aggregator.cfb import total_anchor as ta
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas


def item(pf=30.0, pa=20.0, epa=0.1, opp_epa=0.0, plays=70.0, opp_fbs=True):
    return {"pf": pf, "pa": pa, "epa": epa, "opp_epa": opp_epa, "plays": plays, "opp_fbs": opp_fbs}


class SnapshotTests(unittest.TestCase):
    def test_a_team_needs_enough_games_against_fbs_opponents(self):
        history = [item() for _ in range(3)] + [item(opp_fbs=False) for _ in range(5)]
        self.assertIsNone(ta.snapshot(history))                    # FCS games do not count toward the minimum
        self.assertEqual(ta.snapshot(history + [item()])["n"], 4)

    def test_fcs_blowouts_do_not_inflate_the_scoring_history(self):
        history = [item(pf=30, pa=20) for _ in range(5)] + [item(pf=70, pa=0, opp_fbs=False)]
        self.assertAlmostEqual(ta.snapshot(history)["pf"], 30.0)

    def test_recent_games_weigh_more(self):
        history = [item(pf=10) for _ in range(4)] + [item(pf=40) for _ in range(4)]
        self.assertGreater(ta.snapshot(history)["pf"], 25.0)


class TendencyTests(unittest.TestCase):
    def test_shrunk_toward_zero_and_signed(self):
        over = ta._tendency([6.0] * 12)
        self.assertGreater(over, 0)
        self.assertLess(over, 6.0)
        self.assertLess(ta._tendency([-6.0] * 12), 0)
        self.assertEqual(ta._tendency([]), 0.0)
        self.assertLess(ta._tendency([6.0] * 2), ta._tendency([6.0] * 12))     # less evidence, less pull


def anchor(total=56.0, o_edge=0.0, d_edge=0.0):
    return {"total": total, "o_edge": o_edge, "d_edge": d_edge}


class AdjustTests(unittest.TestCase):
    def test_the_anchor_only_pulls_and_never_concludes(self):
        out = ta.adjust(60.0, 3.0, anchor(total=50.0))
        self.assertAlmostEqual(out["total"], 55.0)                 # half of the 10-point gap
        self.assertGreater(out["total"], 50.0)                     # never all the way to the anchor
        self.assertAlmostEqual(ta.adjust(60.0, 3.0, anchor(total=60.0))["total"], 60.0)

    def test_without_a_quality_gap_the_change_is_shared_evenly(self):
        out = ta.adjust(60.0, 3.0, anchor(total=50.0))
        self.assertAlmostEqual(out["margin"], 3.0)

    def test_when_the_total_goes_up_the_better_offence_picks_up_points(self):
        home_better = ta.adjust(50.0, 0.0, anchor(total=60.0, o_edge=0.14))
        away_better = ta.adjust(50.0, 0.0, anchor(total=60.0, o_edge=-0.14))
        self.assertGreater(home_better["margin"], 0.0)             # home offence better -> margin moves to home
        self.assertLess(away_better["margin"], 0.0)

    def test_when_the_total_goes_down_the_better_defence_gives_up_fewer(self):
        home_better = ta.adjust(60.0, 0.0, anchor(total=50.0, d_edge=0.12))
        away_better = ta.adjust(60.0, 0.0, anchor(total=50.0, d_edge=-0.12))
        self.assertGreater(home_better["margin"], 0.0)             # home defence better -> home loses less
        self.assertLess(away_better["margin"], 0.0)

    def test_the_wrong_side_of_quality_does_not_steer_a_move(self):
        """A total going down is steered by DEFENCE quality only, and going up by OFFENCE only."""
        self.assertAlmostEqual(ta.adjust(60.0, 0.0, anchor(total=50.0, o_edge=0.14))["margin"], 0.0)
        self.assertAlmostEqual(ta.adjust(50.0, 0.0, anchor(total=60.0, d_edge=0.12))["margin"], 0.0)

    def test_a_bigger_quality_gap_moves_the_spread_proportionally_more_up_to_a_cap(self):
        small = ta.adjust(50.0, 0.0, anchor(total=60.0, o_edge=0.03))["margin"]
        large = ta.adjust(50.0, 0.0, anchor(total=60.0, o_edge=0.10))["margin"]
        capped = ta.adjust(50.0, 0.0, anchor(total=60.0, o_edge=5.0))["margin"]
        self.assertTrue(0 < small < large <= capped)
        self.assertAlmostEqual(capped, 2 * ta.SHIFT_KAPPA * 5.0)   # saturates at one standard deviation of edge

    def test_no_side_ever_loses_points_when_the_total_rises(self):
        out = ta.adjust(50.0, 0.0, anchor(total=60.0, o_edge=5.0))
        home, away = (out["total"] + out["margin"]) / 2, (out["total"] - out["margin"]) / 2
        self.assertGreater(home, 25.0)
        self.assertGreater(away, 25.0)


class EngineScoreWiringTests(unittest.TestCase):
    def _score(self, anchor_value):
        game = {"season": 2026, "game_id": 1, "home_team": "H", "away_team": "A"}
        projection = {"home": {"expected_points": 31.0}, "away": {"expected_points": 29.0}}
        with patch.object(mr, "predict_live_margin", return_value={"value": 4.0, "variant": "plus_elo"}), \
             patch.object(mr, "_calibrated_live_total", return_value={"value": 60.0}), \
             patch.object(mr.total_anchor, "for_game", return_value=anchor_value):
            return mr.engine_display_score(None, game, projection)

    def test_the_displayed_projection_is_pulled_but_the_research_inputs_are_not(self):
        out = self._score({**anchor(total=50.0, o_edge=0.0, d_edge=0.0), "season_midpoint_total": 50.0,
                           "opponent_adjusted_total": 50.0})
        self.assertAlmostEqual(out["total"], 55.0)
        self.assertAlmostEqual(out["home"] + out["away"], out["total"])
        self.assertAlmostEqual(out["home"] - out["away"], out["margin"])
        self.assertEqual(out["pre_anchor"]["total"], 60.0)         # what the validated regimes keep reading
        self.assertEqual(out["pre_anchor"]["margin"], 4.0)
        self.assertAlmostEqual(out["anchor"]["change"], -5.0)

    def test_without_an_anchor_the_projection_is_unchanged(self):
        out = self._score(None)
        self.assertEqual((out["total"], out["margin"]), (60.0, 4.0))
        self.assertIsNone(out["anchor"])

    def test_a_failing_anchor_never_takes_the_projection_down(self):
        game = {"season": 2026, "game_id": 1, "home_team": "H", "away_team": "A"}
        projection = {"home": {"expected_points": 31.0}, "away": {"expected_points": 29.0}}
        with patch.object(mr, "predict_live_margin", return_value={"value": 4.0, "variant": "plus_elo"}), \
             patch.object(mr, "_calibrated_live_total", return_value={"value": 60.0}), \
             patch.object(mr.total_anchor, "for_game", side_effect=RuntimeError("boom")):
            out = mr.engine_display_score(None, game, projection)
        self.assertEqual(out["total"], 60.0)


class DatasetMatchesLiveTests(unittest.TestCase):
    """The dataset (built in one chronological pass) and the live snapshot (a query) must agree exactly."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        ta.initialize(self.repository)

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def game(self, game_id, season, day, home, away, hp, ap, away_conf="SEC"):
        stamp = f"{season}-09-{day:02d}T16:00:00Z"
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("""INSERT INTO games(game_id,season,week,season_type,start_date,start_time_tbd,completed,
                         neutral_site,conference_game,home_team_id,home_team,home_points,home_conference,
                         away_team_id,away_team,away_points,away_conference,updated_at)
                         VALUES(?,?,1,'regular',?,0,1,0,0,1,?,?,?,2,?,?,?,?)""",
                      (game_id, season, stamp, home, hp, "SEC", away, ap, away_conf, stamp))
            for team, opponent, epa, plays in ((home, away, 0.10 + game_id / 100, 70), (away, home, 0.05, 66)):
                c.execute("""INSERT INTO cfb_team_game_advanced(game_id,team,opponent,model_version,metric_version,
                             scrimmage_plays,rush_plays,pass_plays,epa_per_play,built_at)
                             VALUES(?,?,?,'ep-v2','team-game-advanced-v1',?,30,40,?,?)""",
                          (game_id, team, opponent, plays, epa, stamp))
            c.commit()

    def test_the_dataset_snapshot_equals_the_live_snapshot(self):
        for i in range(1, 8):
            opponent_conf = "Southland" if i == 3 else "SEC"        # one FCS opponent in the middle
            self.game(i, 2023, i, "Alpha", f"Opp{i}", 20 + i, 10 + i, away_conf=opponent_conf)
        self.game(8, 2023, 20, "Alpha", "Opp8", 31, 17)
        ta.build_dataset(self.repository, from_season=2017)
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            stored = dict(c.execute(
                "SELECT * FROM cfb_total_anchor_dataset WHERE game_id=8 AND team='Alpha'").fetchone())
        live = ta.live_snapshot(self.repository, "Alpha", "2023-09-20T16:00:00Z")
        self.assertIsNotNone(live)
        self.assertEqual(stored["n_prior"], live["n"])
        self.assertEqual(live["n"], 6)                                # seven prior games, one against an FCS team
        for key in ("pf", "pa", "off_epa", "def_epa", "plays"):
            self.assertAlmostEqual(stored[key], live[key], places=9, msg=key)

    def test_history_before_the_dataset_start_is_ignored_by_both(self):
        self.game(1, 2015, 1, "Alpha", "Old", 40, 10)                  # before HISTORY_FROM_SEASON
        for i in range(2, 7):
            self.game(i, 2023, i, "Alpha", f"Opp{i}", 20 + i, 10 + i)
        live = ta.live_snapshot(self.repository, "Alpha", "2023-09-30T16:00:00Z")
        self.assertEqual(live["n"], 5)


if __name__ == "__main__":
    unittest.main()
