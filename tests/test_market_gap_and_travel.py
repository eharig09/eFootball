import numpy as np

from sports_aggregator.nfl.market_gap import _segments, _stat
from sports_aggregator.nfl.travel_ablation import _home_bases, _miles


def test_great_circle_miles_known_pairs():
    nyc, la = (40.8135, -74.0745), (33.9535, -118.3392)
    assert 2400 < _miles(nyc, la) < 2500
    assert _miles(nyc, nyc) == 0.0


def test_home_base_is_the_most_hosted_stadium_per_season():
    games = [{"season": 2019, "home_team": "LV", "stadium": "Oakland-Alameda County Coliseum"}] * 7 + \
            [{"season": 2019, "home_team": "LV", "stadium": "Ring Central Coliseum"}] + \
            [{"season": 2020, "home_team": "LV", "stadium": "Allegiant Stadium"}] * 8
    bases = _home_bases(games)
    assert round(bases[(2019, "LV")][0], 1) == 37.8 and round(bases[(2020, "LV")][0], 1) == 36.1


def test_stat_signs_a_market_that_is_closer():
    rows = [{"pred": 10.0, "market": 3.0, "actual": 2.0}] * 40
    s = _stat(rows)
    assert s["gap"] > 0 and s["market_closer"] == 1.0 and s["model_mae"] > s["market_mae"]


def test_segments_cover_the_expected_buckets():
    seg = _segments()
    row = {"week": 2, "market": -3.5, "pred": 1.0, "rest_diff": 1.0, "division_game": 1,
           "weekday": "Thursday", "any_qb_change": True, "elo_diff": 200}
    assert seg["week"](row) == "w01-03" and seg["abs_spread"](row) == "3-6.5"
    assert seg["slot"](row) == "thu_mon" and seg["model_vs_market_side"](row) == "model_more_home"
    assert seg["qb_change"](row) == "qb_change" and seg["rest"](row) == "home_rest_edge"
