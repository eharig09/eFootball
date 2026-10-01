from sports_aggregator.nfl.qb_player_ablation import EPA_PRIOR_ATTEMPTS, _paired, _rating


def _game(season, att, epa):
    return {"season": season, "attempts": att, "total_epa": epa, "cpoe_plays": att, "cpoe_total": 0.0}


def test_no_history_rating_is_league_prior():
    r = _rating([], 2020, 0.6, league_epa=0.05, league_cpoe=1.0)
    assert r["epa"] == 0.05 and r["cpoe"] == 1.0 and r["exp"] == 0.0


def test_rating_shrinks_small_samples_toward_league():
    small = _rating([_game(2020, 20, 10.0)], 2020, 0.6, 0.0, 0.0)["epa"]
    big = _rating([_game(2020, 2000, 1000.0)], 2020, 0.6, 0.0, 0.0)["epa"]
    assert 0 < small < 0.5 * big < 0.25
    assert abs(small - 10.0 / (20 + EPA_PRIOR_ATTEMPTS)) < 1e-9


def test_older_seasons_decay():
    old = _rating([_game(2018, 500, 250.0)], 2020, 0.5, 0.0, 0.0)["epa"]
    new = _rating([_game(2020, 500, 250.0)], 2020, 0.5, 0.0, 0.0)["epa"]
    assert old < new


def test_paired_needs_sample_and_signs_improvement():
    assert _paired([{}] * 5, "a", "b") == {"n": 5}
    rows = [{"actual_margin": 0.0, "pred_a": 10.0 + i % 3, "pred_b": 5.0 + i % 3} for i in range(60)]
    assert _paired(rows, "pred_a", "pred_b")["mean_abs_err_diff"] < 0
