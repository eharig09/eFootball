from contextlib import closing
import io

import pytest

from app import create_app
from sports_aggregator.cfb.models import normalize_alias
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.nfl.models import Game
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.pff_team_grades import (
    GRADE_KEYS, TeamGradeRow, TeamGradesError, _enforce, _record_check,
    import_cfb, import_nfl, latest_cfb_team_grades, latest_nfl_team_grades, parse_team_grades,
)


def _block(record="1 - 2", pf=63, pa=81, start=60.0):
    return [record, str(pf), str(pa), *[str(start + i) for i in range(len(GRADE_KEYS))], "Team Reports"]


def _sheet(left, right=None):
    """Two side-by-side streams, the way the sheet was pasted: names first, then blocks."""
    cols = [list(left) + [c for b in left.values() for c in b]]
    if right:
        cols.append(list(right) + [c for b in right.values() for c in b])
    height = max(map(len, cols))
    lines = [",".join(col[i] if i < len(col) else "" for col in cols) for i in range(height)]
    return "\n".join(lines) + "\n"


def test_two_side_by_side_tables_of_different_lengths_parse_into_per_team_rows():
    text = _sheet({"Air Force": _block("2 - 1", 102, 71), "Akron": _block("0 - 3", 20, 90)},
                  {"Arizona Cardinals": _block("1 - 2", 63, 81, 70.0)})

    college, nfl = parse_team_grades(text)

    assert [r.name for r in college] == ["Air Force", "Akron"]
    assert (college[0].wins, college[0].losses, college[0].points_for, college[0].points_against) == (2, 1, 102, 71)
    assert nfl[0].grades["overall"] == 70.0 and nfl[0].grades["special_teams"] == 82.0
    assert list(nfl[0].grades) == list(GRADE_KEYS)


def test_a_malformed_record_is_refused_not_guessed_at():
    # The first block's record locates the table, so the bad one must follow it.
    text = _sheet({"Team A": _block(), "Team B": _block("W - L")})
    with pytest.raises(TeamGradesError, match="not 'W - L'"):
        parse_team_grades(text)


def test_a_record_with_ties_parses_and_counts_them_as_games():
    text = _sheet({"Team A": _block("3 - 1 - 1", 90, 80), "Team B": _block("2 - 3", 70, 75)})

    (rows,) = parse_team_grades(text)

    a, b = rows
    assert (a.wins, a.losses, a.ties, a.games) == (3, 1, 1, 5)
    assert (b.wins, b.losses, b.ties, b.games) == (2, 3, 0, 5)


@pytest.mark.parametrize("mutate,message", [
    (lambda b: b[:-1], "cut off"),                                   # lost the closing marker
    (lambda b: [*b[:5], "x", *b[6:]], "not a number"),
    (lambda b: [*b[:3], "140", *b[4:]], "out of range"),
    (lambda b: b[:10] + b[11:], "expected 16 cells"),                # dropped a grade
])
def test_malformed_sheets_are_refused_not_guessed_at(mutate, message):
    with pytest.raises(TeamGradesError, match=message):
        parse_team_grades(_sheet({"Team A": mutate(_block())}))


def test_name_and_block_counts_must_agree():
    lines = _sheet({"A": _block(), "B": _block()}).splitlines()
    with pytest.raises(TeamGradesError, match="2 team names but 1 data blocks"):
        parse_team_grades("\n".join(lines[:-17]) + "\n")
    with pytest.raises(TeamGradesError, match="no team blocks"):
        parse_team_grades("a,b\nc,d\n")


def _row(name, record=(1, 2), pf=63, pa=81, ties=0):
    return TeamGradeRow(name, record[0], record[1], pf, pa, {k: 60.0 for k in GRADE_KEYS}, ties)


def test_misaligned_names_are_caught_by_comparing_with_stored_results():
    rows = [_row(f"T{i}", (i, 0), 20 + i, 10) for i in range(10)]
    shifted = {f"T{i}": [i + 1, 0, 20 + i + 1, 10] for i in range(10)}      # every team off by one
    with pytest.raises(TeamGradesError, match="misaligned"):
        _enforce(_record_check(rows, lambda row: shifted[row.name]), "NFL")

    ok = _record_check(rows, lambda row: [int(row.name[1:]), 0, 20 + int(row.name[1:]), 10])
    _enforce(ok, "NFL")  # all match: no error
    assert ok["matched"] == 10


def _nfl_repo(tmp_path):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.execute("INSERT INTO teams VALUES ('ARI','Arizona Cardinals','Cardinals','NFC','West',NULL,NULL,NULL,'now')")
        connection.execute("INSERT INTO teams VALUES ('ATL','Atlanta Falcons','Falcons','NFC','South',NULL,NULL,NULL,'now')")
        connection.commit()
    repository.replace_games(2026, [Game(
        game_id="g1", season=2026, season_type="REG", week=1, game_date="2026-09-10",
        game_time="20:00", away_team="ARI", home_team="ATL", away_score=17, home_score=24,
        overtime=False, division_game=False, stadium=None, roof=None, surface=None,
        temperature=None, wind=None, spread_line=None, total_line=None)])
    return repository


def test_nfl_import_stores_latest_snapshot_and_replaces_the_same_week(tmp_path):
    repository = _nfl_repo(tmp_path)
    rows = [_row("Arizona Cardinals", (0, 1), 17, 24), _row("Atlanta Falcons", (1, 0), 24, 17)]

    report = import_nfl(repository, rows, season=2026, week=1)
    import_nfl(repository, rows, season=2026, week=1)   # same week again: replaced, not doubled
    import_nfl(repository, [_row("Arizona Cardinals", (0, 1), 17, 24)], season=2026, week=2)

    assert (report["compared"], report["matched"]) == (2, 2)
    latest = latest_nfl_team_grades(repository, 2026)
    assert [(r["team"], r["week"]) for r in latest] == [("ARI", 2)]
    assert len(latest_nfl_team_grades(repository, 2026, "ARI")) == 1


