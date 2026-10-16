"""NFL picks are judged at the best tracked-book line stored with them; older picks keep the consensus grading."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3

from app import create_app
from sports_aggregator import best_lines, odds_api_tracker as tracker
from sports_aggregator.nfl import pick_record
from sports_aggregator.nfl.forecast_ledger import freeze_dashboard, grading_report
from sports_aggregator.nfl.models import Game
from sports_aggregator.nfl.repository import NFLRepository


def _game(game_id="g1", *, away=20, home=23, spread=3.0, total=44.0, date="2026-09-01"):
    return Game(
        game_id=game_id, season=2026, season_type="REG", week=1, game_date=date, game_time="20:00",
        away_team="A", home_team="B", away_score=away, home_score=home, overtime=False, division_game=False,
        stadium=None, roof=None, surface=None, temperature=None, wind=None, spread_line=spread, total_line=total)


def _dashboard(game_id="g1", *, ats="B -3", total_pick="Over 45", su="B", market_margin=3.0, market_total=45.0):
    return {"version": "t", "games": [{
        "game_id": game_id, "season": 2026, "week": 1, "away_team": "A", "home_team": "B",
        "football_lab": {"away_points": 20.0, "home_points": 26.0, "margin": 6.0, "total": 47.0},
        "market_anchor": {"margin": market_margin, "total": market_total},
        "straight_up_pick": su, "ats_pick": ats, "total_pick": total_pick, "narrative_tags": []}]}


def _repository(tmp_path, **game_kwargs):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    repository.replace_games(2026, [_game(**game_kwargs)])
    with closing(repository._connect()) as c:
        c.executescript(tracker.LINES_SCHEMA)
        c.commit()
    return repository


def _line(repository, book, captured, home_spread, total, *, game_id="g1"):
    with closing(repository._connect()) as c:
        c.execute(
            "INSERT INTO odds_api_lines (captured_at,league,event_id,commence_time,game_id,home_team,away_team,bookmaker,"
            "home_spread,home_spread_price,away_spread_price,total,over_price,under_price,book_updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (captured, "nfl", "e1", "2026-09-01T00:00:00Z", game_id, "B", "A", book, home_spread, -110, -110,
             total, -110, -110, captured))
        c.commit()


def _ledger_row(repository):
    with closing(repository._connect()) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM nfl_engine_forecasts ORDER BY forecast_id DESC LIMIT 1").fetchone())


def _moneylines(repository):
    # B (home, the SU pick) pays +/-: DraftKings -150, FanDuel -140 (best for B), BetMGM -160; A pays +120, +125 (best), +115
    with closing(repository._connect()) as c:
        for book, home, away in (("draftkings", -150, 120), ("fanduel", -140, 115), ("betmgm", -160, 125)):
            c.execute("UPDATE odds_api_lines SET home_moneyline=?, away_moneyline=? WHERE bookmaker=?", (home, away, book))
        c.commit()


def _books(repository):
    # B is the home favourite: DraftKings -3.0, FanDuel -2.5 (best for B), BetMGM -3.5 (best for A)
    _line(repository, "draftkings", "2026-08-30T10:00:00+00:00", -3.0, 44.5)
    _line(repository, "fanduel", "2026-08-30T10:05:00+00:00", -2.5, 44.0)
    _line(repository, "betmgm", "2026-08-30T10:10:00+00:00", -3.5, 45.0)


ISSUED = "2026-08-30T12:00:00Z"


def test_a_home_pick_freezes_the_best_home_number_in_the_ledger_sign(tmp_path):
    repository = _repository(tmp_path)
    _books(repository)
    freeze_dashboard(repository, _dashboard(ats="B -3", total_pick="Over 45"), generated_at=ISSUED)
    row = _ledger_row(repository)
    # B gets -2.5 at best (FanDuel); the ledger keeps nflverse's sign (positive = home favored), so +2.5
    assert (row["best_ats_spread"], row["best_ats_book"]) == (2.5, "FanDuel")
    assert (row["best_total"], row["best_total_book"]) == (44.0, "FanDuel")          # over wants the lowest total


def test_an_away_pick_freezes_the_best_away_number_and_the_under_the_highest_total(tmp_path):
    repository = _repository(tmp_path)
    _books(repository)
    freeze_dashboard(repository, _dashboard(ats="A +3", total_pick="Under 45", su="A"), generated_at=ISSUED)
    row = _ledger_row(repository)
    # BetMGM lays B -3.5, so A gets +3.5 there, the most any book offers A. The ledger keeps A's own spread for an
    # away pick, which is already in nflverse's sign (home favored by 3.5 = positive 3.5).
    assert (row["best_ats_spread"], row["best_ats_book"]) == (3.5, "BetMGM")
    assert (row["best_total"], row["best_total_book"]) == (45.0, "BetMGM")


def test_lines_that_arrived_after_the_pick_do_not_count(tmp_path):
    repository = _repository(tmp_path)
    _line(repository, "fanduel", "2026-08-30T13:00:00+00:00", -1.0, 40.0)           # an hour after issue
    freeze_dashboard(repository, _dashboard(), generated_at=ISSUED)
    row = _ledger_row(repository)
    assert (row["best_ats_spread"], row["best_total"]) == (None, None)


def test_a_pick_with_no_tracked_lines_keeps_the_consensus_line(tmp_path):
    repository = _repository(tmp_path)
    freeze_dashboard(repository, _dashboard(), generated_at=ISSUED)
    row = _ledger_row(repository)
    assert row["best_ats_spread"] is None and row["spread_at_issue"] == 3.0


def test_the_best_line_grades_the_pick_and_consensus_is_the_fallback(tmp_path):
    # B wins by exactly 3: a push at the consensus 3.0, a win at the best 2.5
    best_repo = _repository(tmp_path)
    _books(best_repo)
    freeze_dashboard(best_repo, _dashboard(), generated_at=ISSUED)
    issuance = grading_report(best_repo, season=2026)["issuances"][0]
    assert (issuance["ats_grade"], issuance["ats_line_basis"]) == ("win", "best")

    (tmp_path / "plain").mkdir()
    plain = _repository(tmp_path / "plain")
    freeze_dashboard(plain, _dashboard(), generated_at=ISSUED)
    issuance = grading_report(plain, season=2026)["issuances"][0]
    assert (issuance["ats_grade"], issuance["ats_line_basis"]) == ("push", "consensus")


def test_totals_are_graded_at_the_best_total(tmp_path):
    # actual total 43: the over loses at 45 and at 44.0; make it 44.5 so best 44.0 wins and consensus 45 loses
    repository = _repository(tmp_path, away=20, home=24.5)
    _books(repository)
    freeze_dashboard(repository, _dashboard(total_pick="Over 45"), generated_at=ISSUED)
    issuance = grading_report(repository, season=2026)["issuances"][0]
    assert (issuance["total_grade"], issuance["total_line_basis"]) == ("win", "best")


def test_the_best_moneyline_for_the_straight_up_team_is_stored_and_priced(tmp_path):
    repository = _repository(tmp_path)
    _books(repository)
    _moneylines(repository)
    freeze_dashboard(repository, _dashboard(su="B"), generated_at=ISSUED)
    row = _ledger_row(repository)
    assert (row["best_su_price"], row["best_su_book"]) == (-140.0, "FanDuel")          # B: the highest of -150/-140/-160
    issuance = grading_report(repository, season=2026)["issuances"][0]
    assert (issuance["su_price_basis"], issuance["su_price_used"]) == ("best", -140.0)


def test_an_away_straight_up_pick_takes_the_best_away_moneyline(tmp_path):
    repository = _repository(tmp_path)
    _books(repository)
    _moneylines(repository)
    freeze_dashboard(repository, _dashboard(ats="A +3", su="A"), generated_at=ISSUED)
    row = _ledger_row(repository)
    assert (row["best_su_price"], row["best_su_book"]) == (125.0, "BetMGM")


def test_straight_up_units_use_the_best_price_only_when_one_was_stored(tmp_path):
    # B wins 23-20 at -140 (best) vs the consensus price stored at issue
    repository = _repository(tmp_path)
    _books(repository)
    _moneylines(repository)
    freeze_dashboard(repository, _dashboard(su="B"), generated_at=ISSUED)
    best = pick_record.build(repository, 2026)
    assert best["best_line"]["su"] == 1
    assert best["straight_up"]["units"] == round(100 / 140, 2)          # one win at -140

    (tmp_path / "plain").mkdir()
    plain_repo = _repository(tmp_path / "plain")
    freeze_dashboard(plain_repo, _dashboard(su="B"), generated_at=ISSUED)
    plain = pick_record.build(plain_repo, 2026)
    assert plain["best_line"]["su"] == 0
    assert plain["straight_up"]["priced"] in (0, 1)                      # no stored price at all here, so none can be priced


def test_the_record_says_how_many_picks_were_judged_at_the_best_line(tmp_path):
    repository = _repository(tmp_path)
    _books(repository)
    freeze_dashboard(repository, _dashboard(), generated_at=ISSUED)
    record = pick_record.build(repository, 2026)
    assert record["best_line"] == {"ats": 1, "ats_of": 1, "totals": 1, "totals_of": 1, "su": 0, "su_of": 1}
    assert "best tracked-book line" in record["policy"]["line"]


def test_the_record_page_renders_with_the_best_line_note(tmp_path):
    repository = _repository(tmp_path)
    _books(repository)
    freeze_dashboard(repository, _dashboard(), generated_at=ISSUED)
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False, "NFL_DATABASE_PATH": str(repository.path),
                      "CFB_DATABASE_PATH": str(tmp_path / "cfb.sqlite3")})
    page = app.test_client().get("/nfl/picks/record/?season=2026")
    assert page.status_code == 200
    assert b"judged at the best tracked-book line" in page.data


def test_espn_draftkings_is_a_fallback_with_the_sign_flipped(tmp_path):
    repository = _repository(tmp_path)
    with closing(repository._connect()) as c:
        c.row_factory = sqlite3.Row
        c.execute("DELETE FROM espn_market_lines WHERE game_id='g1'")
        c.execute(
            "INSERT INTO espn_market_lines (game_id,season,week,event_id,provider,final,open_spread,open_total,"
            "open_home_moneyline,open_away_moneyline,current_spread,current_total,current_home_moneyline,"
            "current_away_moneyline,fetched_at) VALUES ('g1',2026,1,'x','DraftKings',0,3.5,45.0,NULL,NULL,3.0,44.5,NULL,NULL,?)",
            (datetime.now(timezone.utc).isoformat(),))
        c.commit()
        rows = best_lines.espn_rows(c, "g1")
    assert [(r["book"], r["home_spread"], r["opened_spread"], r["total"]) for r in rows] == [("draftkings", -3.0, -3.5, 44.5)]


def test_a_live_pick_uses_the_espn_fallback_but_an_old_issue_time_does_not(tmp_path):
    repository = _repository(tmp_path)
    with closing(repository._connect()) as c:
        c.execute("DELETE FROM espn_market_lines WHERE game_id='g1'")
        c.execute(
            "INSERT INTO espn_market_lines (game_id,season,week,event_id,provider,final,open_spread,open_total,"
            "open_home_moneyline,open_away_moneyline,current_spread,current_total,current_home_moneyline,"
            "current_away_moneyline,fetched_at) VALUES ('g1',2026,1,'x','DraftKings',0,3.5,45.0,NULL,NULL,3.0,44.5,NULL,NULL,?)",
            (datetime.now(timezone.utc).isoformat(),))
        c.commit()
    freeze_dashboard(repository, _dashboard(), generated_at=ISSUED)           # months ago: today's ESPN line is not that
    assert _ledger_row(repository)["best_ats_spread"] is None
    # a moment ago: issued now, and the fallback is today's line
    repository.replace_games(2026, [_game(date=(datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat(),
                                          away=None, home=None)])
    # different numbers, so the ledger stores a new issuance rather than skipping an unchanged one
    freeze_dashboard(repository, _dashboard(market_margin=3.5), generated_at=datetime.now(timezone.utc).isoformat())
    row = _ledger_row(repository)
    assert (row["best_ats_spread"], row["best_ats_book"]) == (3.0, "DraftKings")


def test_an_existing_ledger_gains_the_new_columns_and_keeps_its_rows(tmp_path):
    repository = _repository(tmp_path)
    freeze_dashboard(repository, _dashboard(), generated_at=ISSUED)
    with closing(sqlite3.connect(repository.path)) as c:
        for name in ("best_ats_spread", "best_ats_book", "best_total", "best_total_book"):
            c.execute(f"ALTER TABLE nfl_engine_forecasts DROP COLUMN {name}")
        c.commit()
    repository._initialize_schema()                      # initialize() is memoised once per process
    row = _ledger_row(repository)
    assert row["spread_at_issue"] == 3.0
    assert {"best_ats_spread", "best_ats_book", "best_total", "best_total_book"} <= set(row)


def test_the_nfl_book_table_moves_from_espns_true_open(tmp_path):
    repository = _repository(tmp_path)
    _books(repository)                                   # the tracker first saw DraftKings at -3.0 / 44.5
    with closing(repository._connect()) as c:
        c.row_factory = sqlite3.Row
        c.execute("DELETE FROM espn_market_lines WHERE game_id='g1'")
        c.execute(
            "INSERT INTO espn_market_lines (game_id,season,week,event_id,provider,final,open_spread,open_total,"
            "open_home_moneyline,open_away_moneyline,current_spread,current_total,current_home_moneyline,"
            "current_away_moneyline,fetched_at) VALUES ('g1',2026,1,'x','DraftKings',0,4.5,47.5,NULL,NULL,3.0,44.5,NULL,NULL,?)",
            (datetime.now(timezone.utc).isoformat(),))
        c.commit()
        view = best_lines.display_rows(c, "g1", fallback=best_lines.espn_rows(c, "g1"))
    rows = {r["book"]: r for r in view["rows"]}
    # ESPN stores positive = home favored, so an open of 4.5 is a home spread of -4.5; now -3.0 is a move of +1.5
    assert (rows["draftkings"]["spread_move"], rows["draftkings"]["total_move"]) == (1.5, -3.0)
    assert rows["fanduel"]["spread_move"] == 0.0         # no ESPN opener for FanDuel: first-seen


def test_the_best_number_note_renders_on_its_own(tmp_path):
    repository = _repository(tmp_path)
    _books(repository)
    with closing(repository._connect()) as c:
        c.row_factory = sqlite3.Row
        view = best_lines.display_rows(c, "g1")
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False, "NFL_DATABASE_PATH": str(repository.path),
                      "CFB_DATABASE_PATH": str(tmp_path / "cfb.sqlite3")})
    html = app.jinja_env.from_string(
        "{% from '_ui_kit.html' import best_line_note %}{{ best_line_note(view, 'B', 'A') }}").render(view=view)
    assert "Best number on offer" in html and "<table" not in html
    assert app.jinja_env.from_string(
        "{% from '_ui_kit.html' import best_line_note %}{{ best_line_note(none, 'B', 'A') }}").render().strip() == ""


def test_the_shared_table_marks_the_best_numbers_on_both_pages(tmp_path):
    repository = _repository(tmp_path)
    _books(repository)
    with closing(repository._connect()) as c:
        c.row_factory = sqlite3.Row
        view = best_lines.display_rows(c, "g1")
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False, "NFL_DATABASE_PATH": str(repository.path),
                      "CFB_DATABASE_PATH": str(tmp_path / "cfb.sqlite3")})
    html = app.jinja_env.from_string(
        "{% from '_ui_kit.html' import book_lines_panel %}{{ book_lines_panel(view, 'B', 'A') }}").render(view=view)
    assert "FanDuel" in html and "BetMGM" in html and "DraftKings" in html
    assert 'class="num best-line"' in html
    assert "New picks are judged against these numbers" in html
    assert app.jinja_env.from_string(
        "{% from '_ui_kit.html' import book_lines_panel %}{{ book_lines_panel(none, 'B', 'A') }}").render().strip() == ""
