from pathlib import Path


def test_projection_tab_prioritizes_points_yards_and_quality():
    template = Path("templates/cfb_game.html").read_text(encoding="utf-8")
    assert "projection-scoreboard" in template
    assert "Projected points" in template
    assert "Total yards" in template
    assert "Quality edge" in template
    assert "Projected total" in template
    assert "Projected margin" in template


def test_projection_detail_is_secondary():
    template = Path("templates/cfb_game.html").read_text(encoding="utf-8")
    assert '<details class="projection-detail">' in template
    assert "<summary>Full projection detail</summary>" in template


def test_projection_ui_has_mobile_layout():
    css = Path("static/pages/cfb_game.css").read_text(encoding="utf-8")
    assert ".projection-scoreboard" in css
    assert ".projection-points" in css
    assert ".projection-quality.is-positive" in css
    assert "@media (max-width: 720px)" in css
