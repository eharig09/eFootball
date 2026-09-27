from app import create_app


def test_injury_popover_has_readable_sections_and_accessible_relationship():
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})
    template = app.jinja_env.from_string(
        "{% from '_nfl_injury.html' import injury_popover %}"
        "{{ injury_popover(injury, 'injury player/1', true) }}"
    )
    injury = {
        "designation": "Q",
        "designation_class": "questionable",
        "designation_label": "Questionable",
        "detail_line": "Left calf · Limited practice",
        "short_comment": "Exited Thursday practice.",
        "long_comment": "The team will evaluate him before kickoff.",
        "return_date": "2026-10-04",
        "report_date": "2026-09-30",
        "note_source": "ESPN",
    }

    markup = template.render(injury=injury)

    assert 'aria-describedby="injury-player-1"' in markup
    assert 'id="injury-player-1" role="tooltip"' in markup
    assert 'class="injury-tooltip-head"' in markup
    assert 'class="injury-tooltip-summary"' in markup
    assert 'class="injury-tooltip-detail"' in markup
    assert 'class="injury-tooltip-meta"' in markup
    assert "Last updated" in markup


def test_portal_preserves_vertical_injury_layout():
    script = open("static/tooltips.js", encoding="utf-8").read()
    css = open("static/nfl_components.css", encoding="utf-8").read()

    assert 'setProperty("flex-direction", "column", "important")' in script
    assert ".injury-tooltip-head{" in css
    assert ".injury-tooltip-summary,.injury-tooltip-detail{" in css
