from pathlib import Path

from sports_aggregator.cfb.cfbdepth_data import (
    import_player_updates,
    import_roster_breakdown,
    import_team_impact,
    player_updates,
    roster_breakdown,
    team_impact,
)
from sports_aggregator.cfb.coordinator_balance import coordinator_run_pass_context
from sports_aggregator.cfb.coordinators import initialize as initialize_coordinators
from sports_aggregator.cfb.models import Player, Team
from sports_aggregator.cfb.repository import CFBRepository


def _repo(tmp_path):
    repository = CFBRepository(tmp_path / "cfb.sqlite3")
    repository.initialize()
    return repository


def test_cfbdepth_csv_imports(tmp_path):
    repository = _repo(tmp_path)
    roster_csv = """School,Conference,Active Players,Transfers,Transfer%,Home Grown,Home Grown%,5-Star,4-Star,3-Star,2-Star,0-Star,Blue Chip%,OL Avg Wt,DL Avg Wt,Roster Avg Wt
Alabama,SEC,120,28,23%,92,77%,5,42,60,0,13,39.2%,318.1,292.4,238.7
"""
    impact_csv = """School,Conference,Injury Number,Injury New,OFS,O,D,Q,P,S,GTD,OPT,RET,Injury Impact,Impact PP
Alabama,SEC,7,0,1,2,1,2,2,0,0,0,0,12.0,1.7
"""
    updates_csv = """Abb,Name,Team,Pos,Status,Rating,Impact,New,Last Update,Update
Bama,Jorden Edmonds,Alabama,DB,Questionable,13.6,1.25695,,8/27/2026,Missing time in camp.
"""

    assert import_roster_breakdown(repository, roster_csv) == 1
    assert import_team_impact(repository, impact_csv) == 1
    assert import_player_updates(repository, updates_csv) == 1

    roster = roster_breakdown(repository, "Alabama")
    assert roster["transfers"] == 28
    assert roster["blue_chip_pct"] == 39.2

    impact = team_impact(repository, "Alabama")
    assert impact["injury_impact"] == 12.0
    assert impact["impact_pp"] == 1.7

    updates = player_updates(repository, "Jorden Edmonds", "Alabama")
    assert len(updates) == 1
    assert updates[0]["status"] == "Questionable"
    assert updates[0]["update_text"] == "Missing time in camp."


def test_matchup_flags_render_as_grouped_availability_board(tmp_path):
    """Availability is grouped by team with aligned status and impact fields."""
    from app import create_app
    from sports_aggregator.cfb.cfbdepth_enhancements import _matchup_flags

    repository = _repo(tmp_path)
    repository.replace_players(2026, [
        Player("p1", 2026, "Jordan", "Allen", "Georgia Tech", "WR", 1, 72.0, 190, 3),
    ])
    import_player_updates(repository, """Abb,Name,Team,Pos,Status,Rating,Impact,New,Last Update,Update
GT,Jordan Allen,Georgia Tech,WR,Questionable,80.1,0.2,,8/27/2026,Tweaked an ankle Tuesday.
""")

    app = create_app({"TESTING": True})
    with app.test_request_context():
        markup = str(_matchup_flags(repository, "Colorado", "Georgia Tech", 2026))

    assert 'class="cfbdepth-availability-grid"' in markup
    assert '<h4>Colorado</h4><span>0 flagged</span>' in markup
    assert '<h4>Georgia Tech</h4><span>1 flagged</span>' in markup
    assert 'class="cfbdepth-availability-row status-questionable"' in markup
    assert '<strong class="status">Questionable</strong>' in markup
    assert '<span>WR</span>' in markup
    assert 'title="Tweaked an ankle Tuesday. — Updated 8/27/2026"' in markup

    css = (Path(__file__).resolve().parent.parent / "static" / "cfb.css").read_text(encoding="utf-8")
    row = css.split(".cfbdepth-availability-row {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns" in row, "availability fields must stay aligned"


def test_the_template_insert_modules_no_longer_ship_dead_style_blocks():
    """coordinator_display and the two cfbdepth_* modules each spliced their CSS
    into a `<style>` block the CFB templates dropped when styling moved to
    static/cfb.css. The rules belong to the sheet now; the splice is gone."""
    package = Path(__file__).resolve().parent.parent / "sports_aggregator" / "cfb"
    for name in ("cfbdepth_enhancements.py", "cfbdepth_display.py", "coordinator_display.py"):
        body = (package / name).read_text(encoding="utf-8")
        assert "STYLE_INSERT" not in body and "STYLE_ANCHOR" not in body, name
        assert "</style>" not in body, name

    css = (Path(__file__).resolve().parent.parent / "static" / "cfb.css").read_text(encoding="utf-8")
    for selector in (".cfbdepth-availability-row", ".cfbdepth-update", ".cfbdepth-strip", ".team-facts"):
        assert selector in css, selector


def test_player_update_name_fallback_requires_one_source_team(tmp_path):
    repository = _repo(tmp_path)
    updates_csv = """Abb,Name,Team,Pos,Status,Rating,Impact,New,Last Update,Update
AAA,Alex Smith,Alpha,QB,Questionable,10,1,,8/27/2026,Alpha update.
BBB,Alex Smith,Beta,QB,Out,10,1,,8/27/2026,Beta update.
"""
    import_player_updates(repository, updates_csv)
    assert player_updates(repository, "Alex Smith", "Gamma") == []
    assert player_updates(repository, "Alex Smith", "Alpha")[0]["update_text"] == "Alpha update."


def test_coordinator_run_pass_balance_uses_assigned_seasons(tmp_path):
    repository = _repo(tmp_path)
    repository.replace_teams([
        Team(
            team_id=1, school="Alpha", mascot="A", abbreviation="ALP",
            conference="Test", division=None, classification="fbs",
            color="#000000", alternate_color="#ffffff", logos=(),
            aliases=("Alpha",), venue_id=None, venue_name=None,
        )
    ])
    initialize_coordinators(repository)
    with repository.transaction() as connection:
        connection.execute(
            """INSERT INTO coordinator_seasons(
               season,team_id,team,side,role,coach_name,rating,experience_years,
               source_name,source_url,verified_official,official_source_url,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (2025, 1, "Alpha", "offense", "OC", "Coach One", None, None,
             "test", "https://example.com", 1, None, "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            """INSERT INTO coordinator_seasons(
               season,team_id,team,side,role,coach_name,rating,experience_years,
               source_name,source_url,verified_official,official_source_url,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (2026, 1, "Alpha", "offense", "OC", "Coach One", None, None,
             "test", "https://example.com", 1, None, "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO team_stats VALUES(?,?,?,?,?)",
            (2025, "Alpha", "Test", "rushingAttempts", "600"),
        )
        connection.execute(
            "INSERT INTO team_stats VALUES(?,?,?,?,?)",
            (2025, "Alpha", "Test", "passingAttempts", "400"),
        )

    result = coordinator_run_pass_context(repository, 1, 2026)
    assert result["coach_name"] == "Coach One"
    assert result["program"]["run_pct"] == 60.0
    assert result["program"]["pass_pct"] == 40.0
    assert result["season_splits"][0]["season"] == 2025