def test_ties_are_stored_and_shown_in_the_record(tmp_path):
    from sports_aggregator.pff_team_grades import team_grades_table

    repository = _nfl_repo(tmp_path)
    import_nfl(repository, [_row("Arizona Cardinals", (0, 1), 17, 24, ties=1),
                            _row("Atlanta Falcons", (1, 0), 24, 17)], season=2026, week=1)

    latest = latest_nfl_team_grades(repository, 2026)
    assert {r["team"]: r["ties"] for r in latest} == {"ARI": 1, "ATL": 0}
    assert team_grades_table(latest, "ARI", "team")["record"] == "0-1-1"
    assert team_grades_table(latest, "ATL", "team")["record"] == "1-0"


def test_existing_databases_gain_the_ties_column_and_keep_their_rows(tmp_path):
    import sqlite3

    path = tmp_path / "nfl.sqlite3"
    repository = NFLRepository(path)
    repository.initialize()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE nfl_pff_team_grades")
        connection.execute(
            "CREATE TABLE nfl_pff_team_grades (season INTEGER NOT NULL, week INTEGER NOT NULL, "
            "team TEXT NOT NULL, wins INTEGER NOT NULL, losses INTEGER NOT NULL, "
            "points_for INTEGER NOT NULL, points_against INTEGER NOT NULL, "
            + ", ".join(f"{key} REAL NOT NULL" for key in GRADE_KEYS)
            + ", imported_at TEXT NOT NULL, PRIMARY KEY(season,week,team))")
        connection.execute(
            "INSERT INTO nfl_pff_team_grades VALUES (2025,1,'ARI',1,0,20,10,"
            + ",".join("60" for _ in GRADE_KEYS) + ",'now')")
        connection.commit()

    NFLRepository(path)._initialize_schema()     # initialize() is memoised once per process

    (row,) = latest_nfl_team_grades(NFLRepository(path), 2025)
    assert (row["wins"], row["losses"], row["ties"]) == (1, 0, 0)


def test_unknown_team_names_stop_the_import_before_anything_is_written(tmp_path):
    repository = _nfl_repo(tmp_path)
    with pytest.raises(TeamGradesError, match="not recognised"):
        import_nfl(repository, [_row("Arizona Cardinals"), _row("Atlantis Falcons")], season=2026, week=1)
    assert latest_nfl_team_grades(repository, 2026) == []


def _cfb_repo(tmp_path):
    repository = CFBRepository(tmp_path / "cfb.sqlite3")
    repository.initialize()
    with closing(repository._connect()) as connection:
        for team_id, school in ((1, "Air Force"), (2, "Ole Miss")):
            connection.execute("INSERT INTO teams (team_id,school,updated_at) VALUES (?,?,'now')", (team_id, school))
            connection.execute("INSERT INTO team_aliases (team_id,alias,normalized_alias) VALUES (?,?,?)",
                               (team_id, school, normalize_alias(school)))
        connection.commit()
    return repository


def test_college_import_resolves_pffs_full_school_names(tmp_path):
    repository = _cfb_repo(tmp_path)

    report = import_cfb(repository, [_row("Air Force", (2, 1), 102, 71), _row("Mississippi", (3, 0))],
                        season=2026, week=5)

    assert report["teams"] == 2
    stored = {r["team_name"]: r for r in latest_cfb_team_grades(repository, 2026)}
    assert set(stored) == {"Air Force", "Ole Miss"} and stored["Air Force"]["points_for"] == 102
    assert len(latest_cfb_team_grades(repository, 2026, 2)) == 1


def test_upload_route_requires_authorization_and_reports_bad_sheets(tmp_path):
    nfl, cfb = _nfl_repo(tmp_path), _cfb_repo(tmp_path)
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False, "NFL_REPOSITORY": nfl,
                      "CFB_REPOSITORY": cfb, "CFB_ADMIN_PIN": "1234"})
    client = app.test_client()
    sheet = _sheet({"Air Force": _block("2 - 1", 102, 71)},
                   {"Arizona Cardinals": _block("0 - 1", 17, 24)})
    data = {"season": "2026", "nfl_week": "1", "cfb_week": "5", "token": "1234"}

    def post(extra, payload):
        return client.post("/nfl/data-import/team-grades",
                           data={**data, **extra, "sheet": (io.BytesIO(payload), "s.csv")})

    denied = post({"token": "nope"}, sheet.encode())
    ok = post({}, sheet.encode())
    bad = post({}, b"a,b\nc,d\n")

    assert denied.status_code == 401
    assert ok.status_code == 200 and b"Stored 2 team-grade rows" in ok.data
    assert bad.status_code == 400 and b"was not imported" in bad.data
    assert len(latest_nfl_team_grades(nfl, 2026)) == 1 and len(latest_cfb_team_grades(cfb, 2026)) == 1


def test_team_page_shows_pff_team_grades_with_league_rank(tmp_path):
    nfl = _nfl_repo(tmp_path)
    import_nfl(nfl, [_row("Arizona Cardinals", (0, 1), 17, 24), _row("Atlanta Falcons", (1, 0), 24, 17)],
               season=2026, week=1)
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False, "NFL_REPOSITORY": nfl})

    page = app.test_client().get("/nfl/teams/ARI/?season=2026")

    assert page.status_code == 200
    assert b"PFF team grades" in page.data and b"through week 1" in page.data
    assert b"#1 of 2" in page.data  # equal grades share the top rank
