"""Opponent-adjusted style ratings and the categories built from them."""
import pytest

from sports_aggregator.nfl import team_styles as ts
from sports_aggregator.nfl.style_matchup import Cells


def obs(game, week, team, opp, points, home_col=0.0):
    return {"game_id": game, "season": 2020, "week": week, "team": team, "opp": opp, "home_col": home_col,
            "points": points, "plays": 60, "pass_plays": 30, "pass_epa": 0.0, "rush_plays": 30, "rush_epa": 0.0,
            "explosive_plays": 6, "neutral_plays": 30, "neutral_passes": 15, "seconds_sum": 1500.0,
            "clocked_plays": 60}


def test_a_big_score_against_a_stingy_defence_rates_the_offence_higher_than_the_same_score_against_a_soft_one():
    teams = ["A", "B", "C", "D", "E", "F"]
    # D is soft (30 to both A and E); C is tougher (30 to B but only 10 to F). A and B both scored 30.
    rows = [obs(1, 1, "A", "D", 30), obs(1, 1, "D", "A", 10), obs(2, 1, "B", "C", 30), obs(2, 1, "C", "B", 10),
            obs(3, 2, "E", "D", 30), obs(3, 2, "D", "E", 10), obs(4, 2, "F", "C", 10), obs(4, 2, "C", "F", 10)]
    rated = ts.solve(rows, "points", teams, prior_k=0.5)
    assert rated["def"]["C"] < rated["def"]["D"]            # C allows less
    assert rated["off"]["B"] > rated["off"]["A"]


def test_ratings_are_centred_so_zero_is_the_league_average():
    teams = ["A", "B"]
    rows = [obs(1, 1, "A", "B", 30), obs(1, 1, "B", "A", 10)]
    rated = ts.solve(rows, "points", teams)
    assert sum(rated["off"].values()) == pytest.approx(0.0, abs=1e-9)
    assert sum(rated["def"].values()) == pytest.approx(0.0, abs=1e-9)


def test_with_no_games_the_league_mean_comes_from_the_prior_not_zero():
    rated = ts.solve([], "points", ["A", "B"], league_prior=(22.5, 1.5))
    assert rated["mu"] == pytest.approx(22.5, abs=0.05)


def test_neutral_site_rows_carry_no_home_edge():
    row = {"neutral_site": 1, "is_home": 1}
    assert (0.0 if row["neutral_site"] else (0.5 if row["is_home"] else -0.5)) == 0.0


def test_categories_are_league_terciles_of_the_snapshot():
    teams = [f"T{i:02d}" for i in range(30)]
    ranked = {t: float(i) for i, t in enumerate(teams)}
    side = lambda values: {"off": values, "def": values}
    ratings = {m: side(ranked) for m in ("pass_rate", "pace", "explosive", "pass_epa", "rush_epa")}
    ratings["rush_epa"] = side({t: -v for t, v in ranked.items()})        # defences ordered the other way on the run
    labels = ts.classify(ratings)
    assert labels["T29"]["approach"] == "Pass-heavy" and labels["T00"]["approach"] == "Run-heavy"
    assert labels["T00"]["tempo"] == "Up-tempo" and labels["T29"]["tempo"] == "Slow"
    assert labels["T29"]["vulnerability"] == "Pass-vulnerable" and labels["T00"]["vulnerability"] == "Run-vulnerable"
    counts = {}
    for t in teams:
        counts[labels[t]["approach"]] = counts.get(labels[t]["approach"], 0) + 1
    assert set(counts) == {"Run-heavy", "Balanced", "Pass-heavy"} and min(counts.values()) >= 9


def test_cell_adjustments_start_at_zero_and_are_shrunk_toward_it():
    cells = Cells(k=10)
    assert cells.adjustment("x") == 0.0
    for _ in range(10):
        cells.add("x", 4.0)
    assert cells.adjustment("x") == pytest.approx(2.0)        # 40 / (10 + 10)


