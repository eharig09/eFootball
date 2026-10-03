from app import create_app
from sports_aggregator.nfl import odds_history
from sports_aggregator.nfl.models import Game
from sports_aggregator.nfl.repository import NFLRepository


def _item(open_home="-8.5", now_home="-3.5", open_total="45.5", now_total="43.5"):
    def side(open_line, now_line, open_ml, now_ml, odds):
        return {"moneyLine": now_ml, "spreadOdds": odds,
                "open": {"pointSpread": {"american": open_line}, "moneyLine": {"american": open_ml}},
                "current": {"pointSpread": {"american": now_line}, "moneyLine": {"american": str(now_ml)}}}
    flip = lambda line: ("+" + line[1:]) if line.startswith("-") else ("-" + line[1:])  # noqa: E731
    return {"provider": {"name": "DraftKings"}, "overOdds": 100.0, "underOdds": -120.0,
            "homeTeamOdds": side(open_home, now_home, "-380", -185, -112.0),
            "awayTeamOdds": side(flip(open_home), flip(now_home), "+300", 154, -108.0),
            "open": {"total": {"american": open_total}},
            "current": {"total": {"american": now_total}}}


def test_parse_odds_uses_nflverse_sign_and_keeps_unknowns_unknown():
    row = odds_history.parse_odds(_item())

    # Home favored by 8.5 at open and 3.5 now -> positive in nflverse terms.
    assert (row["open_spread"], row["current_spread"]) == (8.5, 3.5)
    assert (row["open_total"], row["current_total"]) == (45.5, 43.5)
    assert row["open_home_moneyline"] == -380.0 and row["current_away_moneyline"] == 154.0
    assert odds_history.parse_odds({})["open_spread"] is None
    assert odds_history.parse_odds(_item(open_home="PK", now_home="PK"))["open_spread"] == 0.0
    # Pick'em must not collapse into "missing", nor an absent line into zero.
    assert odds_history._number("o45.5") == 45.5 and odds_history._number("") is None


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Session:
    def __init__(self, item):
        self.item, self.calls = item, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if "scoreboard" in url:
            return _Response({"events": [{
                "id": "900", "status": {"type": {"completed": False}},
                "competitions": [{"competitors": [
                    {"homeAway": "home", "team": {"abbreviation": "B"}},
                    {"homeAway": "away", "team": {"abbreviation": "A"}}]}]}]})
        return _Response({"items": [self.item]})


def _repo(tmp_path, completed=False):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    repository.replace_games(2026, [Game(
        game_id="g1", season=2026, season_type="REG", week=4, game_date="2026-10-04",
        game_time="13:00", away_team="A", home_team="B",
        away_score=17 if completed else None, home_score=24 if completed else None,
        overtime=False, division_game=False, stadium=None, roof=None, surface=None,
        temperature=None, wind=None, spread_line=3.5, total_line=43.5)])
    return repository


def test_sync_stores_open_and_current_and_only_snapshots_changes(tmp_path):
    repository = _repo(tmp_path)
    session = _Session(_item())

    assert odds_history.sync_market_lines(repository, 2026, [4], session=session)["changed"] == 1
    assert odds_history.sync_market_lines(repository, 2026, [4], session=session)["changed"] == 0
    session.item = _item(now_home="-4.5")
    assert odds_history.sync_market_lines(repository, 2026, [4], session=session)["changed"] == 1

    stored = repository.espn_market("g1")
    assert (stored["open_spread"], stored["current_spread"]) == (8.5, 4.5)
    espn = [s for s in repository.market_line_history("g1") if s["source"] == "espn"]
    assert [s["spread_line"] for s in espn] == [3.5, 4.5]


def test_settled_games_are_not_refetched(tmp_path):
    repository = _repo(tmp_path, completed=True)
    session = _Session(_item())

    odds_history.sync_market_lines(repository, 2026, [4], session=session)
    calls = len(session.calls)
    result = odds_history.sync_market_lines(repository, 2026, [4], session=session)

    assert result["games"] == 0 and len(session.calls) == calls + 1  # scoreboard only


def test_movement_packet_and_game_page_panel(tmp_path):
    repository = _repo(tmp_path)
    session = _Session(_item())
    odds_history.sync_market_lines(repository, 2026, [4], session=session)
    session.item = _item(now_home="-4.5", now_total="44.5")
    odds_history.sync_market_lines(repository, 2026, [4], session=session)

    packet = odds_history.movement_packet(
        repository, repository.schedule(2026)[0], model_margin=6.0, model_total=47.0)

    assert packet["available"] and packet["spread_move"] == -4.0
    assert packet["spread_chart"]["has_data"] and len(packet["spread_chart"]["series"]) == 2
    assert packet["table"].rows[0]["open"] == "+8.5" and packet["table"].rows[0]["move"] == "-4"
    assert odds_history.movement_packet(repository, {"game_id": "none"})["available"] is False

    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "NFL_REPOSITORY": repository})
    page = app.test_client().get("/nfl/games/g1/")
    assert page.status_code == 200 and b"Line movement" in page.data
