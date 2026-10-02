"""Pace / xDrives totals: leak-safe norms, drift-aware centering, and the points-per-drive matchup blend."""
import pytest

from sports_aggregator.nfl import pace_totals as pt
from sports_aggregator.nfl.naming import canon_team

HOME, AWAY = canon_team("DEN"), canon_team("KAN")


def rec(gid, season, week, team, opp, pts, drives, opp_pts, opp_drives):
    return {"game_id": gid, "team": team, "opponent": opp, "season": season, "week": week,
            "pts": float(pts), "drives": float(drives), "opp_pts": float(opp_pts), "opp_drives": float(opp_drives)}


def test_season_norms_use_only_earlier_seasons():
    records = [rec("a", 2019, 1, HOME, AWAY, 30, 12, 20, 10), rec("a", 2019, 1, AWAY, HOME, 20, 10, 30, 12),
               rec("b", 2020, 1, HOME, AWAY, 99, 99, 99, 99)]
    norms = pt.season_norms(records)
    assert 2019 not in norms                                      # nothing came before it
    assert norms[2020]["drives_per_game"] == pytest.approx(2 * (12 + 10) / 2)   # 2019 only; the 2020 game is invisible
    assert norms[2020]["ppd"] == pytest.approx((30 + 20) / (12 + 10))


def test_drive_model_centre_is_the_previous_season_not_the_whole_history():
    rows = ([{"season": 2018, "sum_pred_drives": 24.0}] * 150 + [{"season": 2019, "sum_pred_drives": 22.0}] * 150 +
            [{"season": 2020, "sum_pred_drives": 20.0}] * 150 + [{"season": 2021, "sum_pred_drives": 20.0}] * 150)
    norms = pt.xd_norms(rows)
    assert 2018 not in norms
    assert norms[2019]["xd_mean"] == 24.0 and norms[2020]["xd_mean"] == 22.0
    assert norms[2021]["xd_mean"] == 20.0          # NOT the 22.0 average of everything earlier: drives drift down over time
    thin = pt.xd_norms([{"season": 2019, "sum_pred_drives": 22.0}] * 50 + [{"season": 2020, "sum_pred_drives": 21.0}] * 150)
    assert 2020 not in thin                         # a previous season with under 100 games is not trusted


def test_ppd_snapshots_exclude_the_games_own_week_and_shrink_to_the_league():
    records = [rec("w1", 2020, 1, HOME, AWAY, 40, 10, 10, 10), rec("w1", 2020, 1, AWAY, HOME, 10, 10, 40, 10),
               rec("w2", 2020, 2, HOME, AWAY, 7, 10, 7, 10), rec("w2", 2020, 2, AWAY, HOME, 7, 10, 7, 10)]
    snaps = pt.ppd_snapshots(records, 2020)
    assert "w1" not in snaps                                       # no history before week 1
    home = snaps["w2"][HOME]
    assert home["off_ppd"] > 2.1 and home["off_ppd"] < 4.0         # saw the 40-point week, but shrunk toward the league mean
    league = (40 + 10) / (10 + 10)                                  # 50 points over 20 drives
    assert home["off_ppd"] == pytest.approx((40 + pt.PRIOR_DRIVES * league) / (10 + pt.PRIOR_DRIVES), rel=1e-6)
    assert snaps["w2"][AWAY]["def_ppd"] > snaps["w2"][HOME]["def_ppd"]    # HOME's defense allowed 10, AWAY's allowed 40


def test_pace_total_blends_own_offense_with_the_opponents_defense_and_centres_excess_drives():
    other = canon_team("SEA")
    records = [rec("w1", 2020, 1, HOME, AWAY, 40, 10, 10, 10), rec("w1", 2020, 1, AWAY, HOME, 10, 10, 40, 10),
               # a second week-1 game in which AWAY's defense shut a team out, so its defense differs from HOME's offense
               rec("x1", 2020, 1, other, AWAY, 0, 10, 20, 10), rec("x1", 2020, 1, AWAY, other, 20, 10, 0, 10),
               rec("p1", 2019, 1, HOME, AWAY, 22, 11, 22, 11), rec("p1", 2019, 1, AWAY, HOME, 22, 11, 22, 11)]
    row = {"game_id": "g", "season": 2020, "home_team": "DEN", "away_team": "KAN",
           "sum_pred_drives": 24.0, "diff_pred_drives": 2.0}
    # the game itself, in week 2, so week 1 supplies the ratings
    records += [rec("g", 2020, 2, HOME, AWAY, 0, 10, 0, 10), rec("g", 2020, 2, AWAY, HOME, 0, 10, 0, 10)]
    prior_rows = [{"game_id": f"old{i}", "season": 2019, "sum_pred_drives": 22.0} for i in range(150)] + [row]
    pt.attach_pace(prior_rows, records, 2020)
    snap = pt.ppd_snapshots(records, 2020)["g"]
    ppd_home = (snap[HOME]["off_ppd"] + snap[AWAY]["def_ppd"]) / 2        # home offense with AWAY's defense
    ppd_away = (snap[AWAY]["off_ppd"] + snap[HOME]["def_ppd"]) / 2
    assert row["pace_total"] == pytest.approx(13.0 * ppd_home + 11.0 * ppd_away)     # xD split (24 +/- 2)/2 = 13 and 11
    assert row["pace_total"] != pytest.approx(13.0 * snap[HOME]["off_ppd"] + 11.0 * snap[AWAY]["off_ppd"])
    assert row["excess_drives"] == pytest.approx(24.0 - 22.0)               # centred on the previous season's projection
    assert row["excess_points"] == pytest.approx(2.0 * pt.season_norms(records)[2020]["ppd"])


def test_the_line_miss_test_buckets_by_z_and_splits_the_rule_by_era():
    rows = []
    for season in (2016, 2017, 2018, 2019, 2020, 2021):
        for z, delta in ((1.5, -3.0), (-1.5, 3.0), (0.0, 0.0)):       # more drives -> under, fewer -> over: an inverted pattern
            for i in range(30):
                rows.append({"season": season, "excess_z": z, "excess_drives": z, "excess_points": 2.0 * z,
                             "market_total": 45.0, "actual_total": 45.0 + delta + (0.5 if i % 2 else -0.5)})
    out = pt._bucket_vs_line(rows)
    assert out["buckets"]["z>=1 (far more drives)"]["mean_actual_minus_line"] == pytest.approx(-3.0, abs=0.1)
    assert out["buckets"]["z>=1 (far more drives)"]["over_rate"] == 0.0
    rule = out["rule_excess_z_ge_1"]
    assert rule["all"]["win_rate"] == 0.0 and rule["all"]["over_picks"] == 180 and rule["all"]["under_picks"] == 180
    assert rule["first_half"]["n"] > 0 and rule["second_half"]["n"] > 0 and rule["second_half_starts"] == 2019


def test_drive_diagnostics_report_how_little_drives_explain():
    rows = [{"actual_drives": 20.0 + (i % 5), "xd_sum": 21.0 + (i % 3) * 0.5, "actual_total": 30.0 + 4 * (i % 7)} for i in range(200)]
    d = pt._drive_diagnostics(rows)
    assert d["games"] == 200 and d["xd_mae"] >= 0 and d["total_variance_share"]["drives_only_r2"] < 0.05
