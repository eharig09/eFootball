import itertools

import numpy as np

from sports_aggregator.nfl import playoff_bracket as bracket
from sports_aggregator.nfl import tiebreakers as tb
from sports_aggregator.nfl.playoff_sim import Prepared, simulate
from sports_aggregator.nfl.playoff_state import SeasonState, fit_ratings


# ---------------------------------------------------------------- tiebreakers
def ctx_from(n, games, conf=None, div=None, rand=None):
    conf = conf if conf is not None else ["A"] * n
    div = div if div is not None else ["A1"] * n
    align = tb.Alignment.from_labels(conf, div)
    H, N, ND, PF, PA = tb.matrices_from_games(n, games)
    return tb.TieContext(H, N, ND, PF, PA, align, np.arange(n, dtype=float) if rand is None else rand)


def test_head_to_head_decides_before_point_differential():
    # 0 and 1 are both 1-1; 0 beat 1 head to head but 1 has the far better point differential
    games = [(0, 1, 17, 16), (1, 2, 50, 0), (2, 0, 10, 3)]
    ctx = ctx_from(3, games)
    assert tb.break_tie([0, 1], tb.DIVISION_STEPS, ctx) == [0, 1]


def test_division_record_decides_when_head_to_head_is_split():
    # 0 and 1 split their games; 0 is 2-0 vs the rest of the division, 1 is 1-1
    games = [(0, 1, 10, 20), (1, 0, 10, 20), (0, 2, 10, 3), (0, 3, 10, 3),
             (1, 2, 10, 3), (3, 1, 10, 3)]
    ctx = ctx_from(4, games)
    assert tb.break_tie([0, 1], tb.DIVISION_STEPS, ctx) == [0, 1]


def test_three_team_cycle_falls_through_to_net_points():
    # 0 beat 1, 1 beat 2, 2 beat 0: head-to-head cannot separate them
    games = [(0, 1, 30, 10), (1, 2, 21, 20), (2, 0, 24, 23)]
    ctx = ctx_from(3, games)
    order = tb.break_tie([0, 1, 2], tb.DIVISION_STEPS, ctx)
    # net points: team 0 +19, team 2 -3+... team 0 is clearly first
    assert order[0] == 0 and sorted(order) == [0, 1, 2]


def test_wildcard_sweep_rule_requires_beating_everyone():
    # 0 beat both others (swept); 1 has the better point differential but did not sweep
    games = [(0, 1, 10, 9), (0, 2, 10, 9), (1, 2, 40, 0)]
    ctx = ctx_from(3, games, div=["X", "Y", "Z"])
    assert tb.break_tie([0, 1, 2], tb.WILDCARD_STEPS, ctx)[0] == 0


def test_losing_to_everyone_eliminates_a_club_then_the_rest_restart():
    # 2 lost to both; 0 and 1 then split, so later steps (net points) separate them
    games = [(0, 2, 10, 3), (1, 2, 10, 3), (0, 1, 24, 20), (1, 0, 30, 3)]
    ctx = ctx_from(3, games, div=["X", "Y", "Z"])
    order = tb.break_tie([0, 1, 2], tb.WILDCARD_STEPS, ctx)
    assert order[-1] == 2 and set(order[:2]) == {0, 1}


def test_coin_toss_is_the_last_resort():
    ctx = ctx_from(2, [], rand=np.array([0.9, 0.1]))
    assert tb.break_tie([0, 1], tb.DIVISION_STEPS, ctx) == [1, 0]


def test_division_reduction_before_wildcard_comparison():
    # 0 and 1 share a division and 0 beat 1; both are tied with outsider 2
    games = [(0, 1, 20, 10), (2, 0, 20, 10), (1, 2, 20, 10)]
    ctx = ctx_from(3, games, div=["D1", "D1", "D2"])
    order = tb._wildcard_group_order([0, 1, 2], ctx)
    assert sorted(order) == [0, 1, 2] and order.index(0) < order.index(1)


def _teams():
    return [(c, f"{c} {d}", f"{c}{d}{i}") for c in ("AFC", "NFC") for d in range(4) for i in range(4)]


def test_conference_seeds_pick_division_winners_then_best_wild_cards():
    teams = _teams()
    conf = [t[0] for t in teams]; div = [t[1] for t in teams]
    n = len(teams)

    def strength(i):   # earlier division, earlier slot = stronger
        return -(int(teams[i][2][3]) * 4 + int(teams[i][2][4]))

    games = []
    for a, b in itertools.combinations(range(n), 2):
        if conf[a] == conf[b]:
            games.append((a, b, 24, 10) if strength(a) > strength(b) else (b, a, 24, 10))
    ctx = ctx_from(n, games, conf=conf, div=div)
    afc = [i for i in range(n) if conf[i] == "AFC"]
    result = tb.conference_seeds(afc, ctx, 7)
    seeds = [teams[i][2] for i in result["seeds"]]
    assert len(seeds) == 7 and seeds[0] == "AFC00"
    assert sorted(teams[i][2] for i in result["division_winners"]) == ["AFC00", "AFC10", "AFC20", "AFC30"]
    assert set(seeds[4:]) == {"AFC01", "AFC02", "AFC03"}


