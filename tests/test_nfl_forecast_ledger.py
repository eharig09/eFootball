from sports_aggregator.nfl.forecast_ledger import freeze_dashboard, grading_report
from sports_aggregator.nfl.models import Game
from sports_aggregator.nfl.repository import NFLRepository


def test_forecast_ledger_deduplicates_and_grades_line_at_issue(tmp_path):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    repository.replace_games(2026, [Game(
        game_id="g1", season=2026, season_type="REG", week=1,
        game_date="2026-09-01", game_time="20:00", away_team="A", home_team="B",
        away_score=20, home_score=24, overtime=False, division_game=False,
        stadium=None, roof=None, surface=None, temperature=None, wind=None,
        spread_line=3.0, total_line=45.0,
    )])
    dashboard = {
        "version": "test-v1", "games": [{
            "game_id": "g1", "season": 2026, "week": 1,
            "away_team": "A", "home_team": "B",
            "football_lab": {"away_points": 20.0, "home_points": 25.0,
                             "margin": 5.0, "total": 45.0},
            "market_anchor": {"margin": 3.0, "total": 45.0},
            "straight_up_pick": "B", "ats_pick": "B -3", "total_pick": "Over 45",
            "narrative_tags": ["post_blowout_favorite"],
        }],
    }

    assert freeze_dashboard(repository, dashboard, generated_at="2026-09-01T12:00:00Z") == 1
    assert freeze_dashboard(repository, dashboard, generated_at="2026-09-01T13:00:00Z") == 0
    report = grading_report(repository, season=2026)

    assert report["straight_up"]["wins"] == 1
    assert report["against_spread"]["wins"] == 1
    assert report["totals"]["losses"] == 1
    assert report["issuances"][0]["tags"] == ["post_blowout_favorite"]
