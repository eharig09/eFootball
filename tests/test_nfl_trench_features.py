"""Offensive line vs defensive front features (nfl.trench_features) and the analysis helpers (nfl.trench_analysis)."""
import sqlite3

import pandas as pd
import pytest

from sports_aggregator.nfl import trench_analysis as ta
from sports_aggregator.nfl import trench_features as tf
from sports_aggregator.nfl.naming import canon_team

HOME, AWAY = canon_team("DEN"), canon_team("KAN")


def frame(rows):
    base = {c: None for c in tf.PBP_COLUMNS}
    base.update(game_id="g1", week=1, season_type="REG", posteam="DEN", defteam="KAN")
    return pd.DataFrame([{**base, **r} for r in rows])


def test_pass_and_run_line_numbers_are_counted_per_offense():
    plays = [
        {"play_type": "pass", "qb_dropback": 1, "sack": 1, "qb_hit": 1, "epa": -2.0},       # a sack (its hit is not double counted)
        {"play_type": "pass", "qb_dropback": 1, "sack": 0, "qb_hit": 1, "epa": 0.5},        # a non-sack hit
        {"play_type": "pass", "qb_dropback": 1, "sack": 0, "qb_hit": 0, "epa": 1.5},        # clean
        {"play_type": "run", "qb_scramble": 0, "yards_gained": -1, "tackled_for_loss": 1, "epa": -0.8},   # stuffed, TFL
        {"play_type": "run", "qb_scramble": 0, "yards_gained": 0, "tackled_for_loss": 0, "epa": -0.3},    # stuffed, no TFL
        {"play_type": "run", "qb_scramble": 0, "yards_gained": 6, "tackled_for_loss": 0, "epa": 0.4},
        {"play_type": "run", "qb_scramble": 1, "yards_gained": 9, "tackled_for_loss": 0, "epa": 1.0},     # scramble: not a designed rush
    ]
    team = {t["team"]: t for t in tf.extract_season(frame(plays), 2020)}[HOME]
    assert (team["dropbacks"], team["sacks"], team["hits"]) == (3, 1, 1)
    assert team["pass_epa"] == pytest.approx(0.0)
    assert (team["rushes"], team["stuffs"], team["tfl"]) == (3, 2, 1)
    assert team["rush_epa"] == pytest.approx(-0.7)


def test_blend_is_own_offence_with_the_opponents_front():
    mine = {"off_sack": 0.08, "def_sack": 0.02}
    theirs = {"off_sack": 0.05, "def_sack": 0.10}
    assert tf.blend(mine, theirs, "sack") == pytest.approx((0.08 + 0.10) / 2)        # my line against THEIR front
    assert tf.blend(theirs, mine, "sack") == pytest.approx((0.05 + 0.02) / 2)


def test_snapshot_separates_a_line_from_the_front_that_faced_it_and_shrinks():
    record = dict.fromkeys(tf.KEYS, 0.0)
    record.update(season=2020, dropbacks=30.0, sacks=3.0, games=1.0)
    record.update({"opp_" + k: 0.0 for k in tf.KEYS})
    record.update(opp_dropbacks=40.0, opp_sacks=1.0, opp_games=1.0)         # the other offence was sacked once in 40
    league = dict.fromkeys(tf.KEYS, 0.0)
    league.update(dropbacks=35.0, sacks=2.5, games=1.0)
    snap = tf._snapshot([record], 2020, league)
    assert snap["off_sack"] == pytest.approx((3.0 + 3 * 2.5) / (30.0 + 3 * 35.0))
    assert snap["def_sack"] == pytest.approx((1.0 + 3 * 2.5) / (40.0 + 3 * 35.0))
    assert snap["off_sack"] > snap["def_sack"]
    assert tf._snapshot([], 2020, league)["off_sack"] == pytest.approx(2.5 / 35.0)          # no history: just the league mean


