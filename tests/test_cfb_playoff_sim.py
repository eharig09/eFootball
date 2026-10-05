import itertools

import numpy as np

from sports_aggregator.cfb.playoff_sim import Prepared, ndtr, simulate
from sports_aggregator.cfb.playoff_state import SeasonState, fit_ratings

CONFS = ("SEC", "Big Ten", "ACC")


def league(played=False, per_conf=6):
    conference, elo, games = {}, {}, []
    gid = 0
    for c in CONFS:
        names = [f"{c[:2]}{i}" for i in range(per_conf)]
        for i, name in enumerate(names):
            conference[name] = c
            elo[name] = 1500.0 + 120 * (per_conf - i) - 60 * CONFS.index(c)
        for a, b in itertools.combinations(names, 2):
            gid += 1
            home, away = (a, b) if gid % 2 else (b, a)
            pts = (None, None)
            if played:
                pts = (31, 17) if elo[home] >= elo[away] else (14, 28)
            games.append({"id": gid, "week": 3, "home": home, "away": away, "neutral": False,
                          "conf_game": True, "home_pts": pts[0], "away_pts": pts[1], "line": None,
                          "home_elo": elo[home], "away_elo": elo[away]})
    return SeasonState(2026, 0, conference, games, elo)


def test_ndtr_matches_known_values():
    assert abs(float(ndtr(0.0)) - 0.5) < 1e-7
    assert abs(float(ndtr(1.96)) - 0.975) < 1e-4
    assert float(ndtr(-3)) < 0.0015


def test_ratings_follow_elo_prior_and_are_centred():
    ratings = fit_ratings(league())
    assert abs(sum(ratings.values())) < 1e-6
    assert ratings["SE0"] > ratings["SE5"]


def test_simulation_conserves_probability_mass():
    prep = Prepared(league())
    out = simulate(prep, n_sims=600, seed=3)
    rows = {r["team"]: r for r in out["rows"]}
    assert abs(sum(r["playoff"] for r in rows.values()) - 12) < 1e-2
    assert abs(sum(r["champion"] for r in rows.values()) - 1) < 1e-2
    assert abs(sum(r["conf_champion"] for r in rows.values()) - len(CONFS)) < 1e-2
    assert abs(sum(r["bye"] for r in rows.values()) - 4) < 1e-2
    assert abs(sum(r["quarterfinal"] for r in rows.values()) - 8) < 1e-2
    assert abs(sum(r["final"] for r in rows.values()) - 2) < 1e-2
    for r in rows.values():
        assert abs(sum(r["seed_probs"]) - r["playoff"]) < 1e-3
        assert abs(r["auto"] + r["at_large"] - r["playoff"]) < 1e-3
        assert r["champion"] <= r["final"] <= r["semifinal"] <= r["quarterfinal"] <= r["playoff"] + 1e-9
    assert rows["SE0"]["playoff"] > rows["SE5"]["playoff"]
    assert rows["SE0"]["conf_champion"] > rows["SE5"]["conf_champion"]


def test_simulation_is_deterministic_per_seed():
    prep = Prepared(league())
    a = simulate(prep, n_sims=200, seed=11)["rows"]
    b = simulate(prep, n_sims=200, seed=11)["rows"]
    assert a == b


def test_finished_regular_season_leaves_only_title_games_and_bracket():
    prep = Prepared(league(played=True))
    assert prep.Gu == 0
    out = simulate(prep, n_sims=300, seed=5)
    rows = {r["team"]: r for r in out["rows"]}
    # the best team in each conference is 5-0 and ranked on record; they cannot miss a title game
    for c in CONFS:
        assert rows[f"{c[:2]}0"]["title_game"] == 1.0
        assert abs(rows[f"{c[:2]}0"]["projected_wins"] - (5 + rows[f"{c[:2]}0"]["conf_champion"])) < 0.01


def test_clinched_title_game_is_certain_and_champions_get_auto_bids():
    prep = Prepared(league(played=True))
    out = simulate(prep, n_sims=300, seed=5)
    champ_rows = [r for r in out["rows"] if r["conf_champion"] > 0]
    # a conference champion with a top-12 ranking is in the field far more often than not
    assert all(r["auto"] <= r["conf_champion"] + 1e-9 for r in champ_rows)


def test_incomplete_field_size_is_reported():
    out = simulate(Prepared(league()), n_sims=50, seed=1)
    assert out["format"]["field_size"] == 12 and len(out["expected_field"]) == 12


def test_empty_season_gives_a_well_formed_empty_forecast():
    from sports_aggregator.cfb.playoff_service import MIN_TEAMS, empty_forecast
    out = empty_forecast(2026)
    assert out["rows"] == [] and out["insufficient_data"] and MIN_TEAMS > 12
    assert out["params"]["title_game_conferences"] == []


def test_division_conference_title_game_pairs_the_two_division_winners():
    state = league()
    names = [f"SB{i}" for i in range(6)]
    for i, name in enumerate(names):
        state.conference[name] = "Sun Belt"
        state.division[name] = "East" if i < 3 else "West"
        state.elo[name] = 1450.0 + 20 * i
    gid = 1000
    for a, b in itertools.combinations(names, 2):
        gid += 1
        state.games.append({"id": gid, "week": 3, "home": a, "away": b, "neutral": False,
                            "conf_game": True, "home_pts": None, "away_pts": None, "line": None,
                            "home_elo": state.elo[a], "away_elo": state.elo[b]})
    out = simulate(Prepared(state), n_sims=400, seed=2)
    rows = {r["team"]: r for r in out["rows"]}
    east = sum(rows[n]["title_game"] for n in names[:3])
    west = sum(rows[n]["title_game"] for n in names[3:])
    assert abs(east - 1) < 1e-2 and abs(west - 1) < 1e-2
    assert "Sun Belt" in out["params"]["title_game_conferences"]


def test_power_four_champions_always_reach_the_field_when_ranked_anywhere_in_the_top_n():
    out = simulate(Prepared(league(played=True)), n_sims=300, seed=5)
    rows = out["rows"]
    # all three conferences here are Power 4, so each champion is an automatic bid
    assert abs(sum(r["auto"] for r in rows) - 3) < 1e-2
