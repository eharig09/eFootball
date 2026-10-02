"""The lean NFL model: the matchup blend, pregame snapshots, and the pick/score helpers."""
import pytest

from sports_aggregator.nfl import lean_model as lm
from sports_aggregator.nfl.naming import canon_team

HOME, AWAY = canon_team("DEN"), canon_team("KAN")


def _rates(off, de, give, take, committed, drawn, plays, allowed):
    return {"off_succ": off, "def_succ": de, "giveaway": give, "takeaway": take,
            "pen_committed": committed, "pen_drawn": drawn, "plays_pg": plays, "plays_allowed_pg": allowed}


def _attach_one(home, away):
    row = {"game_id": "g", "home_team": "DEN", "away_team": "KAN"}
    lm._attach([row], {"g": {HOME: home, AWAY: away}}, {}, {})
    return row


def test_blend_is_own_offense_averaged_with_the_opponents_defense_not_the_teams_own_side():
    home = _rates(0.50, 0.40, 0.010, 0.030, 60.0, 40.0, 65.0, 61.0)
    away = _rates(0.30, 0.45, 0.020, 0.010, 50.0, 70.0, 60.0, 64.0)
    r = _attach_one(home, away)
    assert r["bl_succ_h"] == pytest.approx((0.50 + 0.45) / 2)          # home offense with AWAY defense allowed
    assert r["bl_succ_a"] == pytest.approx((0.30 + 0.40) / 2)          # away offense with HOME defense allowed
    assert r["bl_succ_diff"] == pytest.approx(0.125)
    assert r["bl_succ_diff"] != pytest.approx(0.50 - 0.30)             # NOT the two offenses alone
    assert r["bl_succ_sum"] == pytest.approx(0.475 + 0.35)
    # turnovers: a side's giveaway rate averaged with the other side's takeaway rate
    to_h, to_a = (0.010 + 0.010) / 2, (0.020 + 0.030) / 2
    assert r["bl_to_sum"] == pytest.approx(to_h + to_a) and r["bl_to_diff"] == pytest.approx(to_a - to_h)
    # penalties: yards a side commits averaged with yards the other side's opponents drew
    pen_h, pen_a = (60.0 + 70.0) / 2, (50.0 + 40.0) / 2
    assert r["bl_pen_sum"] == pytest.approx(pen_h + pen_a) and r["bl_pen_diff"] == pytest.approx(pen_a - pen_h)
    # pace: own plays per game averaged with the plays the opponent allows
    assert r["bl_plays_sum"] == pytest.approx((65.0 + 64.0) / 2 + (60.0 + 61.0) / 2)


def test_a_great_defense_lowers_the_opponents_expectation_even_with_an_identical_offense():
    offense = dict(off=0.45, give=0.01, take=0.01)
    stingy = _rates(0.45, 0.30, 0.01, 0.01, 50.0, 50.0, 62.0, 62.0)
    generous = _rates(0.45, 0.55, 0.01, 0.01, 50.0, 50.0, 62.0, 62.0)
    attacker = _rates(offense["off"], 0.45, offense["give"], offense["take"], 50.0, 50.0, 62.0, 62.0)
    vs_stingy = _attach_one(attacker, stingy)["bl_succ_h"]
    vs_generous = _attach_one(attacker, generous)["bl_succ_h"]
    assert vs_stingy < vs_generous


def test_games_without_a_snapshot_for_both_teams_are_left_unfeatured():
    row = {"game_id": "g", "home_team": "DEN", "away_team": "KAN"}
    lm._attach([row], {"g": {HOME: _rates(.4, .4, .01, .01, 50, 50, 60, 60)}}, {}, {})
    assert "bl_succ_diff" not in row


def test_snapshots_use_only_earlier_weeks_and_shrink_to_the_league_mean(monkeypatch):
    def rec(gid, week, team, opp, succ):
        return {"game_id": gid, "team": team, "opponent": opp, "season": 2020, "week": week, "plays": 60.0, "succ": succ,
                "opp_plays": 60.0, "opp_succ": 30.0, "giveaways": 1.0, "off_snaps": 60.0, "takeaways": 1.0,
                "def_snaps": 60.0, "pen_committed": 50.0, "pen_drawn": 50.0}
    records = [rec("w1a", 1, "DEN", "KAN", 40.0), rec("w1a", 1, "KAN", "DEN", 20.0),
               rec("w2a", 2, "DEN", "KAN", 30.0), rec("w2a", 2, "KAN", "DEN", 30.0)]
    monkeypatch.setattr(lm, "_records", lambda *args, **kwargs: records)
    blends = lm.blended_features(None, 2020, 2020)
    assert "w1a" not in blends                                   # week 1: no history for anyone
    snap = blends["w2a"]
    den = snap["DEN"]
    assert den["off_succ"] > 20 / 60                             # DEN's strong week 1 is visible...
    assert den["off_succ"] < 40 / 60                             # ...but shrunk toward the league mean, not taken raw
    assert den["def_succ"] == pytest.approx((30 + lm.PRIOR_GAMES * 30.0) / (60 + lm.PRIOR_GAMES * 60.0), rel=1e-6)


def test_pick_scoring_pushes_edges_and_side_split():
    rows = [{"p": 50.0, "line": 45.0, "y": 52.0}, {"p": 50.0, "line": 45.0, "y": 40.0},
            {"p": 40.0, "line": 45.0, "y": 41.0}, {"p": 40.0, "line": 45.0, "y": 45.0},   # push: dropped
            {"p": 45.5, "line": 45.0, "y": 50.0}]                                         # edge under the 3-pt floor
    all_picks = lm._picks(rows, "p", "line", "y")
    assert (all_picks["n"], all_picks["win_rate"]) == (4, 0.75)      # over, over-miss, under, over: 3 of 4 (push dropped)
    assert lm._picks(rows, "p", "line", "y", 3.0)["n"] == 3
    split = lm._side_split(rows, "p", "line", "y")
    assert split["over_picks"] == {"n": 3, "win_rate": round(2 / 3, 4)} and split["under_picks"]["n"] == 1
    assert split["base_rate_under"] == pytest.approx(2 / 4)


def test_straight_up_accuracy_ignores_ties_and_zero_forecasts():
    rows = [{"actual_margin": 7.0, "p": 3.0}, {"actual_margin": -3.0, "p": 2.0},
            {"actual_margin": 0.0, "p": 5.0}, {"actual_margin": 4.0, "p": 0}]
    assert lm._su(rows, "p") == {"n": 2, "accuracy": 0.5}


def test_walk_forward_scores_substituted_features_without_refitting():
    sample = [{"game_id": f"{s}-{i}", "season": s, "x": float(i % 5), "wx": 0.0, "y": float(i % 5)}
              for s in (2018, 2019, 2020) for i in range(120)]
    stages = (("one", ("x", "wx")),)
    fit = lambda train, feats: {"feats": feats}                 # noqa: E731  (a stand-in model object)
    predict = lambda model, row: row["x"] + 10 * row["wx"]      # noqa: E731
    alt = {"2020-3": {"wx": 1.0}}
    pooled, folds = lm._walk(sample, stages, fit, predict, alt=alt)
    swapped = next(r for r in pooled if r["game_id"] == "2020-3")
    assert swapped["pred_one__alt"] == swapped["pred_one"] + 10.0
    assert all("pred_one__alt" not in r for r in pooled if r["game_id"] != "2020-3")
    assert len(folds) == 3 and {int(r["season"]) for r in pooled} == {2018, 2019, 2020}