# --------------------------------------------------------------------- bracket
def test_bracket_seven_team_chalk_and_reseeding():
    seen = []

    def win_prob(a, b, a_hosts):
        seen.append((a, b, a_hosts))
        return 1.0 if a < b else 0.0

    out = bracket.play_conference([1, 2, 3, 4, 5, 6, 7], win_prob, lambda p: p >= 0.5)
    assert seen[:3] == [(2, 7, True), (3, 6, True), (4, 5, True)]
    assert out["divisional"] == [1, 2, 3, 4] and out["champion"] == 1
    assert seen[3:5] == [(1, 4, True), (2, 3, True)]


def test_bracket_reseeds_so_top_seed_gets_lowest_survivor():
    seen = []
    upsets = {(2, 7), (3, 6), (4, 5)}

    def win_prob(a, b, a_hosts):
        seen.append((a, b))
        return 0.0 if (a, b) in upsets else (1.0 if a < b else 0.0)

    out = bracket.play_conference([1, 2, 3, 4, 5, 6, 7], win_prob, lambda p: p >= 0.5)
    assert out["divisional"] == [1, 5, 6, 7]
    assert seen[3:5] == [(1, 7), (5, 6)]


def test_bracket_six_team_format_has_two_byes():
    seen = []

    def win_prob(a, b, a_hosts):
        seen.append((a, b))
        return 1.0 if a < b else 0.0

    out = bracket.play_conference([1, 2, 3, 4, 5, 6], win_prob, lambda p: p >= 0.5)
    assert seen[:2] == [(3, 6), (4, 5)] and out["divisional"] == [1, 2, 3, 4]


def test_super_bowl_is_neutral():
    flags = []

    def win_prob(a, b, a_hosts):
        flags.append(a_hosts)
        return 1.0 if a < b else 0.0

    bracket.play_playoffs([[1, 2, 3, 4, 5, 6, 7], [8, 9, 10, 11, 12, 13, 14]], win_prob, lambda p: p >= 0.5)
    assert flags[-1] is False and all(flags[:-1])


# ------------------------------------------------------------------- simulator
def league_state(played=False):
    conference, division, elo, games, names = {}, {}, {}, [], []
    for c in ("AFC", "NFC"):
        for d in ("East", "North", "South", "West"):
            for i in range(4):
                name = f"{c[0]}{d[0]}{i}"
                conference[name] = c
                division[name] = f"{c} {d}"
                elo[name] = 1500 + 90 * (3 - i) - (20 if c == "NFC" else 0)
                names.append(name)
    gid = 0
    for a, b in itertools.combinations(names, 2):
        same_div = division[a] == division[b]
        same_conf = conference[a] == conference[b]
        if not (same_div or (same_conf and (sum(map(ord, a + b)) % 3 == 0))):
            continue
        gid += 1
        home, away = (a, b) if gid % 2 else (b, a)
        pts = (None, None)
        if played:
            pts = (27, 17) if elo[home] >= elo[away] else (14, 24)
        games.append({"id": str(gid), "week": 3, "home": home, "away": away, "neutral": False,
                      "div_game": same_div, "home_pts": pts[0], "away_pts": pts[1],
                      "line": None, "total": None})
    return SeasonState(2026, 0, conference, division, games, elo)


def test_ratings_follow_elo_prior_and_are_centred():
    ratings = fit_ratings(league_state())
    assert abs(sum(ratings.values())) < 1e-6 and ratings["AE0"] > ratings["AE3"]


def test_simulation_conserves_probability_mass():
    out = simulate(Prepared(league_state()), n_sims=300, seed=3)
    rows = {r["team"]: r for r in out["rows"]}
    total = lambda key: sum(r[key] for r in rows.values())
    assert abs(total("playoff") - 14) < 0.02
    assert abs(total("bye") - 2) < 0.02
    assert abs(total("division_title") - 8) < 0.02
    assert abs(total("divisional") - 8) < 0.02
    assert abs(total("championship") - 4) < 0.02
    assert abs(total("super_bowl") - 2) < 0.02
    assert abs(total("champion") - 1) < 0.02
    for r in rows.values():
        assert abs(sum(r["seed_probs"]) - r["playoff"]) < 1e-3
        assert r["champion"] <= r["super_bowl"] <= r["championship"] <= r["divisional"] <= r["playoff"] + 1e-9
        assert r["bye"] <= r["division_title"] + 1e-9
    assert rows["AE0"]["playoff"] > rows["AE3"]["playoff"]


def test_simulation_is_deterministic_per_seed():
    prep = Prepared(league_state())
    assert simulate(prep, n_sims=100, seed=5)["rows"] == simulate(prep, n_sims=100, seed=5)["rows"]


def test_finished_season_only_plays_the_bracket():
    prep = Prepared(league_state(played=True))
    assert prep.Gu == 0
    out = simulate(prep, n_sims=200, seed=1)
    rows = {r["team"]: r for r in out["rows"]}
    assert abs(sum(r["playoff"] for r in rows.values()) - 14) < 0.02
    for c in ("A", "N"):
        for d in ("E", "N", "S", "W"):
            assert rows[f"{c}{d}0"]["division_title"] == 1.0


def test_six_team_format():
    out = simulate(Prepared(league_state(), teams_per_conf=6), n_sims=100, seed=2)
    assert abs(sum(r["playoff"] for r in out["rows"]) - 12) < 0.02
    assert abs(sum(r["bye"] for r in out["rows"]) - 4) < 0.02
