"""CFB team styles: the vectorised solver, FBS-only categories, and the panel's guards."""
import random

import pytest

from sports_aggregator.cfb import style_clash, team_styles as cs
from sports_aggregator.nfl import team_styles as nfl_styles


def obs(game, day, team, opp, points, home_col=0.0, season=2025, conference="SEC", **extra):
    return {"game_id": game, "season": season, "start_date": f"{season}-09-{day:02d}T16:00:00Z",
            "team": team, "opp": opp, "home_col": home_col, "conference": conference, "points": points,
            "plays": 70, "pass_plays": 35, "pass_epa": 3.5, "rush_plays": 35, "rush_epa": 1.0,
            "explosive_plays": 8, "neutral_plays": 40, "neutral_passes": 20, "seconds_sum": 1800.0,
            "clocked_plays": 70, **extra}


def season_rows():
    rng = random.Random(7)
    teams = [f"T{i:02d}" for i in range(12)]
    rows, game = [], 0
    for day in range(1, 12):
        shuffled = teams[:]
        rng.shuffle(shuffled)
        for a, b in zip(shuffled[::2], shuffled[1::2]):
            game += 1
            rows.append(obs(game, day, a, b, rng.randint(10, 45), 0.5, pass_epa=rng.uniform(-5, 9)))
            rows.append(obs(game, day, b, a, rng.randint(10, 45), -0.5, pass_epa=rng.uniform(-5, 9)))
    return teams, rows


def test_the_vectorised_solver_matches_the_nfl_solver():
    teams, rows = season_rows()
    for metric in ("points", "pass_epa", "pass_rate", "pace"):
        fast = cs.solve(rows, metric, teams, prior_k=6.0)
        slow = nfl_styles.solve(rows, metric, teams, prior_k=6.0)
        assert fast["mu"] == pytest.approx(slow["mu"], abs=1e-9)
        for team in teams:
            assert fast["off"][team] == pytest.approx(slow["off"][team], abs=1e-9)
            assert fast["def"][team] == pytest.approx(slow["def"][team], abs=1e-9)


def test_ratings_only_use_games_before_the_cutoff():
    teams, rows = season_rows()
    early = cs.ratings_before(rows, 2025, "2025-09-04T00:00:00Z")
    late = cs.ratings_before(rows, 2025, "2025-09-30T00:00:00Z")
    assert early["points"]["games"] < late["points"]["games"]
    assert cs.ratings_before(rows, 2025, "2025-09-01T00:00:00Z")["points"]["games"] == 0


def test_categories_are_relative_to_fbs_teams_only():
    teams, rows = season_rows()
    rows += [obs(900 + i, 5, "FCS A", t, 3, 0.5, conference="Southland") for i, t in enumerate(teams[:2])]
    ratings = cs.ratings_before(rows, 2025, "2025-12-01T00:00:00Z")
    assert "FCS A" in ratings["points"]["off"]                # rated, because FBS teams played it
    fbs = cs.fbs_teams(rows, 2025)
    assert "FCS A" not in fbs
    restricted = cs.only(ratings, fbs)
    assert set(restricted["points"]["off"]) == fbs
    assert set(cs.classify(restricted)) == fbs


def test_a_game_with_an_fcs_team_has_no_style_panel(monkeypatch):
    monkeypatch.setattr(cs, "snapshot_for", lambda *a, **k: {
        "ratings": {"points": {"off": {"A": 0.0}, "def": {"A": 0.0}}}, "labels": {}, "fbs": {"A"}})
    game = {"away_team": "FCS", "home_team": "A", "season": 2025, "start_date": "2025-09-06T16:00:00Z"}
    packet = style_clash.build(object(), game)
    assert packet["available"] is False and "FBS" in packet["reason"]


def test_the_panel_template_renders_nothing_when_unavailable():
    from flask import Flask, render_template_string
    app = Flask(__name__, template_folder="../templates")
    app.jinja_env.filters["cell"] = lambda v, f="text": "—" if v is None else str(v)
    template = "{% from '_cfb_style_clash.html' import cfb_style_clash %}{{ cfb_style_clash(sc, game, None) }}"
    with app.app_context():
        html = render_template_string(template, sc={"available": False, "reason": "x"},
                                      game={"away_team": "A", "home_team": "B"})
        none_html = render_template_string(template, sc=None, game={"away_team": "A", "home_team": "B"})
    assert "style-clash" not in html and "style-clash" not in none_html
