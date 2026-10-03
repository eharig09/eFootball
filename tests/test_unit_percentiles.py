from contextlib import closing

from app import create_app
from sports_aggregator.cfb.game_panels import unit_grade_rows
from sports_aggregator.cfb.page_visuals import pff_unit_grade_bars
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.ranked_panels import percentile_in


def test_percentile_uses_the_same_rule_as_the_ranked_panels():
    field = [60.0, 62.0, 64.0, 66.0, 68.0]

    assert percentile_in(68.0, field) == 100        # the best of N
    assert percentile_in(60.0, field) == 0          # the worst of N
    assert percentile_in(64.0, field) == 50
    assert percentile_in(64.0, [64.0, 64.0, 64.0]) == 100   # ties share the better rank
    assert percentile_in(70.0, field) == 100        # above the field
    assert percentile_in(50.0, field) == 0          # below the field: the bottom, never negative
    assert percentile_in(None, field) is None and percentile_in(64.0, []) is None


def _repo(tmp_path):
    repository = CFBRepository(tmp_path / "cfb.sqlite3")
    repository.initialize()
    with closing(repository._connect()) as connection:
        # Four teams. Pass protection (OL/blocking) 60-66; coverage 60-72 on a different scale.
        for team_id, ol, cov in ((1, 66.0, 61.0), (2, 64.0, 66.0), (3, 62.0, 72.0), (4, 60.0, 60.0)):
            for group, dataset, grade in (("OL", "blocking", ol), ("SECONDARY", "coverage", cov)):
                connection.execute(
                    "INSERT INTO pff_position_groups VALUES (2025,?,?,?,?,?,?,?)",
                    (team_id, f"T{team_id}", group, dataset, grade, 8, 4000.0))
        connection.commit()
    return repository


def test_team_units_are_ranked_by_percentile_not_by_raw_grade(tmp_path):
    repository = _repo(tmp_path)
    units = repository.pff_team_units(2, 2025)
    by_label = {u["label"]: u for u in units}

    # 64.0 pass protection is mid-pack (67th pctl) while 66.0 coverage is 67th too -- but the
    # raw grades differ by 2, which is exactly what ranking by percentile removes.
    assert by_label["Pass protection"]["percentile"] == 67
    assert by_label["Coverage"]["percentile"] == 67

    team_one = {u["label"]: u for u in repository.pff_team_units(1, 2025)}
    assert team_one["Pass protection"]["percentile"] == 100 and team_one["Coverage"]["percentile"] == 33
    ordered = [u["label"] for u in repository.pff_team_units(1, 2025)]
    assert ordered == ["Pass protection", "Coverage"]   # 100th ahead of 33rd, though 61 < 66


def test_game_units_carry_a_percentile_for_each_side(tmp_path):
    repository = _repo(tmp_path)

    rows = {u["label"]: u for u in repository.pff_game_units(3, 1, 2025)}

    assert rows["Coverage"]["home_percentile"] == 100 and rows["Coverage"]["away_percentile"] == 33
    panel = {r["unit"]: r for r in unit_grade_rows(list(rows.values()))}
    assert panel["Coverage"]["home"]["percentile"] == 100 and panel["Coverage"]["lead"] == "home"


def test_bars_use_the_percentile_and_fall_back_to_the_grade_scale():
    bars = pff_unit_grade_bars([{"label": "A", "grade": 60.0, "percentile": 90},
                                {"label": "B", "grade": 60.0, "percentile": 0},
                                {"label": "C", "grade": 70.0, "percentile": None}])

    assert bars[0]["bar_pct"] == 90.0
    assert bars[1]["bar_pct"] == 4.0          # a visible sliver, not an empty bar
    assert 4.0 < bars[2]["bar_pct"] <= 100.0


def test_college_team_page_shows_ordinal_percentiles(tmp_path):
    from sports_aggregator.cfb.page_visuals import pff_unit_grade_bars as bars_for

    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False})
    template = app.jinja_env.from_string(
        '{% from "_cfb_page_visuals.html" import pff_unit_bars_module %}{{ pff_unit_bars_module(units) }}')
    units = bars_for(_repo(tmp_path).pff_team_units(1, 2025))

    markup = template.render(units=units)

    assert "100th" in markup and "33rd" in markup and "pctl" in markup
    assert "percentile among FBS teams" in markup
