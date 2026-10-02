"""NFL Margin Power (SRS): the solver's properties, the pregame snapshots, and the edge-vs-line scoring."""
import pytest

from sports_aggregator.nfl import margin_power as mp


def g(gid, season, week, home, away, hs, as_):
    return {"game_id": gid, "season": season, "week": week, "home_team": home, "away_team": away,
            "home_score": hs, "away_score": as_}


def test_srs_recovers_known_strengths_and_centres_on_zero():
    # neutral-field truth: A beats B by 6, A beats C by 3, C beats B by 3  ->  ratings A +3, C 0, B -3
    h = mp.HOME_FIELD
    games = [g(1, 2020, 1, "A", "B", 6 + h, 0), g(2, 2020, 2, "B", "A", 0, 6 - h),
             g(3, 2020, 3, "A", "C", 3 + h, 0), g(4, 2020, 4, "C", "A", 0, 3 - h),
             g(5, 2020, 5, "C", "B", 3 + h, 0), g(6, 2020, 6, "B", "C", 0, 3 - h)]
    ratings, counts = mp.solve_srs(games)
    assert abs(sum(ratings.values())) < 1e-9
    assert counts == {"A": 4, "B": 4, "C": 4}
    assert ratings == pytest.approx({"A": 3.0, "C": 0.0, "B": -3.0}, abs=1e-9)         # recovered exactly


def test_exact_solver_handles_the_sparse_graph_where_the_averaging_iteration_collapses_to_zero():
    two_teams = [g(i, 2020, i, "A", "B", 24, 10) for i in range(1, 4)]                  # only ever played each other
    exact, _ = mp.solve_srs(two_teams)
    assert exact["A"] == pytest.approx(-exact["B"]) and exact["A"] == pytest.approx((14 - mp.HOME_FIELD) / 2)
    iterative, _ = mp.solve_srs_iterative(two_teams)
    assert iterative["A"] == pytest.approx(0.0) and iterative["B"] == pytest.approx(0.0)    # the oscillation, documented
    # on a well-connected graph the two methods agree
    h = mp.HOME_FIELD
    ring = [g(i, 2020, i, t, u, 7 + h + i, 0) for i, (t, u) in enumerate([("A", "B"), ("B", "C"), ("C", "A"), ("A", "B"), ("B", "C"), ("C", "A"), ("A", "C")])]
    e, _ = mp.solve_srs(ring)
    it, _ = mp.solve_srs_iterative(ring)
    assert all(abs(e[t] - it[t]) < 1e-3 for t in e)


def test_home_field_is_neutralised_so_a_team_is_not_rewarded_for_playing_at_home():
    # both teams win only at home, by exactly the home-field edge: they are equally good
    games = [g(1, 2020, 1, "A", "B", mp.HOME_FIELD, 0), g(2, 2020, 2, "B", "A", mp.HOME_FIELD, 0)]
    ratings, _ = mp.solve_srs(games)
    assert abs(ratings["A"] - ratings["B"]) < 1e-9


def test_opponent_adjustment_a_big_win_over_a_bad_team_counts_less():
    # B beats C by 10; A beats D by 10; but C is awful (loses to everybody) and D is decent
    games = [g(1, 2020, 1, "B", "C", 10 + mp.HOME_FIELD, 0), g(2, 2020, 2, "A", "D", 10 + mp.HOME_FIELD, 0),
             g(3, 2020, 3, "D", "B", 0 + mp.HOME_FIELD, 0), g(4, 2020, 4, "D", "C", 14 + mp.HOME_FIELD, 0),
             g(5, 2020, 5, "A", "C", 14 + mp.HOME_FIELD, 0)]
    ratings, _ = mp.solve_srs(games)
    assert ratings["D"] > ratings["C"]
    assert ratings["A"] > 0 and ratings["C"] < 0


def test_a_prior_pulls_thin_ratings_toward_it_and_fades_as_games_arrive():
    prior = {"A": 6.0, "B": -6.0}
    with_prior, _ = mp.solve_srs([], prior, mp.PRIOR_K)
    assert with_prior["A"] > 0 > with_prior["B"]                                  # week 1: the prior is all there is
    few = [g(1, 2020, 1, "A", "B", mp.HOME_FIELD - 3, 0)]                          # one game says A is 3 points WORSE
    many = few * 1 + [g(i, 2020, i, "A", "B", mp.HOME_FIELD - 3, 0) for i in range(2, 12)]
    r_few, _ = mp.solve_srs(few, prior, mp.PRIOR_K)
    r_many, _ = mp.solve_srs(many, prior, mp.PRIOR_K)
    assert r_few["A"] - r_few["B"] > r_many["A"] - r_many["B"]                     # the more evidence, the less the prior counts
    assert r_many["A"] - r_many["B"] < 0.5 * (r_few["A"] - r_few["B"]) + 1


def test_snapshots_use_only_earlier_weeks_and_the_two_modes_differ_in_availability(monkeypatch):
    games = [g("a", 2019, w, "A", "B", 24, 10) for w in range(1, 6)] + \
            [g(f"s{w}", 2020, w, "A", "B", 20, 17) for w in range(1, 7)]
    monkeypatch.setattr(mp, "_games", lambda *args, **kwargs: games)
    within = mp.snapshots(None, 2020, 2020, "within")
    carry = mp.snapshots(None, 2020, 2020, "carry")
    assert "s1" in carry and "s1" not in within and "s3" not in within            # within needs MIN_GAMES prior games each
    assert "s4" in within                                                           # three games played by both sides
    assert carry["s1"]["srs_margin"] > mp.HOME_FIELD + 3                            # week 1 already knows A was strong in 2019
    # no leakage: a game's own score cannot move its own pregame rating
    altered = [dict(x) for x in games]
    altered[-1]["home_score"] = 99
    monkeypatch.setattr(mp, "_games", lambda *args, **kwargs: altered)
    assert mp.snapshots(None, 2020, 2020, "carry")["s6"]["srs_margin"] == carry["s6"]["srs_margin"]


def test_ats_scoring_backs_the_side_the_rating_favours_and_drops_pushes_and_small_edges():
    rows = [{"edge": 3.0, "line": 0.0, "y": 10.0},       # rating above the line -> back home; home covered: win
            {"edge": 3.0, "line": 0.0, "y": -10.0},      # back home; did not cover: loss
            {"edge": -3.0, "line": 0.0, "y": -10.0},     # rating below the line -> back away; home failed to cover: win
            {"edge": 3.0, "line": 0.0, "y": 0.0},        # push: dropped
            {"edge": 0.2, "line": 0.0, "y": -5.0}]       # tiny edge, backs home, loses (dropped once a floor is set)
    assert mp._ats(rows, "edge", "line", "y") == {"n": 4, "win_rate": 0.5, "z_vs_50": 0.0, "units_at_-110": -0.2}
    # with a floor of 1, only the three decided games with |edge| >= 1 remain, two of them wins
    big = mp._ats(rows, "edge", "line", "y", floor=1.0)
    assert (big["n"], big["win_rate"]) == (3, round(2 / 3, 4))
