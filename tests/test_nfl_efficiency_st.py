"""Drive-efficiency and special-teams extraction, kicker ratings and pregame snapshots (nfl.efficiency_st)."""
import pandas as pd
import pytest

from sports_aggregator.nfl import efficiency_st as st
from sports_aggregator.nfl.naming import canon_team

HOME, AWAY = canon_team("DEN"), canon_team("KAN")


def frame(rows):
    base = {c: None for c in st.PBP_COLUMNS}
    base.update(game_id="g1", week=1, season_type="REG")
    return pd.DataFrame([{**base, **r} for r in rows])


def play(kind, team="DEN", opp="KAN", **kw):
    return {"play_type": kind, "posteam": team, "defteam": opp, **kw}


def test_drive_components_count_touchdowns_three_and_outs_red_zone_and_field_position():
    plays = [
        # drive 1: three plays then a punt (a three-and-out), starting at the opponent's 75
        play("run", fixed_drive=1, fixed_drive_result="Punt", drive_play_count=3, yardline_100=75),
        play("pass", fixed_drive=1, fixed_drive_result="Punt", drive_play_count=3, yardline_100=72),
        play("pass", fixed_drive=1, fixed_drive_result="Punt", drive_play_count=3, yardline_100=70),
        # drive 2: reaches the red zone and scores a touchdown, starting at 40
        play("run", fixed_drive=2, fixed_drive_result="Touchdown", drive_play_count=6, yardline_100=40),
        play("pass", fixed_drive=2, fixed_drive_result="Touchdown", drive_play_count=6, yardline_100=15),
        # drive 3: a kneel-down at the end of the half does not count as a drive
        play("run", fixed_drive=3, fixed_drive_result="End of half", drive_play_count=1, yardline_100=30),
    ]
    team = {t["team"]: t for t in st.extract_season(frame(plays), 2020)["teams"]}[HOME]
    assert (team["drives"], team["tds"], team["three_outs"]) == (2, 1, 1)
    assert (team["rz_trips"], team["rz_tds"]) == (1, 1)
    assert team["start_sum"] == 75 + 40


def test_net_punt_charges_touchbacks_and_credits_the_returner_and_skips_blocks():
    plays = [play("punt", kick_distance=50, return_yards=10, touchback=0, punt_blocked=0),     # net 40
             play("punt", kick_distance=55, return_yards=0, touchback=1, punt_blocked=0),      # net 35 (touchback = 20)
             play("punt", kick_distance=12, return_yards=0, touchback=0, punt_blocked=1)]      # blocked: ignored
    teams = {t["team"]: t for t in st.extract_season(frame(plays), 2020)["teams"]}
    assert (teams[HOME]["punts"], teams[HOME]["punt_net_sum"]) == (2, 75.0)
    assert (teams[AWAY]["recv_n"], teams[AWAY]["recv_ret_sum"]) == (2, 10.0)         # the other side returned them


def test_kicks_and_field_goal_events():
    plays = [play("kickoff", touchback=1), play("kickoff", touchback=0),
             play("field_goal", kick_distance=52, kicker_player_id="K1", field_goal_result="made"),
             play("field_goal", kick_distance=33, kicker_player_id="K1", field_goal_result="missed")]
    out = st.extract_season(frame(plays), 2020)
    home = {t["team"]: t for t in out["teams"]}[HOME]
    assert (home["kos"], home["ko_tb"], home["fg_att"]) == (2, 1, 2)
    assert [(e["distance"], e["made"]) for e in out["fg_events"]] == [(52.0, 1.0), (33.0, 0.0)]


def test_make_rates_use_only_earlier_seasons_and_fall_back_when_thin():
    events = ([{"season": 2019, "distance": 32.0, "made": 1.0 if i % 2 else 0.0, "kicker": "a", "week": 1, "game_id": "x", "team": HOME}
               for i in range(60)] +
              [{"season": 2020, "distance": 32.0, "made": 1.0, "kicker": "a", "week": 1, "game_id": "y", "team": HOME}])
    rates = st.make_rates(events)
    assert rates[2019][30] == st.DEFAULT_MAKE_RATE[30]            # 2019 sees nothing earlier: default
    assert rates[2020][30] == pytest.approx(0.5)                  # 60 earlier kicks, half made; 2020's own kick is invisible
    assert rates[2020][50] == st.DEFAULT_MAKE_RATE[50]            # a bin with under 30 earlier kicks keeps the default


