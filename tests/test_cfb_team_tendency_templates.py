"""The shared UI-kit macros, rendered the way the college football team page uses them."""
import os

os.environ.setdefault("REGISTER_LEGACY_DASHBOARDS", "0")
os.environ.setdefault("NFL_AUTO_SEED", "0")

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from sports_aggregator.nfl.penalties import profiles_from_rows, panel_rows  # noqa: E402
from sports_aggregator.nfl.situational_tendencies import build_grid  # noqa: E402


@pytest.fixture(scope="module")
def env():
    return create_app({"TESTING": True}).jinja_env


def _render(env, source, **context):
    return env.from_string('{% from "_ui_kit.html" import tendency_grid, side_panel, penalty_panels %}' + source).render(**context)


def _penalty_rows():
    flag = dict(penalty_type="False Start", yards=5.0, epa_team=None, auto_first_down=0, no_play=True,
                player_id=None, player_name="Cam East")
    return [dict(flag, team="Tulsa", opponent="Air Force", week=1),
            dict(flag, team="Air Force", opponent="Tulsa", week=1, penalty_type="Holding", yards=10.0, player_name=None)]


def test_penalty_panels_without_epa_hide_the_row_and_header_text(env):
    profiles = profiles_from_rows(_penalty_rows(), {"Tulsa": {"g"}, "Air Force": {"g"}}, 2026)
    profile = profiles["Tulsa"]
    committed = [r for r in panel_rows(profile, "committed") if r["key"] != "epa"]
    drawn = [r for r in panel_rows(profile, "drawn") if r["key"] != "epa"]
    html = _render(env, "{{ penalty_panels(profile, committed, drawn, '', false) }}",
                   profile=profile, committed=committed, drawn=drawn)
    assert "Self-inflicted" in html and "Opponent gifts" in html and "False Start" in html
    assert "EPA cost" not in html and "no-play EPA" not in html and "1 flags" in html
    shown = _render(env, "{{ penalty_panels(profile, committed, drawn) }}", profile=profile,
                    committed=panel_rows(profile, "committed"), drawn=panel_rows(profile, "drawn"))
    assert "EPA cost" in shown and "no-play EPA" in shown        # NFL default is unchanged


def test_side_panel_college_labels_and_nfl_defaults(env):
    section = {"has_data": True, "total": 40, "ratio": "1.17 : 1", "league_ratio": "0.84 : 1",
               "rows": [{"bucket": "Left", "share": .5, "league_share": .4, "n": 20, "epa_per": .1, "epa_class": "is-good",
                         "yards_per": 6.0, "completion_rate": .6, "adot": 7.0, "success": .5}],
               "matrix": [{"label": "Short", "cells": [{"n": 5, "epa_per": .2, "epa_class": None, "heat": None}] * 3}]}
    college = _render(env, "{{ side_panel('Throws', s, true, '40 charted', 'left : right', 'Depth', 'Direction') }}", s=section)
    assert "left : right" in college and ">Direction<" in college and ">Depth<" in college and "Short" in college
    assert "field : boundary" not in college and "Ball starts" not in college
    nfl = _render(env, "{{ side_panel('Throws', s, true) }}", s=section)
    assert "field : boundary" in nfl and "Ball starts" in nfl and ">Side<" in nfl
    empty = _render(env, "{{ side_panel('Runs', s, false, '', 'left : right', 'Depth', 'Direction', 'Too few runs.') }}",
                    s={"has_data": False})
    assert "Too few runs." in empty
    no_grid = _render(env, "{{ side_panel('Runs', s, false) }}", s={**section, "matrix": []})
    assert "ui-matrix" not in no_grid                              # runs have no depth grid


def test_tendency_grid_renders_the_college_profile(env):
    mine = {(3, "long"): [20, 17, 2.0, 9], (1, "medium"): [30, 12, 3.0, 15]}
    profile = build_grid(mine, {(3, "long"): 0.77, (1, "medium"): 0.44}, "Tulsa", 2026)
    html = _render(env, "{{ tendency_grid(p, 'Pass / rush mix by down and distance', '2025 baseline') }}", p=profile)
    assert "3rd &amp; 7+" in html or "3rd & 7+" in html
    assert "PASS 85.0%" in html and "RUN 60.0%" in html and "2025 baseline" in html
