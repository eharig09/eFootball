from contextlib import closing

from app import create_app
from sports_aggregator.nfl import injury_impact
from sports_aggregator.nfl.models import Game
from sports_aggregator.nfl.repository import NFLRepository


def _repo(tmp_path):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    repository.replace_games(2026, [Game(
        game_id="g1", season=2026, season_type="REG", week=4, game_date="2026-10-04",
        game_time="13:00", away_team="A", home_team="B", away_score=None, home_score=None,
        overtime=False, division_game=False, stadium=None, roof=None, surface=None,
        temperature=None, wind=None, spread_line=3.0, total_line=44.0)])
    with closing(repository._connect()) as connection:
        # Prior games: a full-time guard (100%), a rotational WR (40%), a QB.
        for week in range(1, 4):
            for name, position, team, pct in (
                    ("Guard One", "G", "A", 1.0), ("Wide Out", "WR", "A", 0.4),
                    ("Home Back", "CB", "B", 0.5), ("Signal Caller", "QB", "A", 1.0)):
                connection.execute(
                    """INSERT INTO snap_counts (season,week,game_id,player_key,player_name,
                         normalized_name,position,team,opponent,offense_pct,defense_pct)
                       VALUES (2026,?,?,?,?,?,?,?,'X',?,?)""",
                    (week, f"w{week}", name, name, name.lower(), position, team,
                     pct if position != "CB" else 0.0, pct if position == "CB" else 0.0))
        connection.commit()
    return repository


def _report(repository, rows):
    from sports_aggregator.nfl.espn import injury_rows  # noqa: F401  (row shape reference)
    repository.replace_injuries(2026, rows)


def _injury(team, name, position, designation, status):
    return {"season": 2026, "team": team, "injury_id": f"{team}-{name}", "espn_id": f"{team}-{name}",
            "gsis_id": None, "player_name": name, "position": position,
            "designation": designation, "status": status, "injury_type": "Knee",
            "location": None, "detail": None, "side": None, "practice_status": None,
            "return_date": None, "short_comment": None, "long_comment": None,
            "report_date": "2026-10-02", "note_source": None,
            "source_url": "https://example.test"}


def test_index_weights_status_by_typical_snaps_and_skips_qb_and_ir(tmp_path):
    repository = _repo(tmp_path)
    _report(repository, [
        _injury("A", "Guard One", "G", "O", "Out"),               # 1.0 x 1.0
        _injury("A", "Wide Out", "WR", "Q", "Questionable"),      # 0.2 x 0.4
        _injury("A", "Signal Caller", "QB", "O", "Out"),          # modelled elsewhere
        _injury("A", "Long Gone", "LB", "IR", "Injured Reserve"), # already in form
        _injury("A", "Nobody Known", "TE", "O", "Out"),           # no snap history
        _injury("B", "Home Back", "CB", "D", "Doubtful"),         # 0.85 x 0.5
    ])

    team = injury_impact.team_impact(repository, 2026, 4, "A")

    assert team["index"] == round(1.0 + 0.2 * 0.4, 2)
    assert {p["player"] for p in team["players"]} == {"Guard One", "Wide Out"}
    assert {p["player"] for p in team["excluded"]} == {"Signal Caller", "Long Gone"}
    assert [p["player"] for p in team["unrated"]] == ["Nobody Known"]
    assert team["groups"]["Offensive line"] == 1.0
    assert injury_impact.team_impact(repository, 2026, 4, "B")["index"] == round(0.85 * 0.5, 2)


def test_only_games_before_the_target_week_set_a_players_importance(tmp_path):
    repository = _repo(tmp_path)
    with closing(repository._connect()) as connection:
        connection.execute(  # a later, higher-usage game must not leak backwards
            """INSERT INTO snap_counts (season,week,game_id,player_key,player_name,normalized_name,
                 position,team,opponent,offense_pct,defense_pct)
               VALUES (2026,9,'w9','Guard One','Guard One','guard one','G','A','X',0.0,0.0)""")
        connection.commit()

    _report(repository, [_injury("A", "Guard One", "G", "O", "Out")])
    importance, games = injury_impact.SnapHistory(repository, 2026, 4).importance("guard one")

    assert (importance, games) == (1.0, 3)


def test_swing_favours_the_healthier_side_and_adjusts_probability(tmp_path):
    repository = _repo(tmp_path)
    _report(repository, [_injury("A", "Guard One", "G", "O", "Out")])
    game = repository.schedule(2026)[0]

    packet = injury_impact.game_impact(repository, game, model_margin=0.0)

    slope = injury_impact.CALIBRATION["points_per_starter"]
    assert packet["gap"] == 1.0 and packet["home_swing"] == round(slope, 2)
    assert packet["away"]["points"] == round(-slope, 2)
    adjusted = packet["adjusted"]
    assert adjusted["home_win_probability"] == 0.5
    assert adjusted["adjusted_home_win_probability"] > 0.5


def test_no_report_means_no_panel(tmp_path):
    repository = _repo(tmp_path)
    assert injury_impact.game_impact(repository, repository.schedule(2026)[0])["available"] is False


def test_evidence_wording_follows_the_backtest_strength():
    assert injury_impact.evidence_label()["level"] == "supported"
    assert "63%" in injury_impact.evidence_label()["text"]
    weak = dict(injury_impact.CALIBRATION, t=0.9)
    assert injury_impact.evidence_label(weak)["level"] == "weak"
    assert injury_impact.evidence_label({"points_per_starter": None})["level"] == "uncalibrated"


def test_game_page_shows_injury_impact_panel(tmp_path):
    repository = _repo(tmp_path)
    _report(repository, [_injury("A", "Guard One", "G", "O", "Out")])
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "NFL_REPOSITORY": repository})
    client = app.test_client()

    page = client.get("/nfl/games/g1/")
    assert page.status_code == 200
    assert b"Injury impact" in page.data and b"Guard One" in page.data
    assert client.get("/api/v1/nfl/games/g1").status_code == 200


def test_forecast_swing_applies_only_inside_the_window_and_with_evidence(tmp_path):
    from datetime import date

    repository = _repo(tmp_path)
    _report(repository, [_injury("A", "Guard One", "G", "O", "Out")])
    game = repository.schedule(2026)[0]  # kickoff 2026-10-04

    applied = injury_impact.forecast_swing(repository, game, today=date(2026, 10, 2))
    assert applied["status"] == "applied"
    assert applied["swing"] == round(injury_impact.CALIBRATION["points_per_starter"], 2)

    early = injury_impact.forecast_swing(repository, game, today=date(2026, 9, 20))
    assert (early["status"], early["swing"]) == ("too_early", 0.0)

    original = dict(injury_impact.CALIBRATION)
    try:
        injury_impact.CALIBRATION["t"] = 0.5
        weak = injury_impact.forecast_swing(repository, game, today=date(2026, 10, 2))
        assert (weak["status"], weak["swing"]) == ("weak", 0.0)
    finally:
        injury_impact.CALIBRATION.update(original)

    (tmp_path / "x").mkdir()
    empty = _repo(tmp_path / "x")
    assert injury_impact.forecast_swing(empty, empty.schedule(2026)[0],
                                        today=date(2026, 10, 2))["status"] == "no_report"