def test_kicker_ratings_are_shrunk_per_attempt_and_unknown_kickers_are_neutral():
    state = {"k": [10.0, 1.5, 4.0, 0.8]}                          # 10 short attempts +1.5 over expected; 4 long +0.8
    acc, rng = st.kicker_ratings(state, "k")
    assert acc == pytest.approx(1.5 / (10 + st.KICKER_SHORT_PRIOR))
    assert rng == pytest.approx(0.8 / (4 + st.KICKER_LONG_PRIOR))
    assert st.kicker_ratings(state, "nobody") == (0.0, 0.0) and st.kicker_ratings(state, None) == (0.0, 0.0)
    # a small sample of brilliance is pulled hard toward average; a long record is not
    assert abs(st.kicker_ratings({"k": [3.0, 1.0, 0.0, 0.0]}, "k")[0]) < abs(st.kicker_ratings({"k": [300.0, 100.0, 0.0, 0.0]}, "k")[0])


def test_snapshot_shrinks_to_the_league_mean_and_separates_offence_from_defence():
    keys = st.ALL_KEYS
    record = dict.fromkeys(keys, 0.0)
    record.update(season=2020, drives=4.0, tds=2.0, games=1.0)
    record.update({"opp_" + k: 0.0 for k in keys})
    record.update(opp_drives=10.0, opp_tds=1.0, opp_games=1.0)    # the other offence scored on 1 of 10 drives against this team
    league = dict.fromkeys(keys, 0.0)
    league.update(drives=11.0, tds=1.4, games=1.0)
    snap = st._snapshot([record], 2020, league)
    assert snap["off_td"] == pytest.approx((2.0 + 3 * 1.4) / (4.0 + 3 * 11.0))
    assert snap["def_td"] == pytest.approx((1.0 + 3 * 1.4) / (10.0 + 3 * 11.0))
    assert snap["off_td"] != snap["def_td"]


def test_blend_is_own_offence_with_the_opponents_defence():
    me = {"off_td": 0.30, "def_td": 0.10}
    opp = {"off_td": 0.20, "def_td": 0.40}
    assert st.blend_side(me, opp, "td") == pytest.approx((0.30 + 0.40) / 2)      # my offence with THEIR defence
    assert st.blend_side(opp, me, "td") == pytest.approx((0.20 + 0.10) / 2)


def test_attach_builds_diffs_and_sums_and_values_a_kicker_in_points():
    snap = lambda **kw: {**{f"off_{n}": 0.3 for n in list(st.BLENDED) + list(st.UNIT)},    # noqa: E731
                         **{f"def_{n}": 0.3 for n in st.BLENDED}, "fg_att_pg": 2.0, **kw}
    features = {"g": {"teams": (HOME, AWAY), "sides": {HOME: snap(off_punt_net=42.0), AWAY: snap(off_punt_net=39.0)},
                      "kicker": {HOME: (0.02, 0.10), AWAY: (0.0, 0.0)}}}
    row = {"game_id": "g", "home_team": "DEN", "away_team": "KAN"}
    st.attach([row], features)
    assert row["st_punt_net_diff"] == 3.0 and row["st_punt_net_sum"] == 81.0
    expected_home = 2.0 * 3.0 * (0.7 * 0.02 + 0.3 * 0.10)          # attempts x 3 points x blended make-over-expected
    assert row["st_fg_pts_h"] == pytest.approx(expected_home) and row["st_fg_pts_a"] == 0.0
    assert row["st_fg_pts_diff"] == pytest.approx(expected_home) and row["st_fg_pts_sum"] == pytest.approx(expected_home)
    assert row["eff_td_diff"] == 0.0 and row["eff_td_sum"] == pytest.approx(0.6)