def test_alignment_says_whether_the_offence_leans_on_what_the_defence_is_weakest_against():
    from sports_aggregator.nfl.style_clash import alignment
    air = {"approach": "Pass-heavy"}
    ground = {"approach": "Run-heavy"}
    assert alignment(air, {"vulnerability": "Pass-vulnerable"}) == "Fits"
    assert alignment(ground, {"vulnerability": "Run-vulnerable"}) == "Fits"
    assert alignment(air, {"vulnerability": "Run-vulnerable"}) == "Against the grain"
    assert alignment(ground, {"vulnerability": "Pass-vulnerable"}) == "Against the grain"
    assert alignment({"approach": "Balanced"}, {"vulnerability": "Pass-vulnerable"}) == "Neutral"


def test_style_clash_panel_renders_on_a_game_without_ratings_and_never_breaks_the_page():
    from flask import Flask, render_template_string
    template = "{% from '_nfl_style_clash.html' import style_clash %}{{ style_clash(sc, game) }}"
    app = Flask(__name__, template_folder="../templates")
    app.jinja_env.filters["cell"] = lambda v, f="text": "—" if v is None else str(v)
    with app.app_context():
        html = render_template_string(template, sc={"available": False, "reason": "No rows."},
                                      game={"away_team": "A", "home_team": "B"})
    assert 'id="style-clash"' in html and "No rows." in html


def _snap(values):
    """metric -> ratings for teams X and Y; `values` is {metric: (mu, off_x, off_y, def_x, def_y)}."""
    out = {"points": {"mu": 22.0, "home": 0.0, "off": {"X": 0.0, "Y": 0.0}, "def": {"X": 0.0, "Y": 0.0}}}
    for metric, (mu, ox, oy, dx, dy) in values.items():
        out[metric] = {"mu": mu, "home": 0.0, "off": {"X": ox, "Y": oy}, "def": {"X": dx, "Y": dy}}
    return out


def test_scoring_path_contributions_add_up_to_the_gap_to_the_league():
    from sports_aggregator.nfl import team_profile as tp
    snap = _snap({"drives": (10.0, -0.7, 0.7, 0.2, -0.2), "plays_per_drive": (6.0, 0.5, -0.5, -0.1, 0.1),
                  "points_per_play": (0.36, 0.03, -0.03, -0.02, 0.02)})
    paths = tp.paths_table(snap, "X")
    for side in ("off", "def"):
        d = paths[side]
        assert sum(r["points"] for r in d["rows"]) == pytest.approx(d["gap"])
        assert d["estimate"] - d["league_estimate"] == pytest.approx(d["gap"])
    # X holds the ball less often but longer and cashes in more per play: the three contributions disagree in sign
    signs = [r["points"] > 0 for r in paths["off"]["rows"]]
    assert signs == [False, True, True]
    assert paths["off"]["rows"][0]["rank"] == 2          # fewest drives of two teams, offence ranks high-is-first


def test_a_team_with_no_gap_to_the_league_gets_zero_contributions():
    from sports_aggregator.nfl import team_profile as tp
    snap = _snap({"drives": (10.0, 0.0, 0.0, 0.0, 0.0), "plays_per_drive": (6.0, 0.0, 0.0, 0.0, 0.0),
                  "points_per_play": (0.36, 0.0, 0.0, 0.0, 0.0)})
    assert all(r["points"] == 0.0 for r in tp.paths_table(snap, "X")["off"]["rows"])


def test_team_profile_panel_renders_its_unavailable_state():
    from flask import Flask, render_template_string
    template = "{% from '_nfl_team_profile.html' import profile_sections %}{{ profile_sections(tp, team) }}"
    app = Flask(__name__, template_folder="../templates")
    app.jinja_env.filters["cell"] = lambda v, f="text": "—" if v is None else str(v)
    with app.app_context():
        html = render_template_string(template, tp={"available": False, "reason": "No rows yet."},
                                      team={"abbreviation": "X"})
    assert 'id="adjusted-profile"' in html and "No rows yet." in html
