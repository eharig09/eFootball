from contextlib import closing

from app import create_app
from sports_aggregator.nfl import pick_record
from sports_aggregator.nfl.forecast_ledger import freeze_dashboard
from sports_aggregator.nfl.models import Game
from sports_aggregator.nfl.repository import NFLRepository


def _game(game_id, week, away_score, home_score, spread, total, date="2026-09-01"):
    return Game(
        game_id=game_id, season=2026, season_type="REG", week=week,
        game_date=date, game_time="20:00", away_team="A", home_team="B",
        away_score=away_score, home_score=home_score, overtime=False,
        division_game=False, stadium=None, roof=None, surface=None,
        temperature=None, wind=None, spread_line=spread, total_line=total,
    )


def _dashboard(game_id, week, *, margin, total, market_margin, market_total,
               ats="B -3", total_pick="Over 45", su="B"):
    return {"version": "t", "games": [{
        "game_id": game_id, "season": 2026, "week": week, "away_team": "A", "home_team": "B",
        "football_lab": {"away_points": 20.0, "home_points": 20.0 + margin,
                         "margin": margin, "total": total},
        "market_anchor": {"margin": market_margin, "total": market_total},
        "straight_up_pick": su, "ats_pick": ats, "total_pick": total_pick,
        "narrative_tags": [],
    }]}


def _seeded(tmp_path, *, games=1):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    repository.replace_games(2026, [
        _game(f"g{i}", i, 20, 27, spread=4.0, total=44.0) for i in range(1, games + 1)])
    for i in range(1, games + 1):
        # First issue: B -3, over 45. A later reissue at a different price must not double count.
        freeze_dashboard(repository, _dashboard(f"g{i}", i, margin=6.0, total=47.0,
                                                market_margin=3.0, market_total=45.0),
                         generated_at="2026-08-30T12:00:00Z")
        freeze_dashboard(repository, _dashboard(f"g{i}", i, margin=6.5, total=47.0,
                                                market_margin=3.5, market_total=45.0),
                         generated_at="2026-08-31T12:00:00Z")
    return repository


def test_each_game_counts_once_at_its_first_stored_pick(tmp_path):
    record = pick_record.build(_seeded(tmp_path), 2026)

    # Home won 27-20: B -3 covers at the first price. The 3.5 reissue is not counted again.
    assert record["ats"]["record"] == "1-0"
    assert record["frozen"] == 2
    assert record["ats"]["units"] == round(100 / 110, 2)


def test_closing_line_value_uses_direction_of_the_pick(tmp_path):
    record = pick_record.build(_seeded(tmp_path), 2026)

    # Home pick at 3.0, market closed at 4.0 -> got a better number by 1.0.
    assert record["clv"]["ats"]["mean"] == 1.0
    assert record["clv"]["ats"]["beat_close"] == 1
    # Over at 45, total closed at 44 -> worse number by 1.0.
    assert record["clv"]["totals"]["mean"] == -1.0
    assert record["clv"]["totals"]["lost_to_close"] == 1


def test_rates_and_charts_wait_for_a_settled_sample(tmp_path):
    small = pick_record.build(_seeded(tmp_path), 2026)
    assert small["settled"] is False

    (tmp_path / "big").mkdir()
    big = pick_record.build(_seeded(tmp_path / "big", games=pick_record.MIN_GRADED), 2026)
    assert big["settled"] is True
    assert big["ats"]["n"] == pick_record.MIN_GRADED
    assert big["units_chart"]["has_data"] is True


def test_ledger_refuses_forecasts_issued_after_kickoff(tmp_path):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    repository.replace_games(2026, [_game("g1", 1, None, None, spread=3.0, total=45.0)])
    dashboard = _dashboard("g1", 1, margin=6.0, total=47.0, market_margin=3.0, market_total=45.0)

    # Kickoff is 20:00 ET (00:00Z next day): 01:00Z is after it.
    assert freeze_dashboard(repository, dashboard, generated_at="2026-09-02T01:00:00Z") == 0
    assert freeze_dashboard(repository, dashboard, generated_at="2026-09-01T18:00:00Z") == 1
    with closing(repository._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM nfl_engine_forecasts").fetchone()[0] == 1


def test_wilson_interval_is_wide_at_small_n():
    low, high = pick_record.wilson(3, 3)
    assert low < 0.5 and high == 1.0  # 3-0 is not evidence of a 90% bettor
    assert pick_record.wilson(0, 0) is None


def test_record_page_renders_for_an_empty_and_a_seeded_ledger(tmp_path):
    for repository in (NFLRepository(tmp_path / "empty.sqlite3"), _seeded(tmp_path)):
        app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                          "NFL_REPOSITORY": repository})
        client = app.test_client()
        page = client.get("/nfl/picks/record/?season=2026")
        assert page.status_code == 200
        assert b"Football Lab pick record" in page.data
        assert client.get("/api/v1/nfl/picks/record?season=2026").status_code == 200


def test_settled_record_page_shows_charts(tmp_path):
    repository = _seeded(tmp_path, games=pick_record.MIN_GRADED)
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "NFL_REPOSITORY": repository})
    page = app.test_client().get("/nfl/picks/record/?season=2026")
    assert page.status_code == 200
    assert b"<polyline" in page.data and b"Building sample" not in page.data


def test_american_profit_pays_the_real_price():
    assert pick_record.american_profit(-300) == 100 / 300
    assert pick_record.american_profit(150) == 1.5


def test_su_and_total_units_use_the_stored_price_not_flat_110():
    picks = [
        {"g": "win", "p": -300.0}, {"g": "win", "p": 150.0},
        {"g": "loss", "p": -200.0}, {"g": "win", "p": None},
    ]
    tally = pick_record._tally(picks, "g", "p")
    assert tally["record"] == "3-1"
    assert tally["priced"] == 3
    assert tally["units"] == round(1 / 3 + 1.5 - 1, 2)
    flat = pick_record._tally(picks, "g")
    assert flat["units"] == round(3 * 100 / 110 - 1, 2)


def test_freeze_stores_moneyline_of_the_picked_side_and_backfill_matches(tmp_path):
    import sqlite3
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    repository.replace_games(2026, [_game("g1", 1, 20, 27, spread=4.0, total=44.0)])
    with closing(repository._connect()) as connection:
        connection.execute(
            "INSERT INTO market_line_snapshots (game_id,season,week,captured_at,source,spread_line,total_line,"
            "away_moneyline,home_moneyline,under_odds,over_odds) VALUES ('g1',2026,1,'2026-08-30T00:00:00+00:00','t',"
            "4.0,44.0,220,-260,-105,-115)")
        connection.commit()
    freeze_dashboard(repository, _dashboard("g1", 1, margin=6.0, total=47.0, market_margin=3.0, market_total=45.0),
                     generated_at="2026-08-31T00:00:00+00:00")
    with closing(repository._connect()) as connection:
        row = connection.execute("SELECT su_price_at_issue,total_price_at_issue FROM nfl_engine_forecasts").fetchone()
        assert (row[0], row[1]) == (-260.0, -115.0)
        connection.execute("UPDATE nfl_engine_forecasts SET su_price_at_issue=NULL,total_price_at_issue=NULL")
        from sports_aggregator.nfl.repository import backfill_issue_prices
        assert backfill_issue_prices(connection) == 1
        row = connection.execute("SELECT su_price_at_issue,total_price_at_issue FROM nfl_engine_forecasts").fetchone()
        assert (row[0], row[1]) == (-260.0, -115.0)