def test_attach_signs_differences_and_sums_and_skips_thin_pressure_samples():
    def snap(sack, charted):
        s = {f"off_{n}": 0.05 for n in tf.RATES}
        s.update({f"def_{n}": 0.05 for n in tf.RATES})
        s.update(off_sack=sack, charted=charted)
        return s
    snapshots = {"g": {"teams": (HOME, AWAY), "sides": {HOME: snap(0.10, 400.0), AWAY: snap(0.02, 400.0)}}}
    availability = {(2020, 3, HOME): {"ol_out": 1.5, "dl_out": 0.0}, (2020, 3, AWAY): {"ol_out": 0.25, "dl_out": 1.0}}
    row = {"game_id": "g", "season": 2020, "week": 3, "home_team": "DEN", "away_team": "KAN"}
    tf.attach([row], snapshots, availability)
    assert row["tr_sack_h"] == pytest.approx((0.10 + 0.05) / 2) and row["tr_sack_a"] == pytest.approx((0.02 + 0.05) / 2)
    assert row["tr_sack_diff"] == pytest.approx(row["tr_sack_h"] - row["tr_sack_a"])
    assert row["tr_sack_sum"] == pytest.approx(row["tr_sack_h"] + row["tr_sack_a"])
    assert row["tr_ol_out_diff"] == pytest.approx(0.25 - 1.5)        # home line missing more -> negative for the home team
    assert row["tr_dl_out_diff"] == pytest.approx(1.0 - 0.0)         # away front missing more -> positive for the home team
    assert "tr_press_diff" in row
    thin = {"g": {"teams": (HOME, AWAY), "sides": {HOME: snap(0.1, 400.0), AWAY: snap(0.1, tf.MIN_CHARTED_DROPBACKS - 1)}}}
    row2 = {"game_id": "g", "season": 2020, "week": 3, "home_team": "DEN", "away_team": "KAN"}
    tf.attach([row2], thin, {})
    assert "tr_press_diff" not in row2 and "tr_sack_diff" in row2        # pressure needs charted history; the rest does not


def test_line_availability_weights_status_by_how_much_the_player_played(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE nfl_injury_history (season INT, week INT, team TEXT, normalized_name TEXT, position TEXT, report_status TEXT)")
    connection.executemany("INSERT INTO nfl_injury_history VALUES (?,?,?,?,?,?)", [
        (2020, 5, HOME, "starter tackle", "T", "Out"), (2020, 5, HOME, "backup guard", "G", "Questionable"),
        (2020, 5, HOME, "star edge", "DE", "Doubtful"), (2020, 5, HOME, "quarterback", "QB", "Out"),
        (2020, 5, HOME, "healthy guard", "G", "Probable")])

    class Repo:
        def _connect(self):
            class C:
                def execute(s, *a): return connection.execute(*a)
                def close(s): pass
            return C()
    history = {"starter tackle": [(2020, 4, 1.0)], "backup guard": [(2020, 4, 0.2)], "star edge": [(2020, 4, 0.9)],
               "quarterback": [(2020, 4, 1.0)], "healthy guard": [(2020, 4, 1.0)]}
    monkeypatch.setattr(tf, "_snap_history", lambda *a, **k: history)
    out = tf.line_availability(Repo(), 2020, 2020)[(2020, 5, HOME)]
    assert out["ol_out"] == pytest.approx(1.0 * 1.0 + 0.2 * 0.2)              # Out x full-time starter, Questionable x backup
    assert out["dl_out"] == pytest.approx(0.85 * 0.9)                          # Doubtful x near-full-time edge
    # quarterbacks are not linemen and Probable carries no weight: neither appears


def test_blend_vs_one_sided_ranks_the_blend_when_both_sides_matter():
    snapshots, actuals = {}, {}
    rng = __import__("random").Random(1)
    for i in range(300):
        own, opp = rng.random(), rng.random()
        truth = (own + opp) / 2 + rng.gauss(0, 0.05)                 # reality depends on BOTH lines
        a, b = canon_team("DEN"), canon_team("KAN")
        side = lambda off, de: {**{f"off_{n}": 0.0 for n in tf.RATES}, **{f"def_{n}": 0.0 for n in tf.RATES},   # noqa: E731
                                "off_sack": off, "def_sack": de}
        snapshots[f"g{i}"] = {"teams": (a, b), "sides": {a: side(own, 0.5), b: side(0.5, opp)}}
        actuals[(f"g{i}", a)] = {"sacks": truth * 100, "dropbacks": 100.0, "hits": 0.0, "stuffs": 0.0, "rushes": 0.0,
                                 "pass_epa": 0.0, "rush_epa": 0.0}
    sack = ta.blend_vs_one_sided(snapshots, actuals)["sack_rate"]
    assert sack["corr_matchup_blend"] > sack["corr_own_offence_only"]
    assert sack["corr_matchup_blend"] > sack["corr_opposing_defence_only"]
