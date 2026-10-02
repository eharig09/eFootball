"""NFL scoring-form ratings and Line Elo (nfl.scoring_form_model / nfl.line_elo)."""
import math

from sports_aggregator.nfl import line_elo, scoring_form_model as sf


def _g(gid, season, week, home, away, hs, as_, spread=None):
    return {"game_id": gid, "season": season, "week": week, "home_team": home, "away_team": away,
            "home_score": hs, "away_score": as_, "spread_line": spread, "completed": 1 if hs is not None else 0}


# ----------------------------------------------------------------------------------------------- scoring form
def test_season_rating_is_shrunk_toward_the_league_mean_and_decays_old_seasons():
    one_game = [{"season": 2020, "for": 40.0, "against": 10.0, "for_adj": 40.0, "against_adj": 10.0}]
    rating = sf._season_rating(one_game, 2020, 23.0, adjust=False)
    assert 23.0 < rating["off"] < 40.0 and 10.0 < rating["def"] < 23.0           # one game barely moves it
    assert abs(rating["off"] - (40.0 + sf.PRIOR_GAMES * 23.0) / (1 + sf.PRIOR_GAMES)) < 1e-9
    stale = sf._season_rating([dict(one_game[0], season=2017)], 2020, 23.0, adjust=False)
    assert abs(stale["off"] - 23.0) < abs(rating["off"] - 23.0)                  # a three-season-old game counts for less


def test_form_uses_only_the_last_three_games_and_fills_missing_with_the_league_mean():
    records = [{"for": p, "against": 20.0, "for_adj": p, "against_adj": 20.0} for p in (3.0, 30.0, 30.0, 30.0)]
    assert sf._form_rating(records, 23.0, adjust=False)["off"] == 30.0           # the 3-point game fell out
    short = sf._form_rating(records[:1], 23.0, adjust=False)
    assert abs(short["off"] - (3.0 + 2 * 23.0) / 3) < 1e-9


def test_pregame_features_never_use_the_games_own_week(monkeypatch):
    games = [_g("a", 2020, 1, "X", "Y", 30, 10), _g("b", 2020, 1, "Z", "W", 20, 20),
             _g("c", 2020, 2, "X", "Z", 24, 17)]
    monkeypatch.setattr(sf, "_games", lambda *args, **kwargs: games)
    feats = sf.pregame_features(None, 2020, 2020)
    assert "a" not in feats and "b" not in feats                  # week 1: nobody has a history yet
    assert set(feats["c"]) >= {"sf_margin_season", "sf_margin_form", "sf_total_season", "sf_total_form"}
    assert feats["c"]["sf_margin_season"] > 0                    # X won 30-10, Z drew 20-20: X rates ahead of Z
    # totals are the two teams' expected points added together: X scored 30 and Z allowed 20 against a ~25 league
    assert 40 < feats["c"]["sf_total_season"] < 55


# ----------------------------------------------------------------------------------------------- line elo
def test_line_elo_moves_toward_the_market_and_signals_use_pre_update_ratings(monkeypatch):
    games = [_g(i, 2020, i, "A", "B", 24, 17, spread=7.0) for i in range(1, 9)]       # A is always a 7-point home favourite
    monkeypatch.setattr(line_elo, "_games", lambda *args, **kwargs: games)
    rows = line_elo.build(None, 2020, 2020)
    assert abs(rows[0]["line_elo_expected"] - line_elo.HOME_FIELD) < 1e-9             # nothing known yet: just home field
    assert rows[0]["innovation"] == 7.0 - line_elo.HOME_FIELD
    expected = [r["line_elo_expected"] for r in rows]
    assert all(b > a for a, b in zip(expected, expected[1:]))                          # converges up toward 7
    assert rows[-1]["line_elo_expected"] < 7.0
    assert [r["innovation"] for r in rows] == sorted((r["innovation"] for r in rows), reverse=True)
    assert rows[0]["ats_resid"] == 0.0                                                 # 24-17 vs a 7-point line: push


def test_lines_in_the_same_week_do_not_inform_each_other(monkeypatch):
    games = [_g("one", 2020, 1, "A", "B", 24, 17, spread=7.0), _g("two", 2020, 1, "A", "C", 24, 17, spread=7.0)]
    monkeypatch.setattr(line_elo, "_games", lambda *args, **kwargs: games)
    rows = line_elo.build(None, 2020, 2020)
    assert rows[0]["line_elo_expected"] == rows[1]["line_elo_expected"] == line_elo.HOME_FIELD


def test_offseason_regression_pulls_ratings_back_toward_1500(monkeypatch):
    games = [_g(i, 2020, i, "A", "B", 30, 10, spread=10.0) for i in range(1, 6)] + [_g("n", 2021, 1, "A", "B", 30, 10, spread=10.0)]
    monkeypatch.setattr(line_elo, "_games", lambda *args, **kwargs: games)
    kept = line_elo.build(None, 2020, 2021, regression=0.0)
    regressed = line_elo.build(None, 2020, 2021, regression=1.0 / 3.0)
    assert kept[4]["line_elo_expected"] == regressed[4]["line_elo_expected"]           # same within a season
    assert regressed[5]["line_elo_expected"] < kept[5]["line_elo_expected"]            # pulled back across the offseason
    full = line_elo.build(None, 2020, 2021, regression=1.0)
    assert abs(full[5]["line_elo_expected"] - line_elo.HOME_FIELD) < 1e-9              # total regression forgets everything


def test_unplayed_games_get_signals_but_never_update_ratings(monkeypatch):
    games = [_g("done", 2020, 1, "A", "B", 24, 17, spread=7.0), _g("todo", 2020, 2, "A", "B", None, None, spread=7.0)]
    monkeypatch.setattr(line_elo, "_games", lambda *args, **kwargs: games)
    rows = line_elo.build(None, 2020, 2020, include_week=(2020, 2))
    assert rows[1]["margin"] is None and rows[1]["ats_resid"] is None
    assert rows[1]["line_elo_expected"] > line_elo.HOME_FIELD                          # learned from the played game


def test_cover_stats_and_slope_helpers():
    rows = [{"ats_resid": 3.0}, {"ats_resid": -1.0}, {"ats_resid": 0.0}, {"ats_resid": -4.0}]
    home = line_elo._cover_stats(rows, "home")
    assert (home["n"], home["win_rate"]) == (3, round(1 / 3, 4))                      # the push is excluded
    assert line_elo._cover_stats(rows, "away")["win_rate"] == round(2 / 3, 4)
    steep = [{"x": float(i), "ats_resid": 2.0 * i + (1 if i % 2 else -1)} for i in range(100)]
    result = line_elo._slope(steep, "x")
    assert abs(result["slope"] - 2.0) < 0.05 and result["t"] > 50
    assert line_elo._slope(steep[:10], "x") == {"n": 10}                               # too few to fit
    assert math.isclose(line_elo._signal_block(steep, "x", 1000.0)["regression"]["slope"], result["slope"])
