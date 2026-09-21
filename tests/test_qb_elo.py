"""Tests for the CFB QB Elo engine's era-adjustment and rating math -- the
parts with real logic worth verifying beyond eyeballing the leaderboard."""
from __future__ import annotations

import math

from sports_aggregator.cfb.qb_elo import (
    ALPHA, BASE, RATING_SCALE, _season_environment, _zscore,
)


def test_season_environment_is_computed_per_season_not_pooled():
    rows = [
        {"season": 2020, "ppa_all": 0.10, "pass_yards": 300, "attempts": 30, "pass_td": 3, "pass_int": 1},
        {"season": 2020, "ppa_all": 0.30, "pass_yards": 350, "attempts": 30, "pass_td": 4, "pass_int": 0},
        {"season": 2024, "ppa_all": 0.50, "pass_yards": 400, "attempts": 30, "pass_td": 5, "pass_int": 0},
        {"season": 2024, "ppa_all": 0.70, "pass_yards": 450, "attempts": 30, "pass_td": 6, "pass_int": 0},
    ]
    env = _season_environment(rows)
    assert set(env.keys()) == {2020, 2024}
    ppa_mean_2020, _ = env[2020]["ppa"]
    ppa_mean_2024, _ = env[2024]["ppa"]
    assert math.isclose(ppa_mean_2020, 0.20)
    assert math.isclose(ppa_mean_2024, 0.60)
    # A 2024-typical game would look exceptional against 2020's environment --
    # this is exactly the cross-era contamination per-season scoping avoids.
    assert ppa_mean_2024 > ppa_mean_2020


def test_season_environment_missing_values_are_excluded_not_zero_filled():
    rows = [
        {"season": 2020, "ppa_all": 0.2, "pass_yards": None, "attempts": 0, "pass_td": None, "pass_int": None},
        {"season": 2020, "ppa_all": None, "pass_yards": 300, "attempts": 30, "pass_td": 2, "pass_int": 1},
    ]
    env = _season_environment(rows)
    ppa_mean, _ = env[2020]["ppa"]
    ypa_mean, _ = env[2020]["ypa"]
    assert math.isclose(ppa_mean, 0.2)  # only the row with a real PPA counts
    assert math.isclose(ypa_mean, 10.0)  # only the row with real yards/attempts counts


def test_zscore_centers_on_zero():
    z = _zscore({"a": 1.0, "b": 2.0, "c": 3.0})
    assert abs(z["b"]) < 1e-9
    assert z["a"] < 0 < z["c"]


def test_alpha_blend_pulls_rating_toward_game_performance_not_all_the_way():
    # Simulate one update by hand the same way build() does it.
    pre_rating = BASE
    ppa_z = 2.0  # an excellent, 2-sigma game
    game_rating = BASE + ppa_z * RATING_SCALE
    post_rating = (1 - ALPHA) * pre_rating + ALPHA * game_rating
    assert post_rating > pre_rating
    # One great game should move the rating meaningfully but not swing it
    # all the way to the single-game-implied rating -- that would make the
    # "person, not just today's box score" framing pointless.
    assert post_rating < game_rating
    assert (post_rating - pre_rating) == ALPHA * (game_rating - pre_rating)


def test_repeated_elite_games_converge_toward_but_never_reach_the_ceiling():
    rating = BASE
    ppa_z = 3.0
    ceiling = BASE + ppa_z * RATING_SCALE
    for _ in range(50):
        game_rating = BASE + ppa_z * RATING_SCALE
        rating = (1 - ALPHA) * rating + ALPHA * game_rating
    assert rating < ceiling
    assert abs(rating - ceiling) < 1.0  # converged close, asymptotically
