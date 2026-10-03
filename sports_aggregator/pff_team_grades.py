"""PFF team-level grades (NFL and college) from the "Team PFF Grades" sheet.

PFF publishes team grades on a wider scale than anything that can be rebuilt from
player grades (see unit_grades.py), but only on its website. This module ingests a
copy of that table so the site can show them beside the player-based unit grades.

The sheet is a copy-paste of two side-by-side tables, so it is not a tidy CSV. Each
column is an independent stream: team names first, then one block per team in the
same order, every block ending in the literal cell ``Team Reports``::

    record, points for, points against, then 13 grades, "Team Reports"

The grade columns carry no header. Their order was established against our own
data rather than assumed -- on the 2026 week-4 file, with Pearson correlations over
the 32 NFL teams: Offense vs offensive EPA/play 0.83, Passing vs pass EPA 0.73,
Rushing vs rush EPA 0.66, Defense vs defensive EPA allowed 0.52, Run Defense vs
rush EPA allowed 0.53, Coverage vs pass EPA allowed 0.48, Pass Rush vs sacks per
game 0.43 -- and every team's record and points for/against equal our stored game
results exactly (32 of 32 NFL, 136 of 138 college; the other two are name aliases).
Parsing therefore refuses anything that does not have exactly that shape.
"""
from __future__ import annotations

from contextlib import closing
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import re
from typing import Any

#: Column order inside a block, after record / points for / points against.
GRADE_COLUMNS = (
    ("overall", "Overall"), ("offense", "Offense"), ("passing", "Passing"),
    ("pass_block", "Pass Block"), ("receiving", "Receiving"), ("rushing", "Rushing"),
    ("run_block", "Run Block"), ("defense", "Defense"), ("run_defense", "Run Defense"),
    ("tackling", "Tackling"), ("pass_rush", "Pass Rush"), ("coverage", "Coverage"),
    ("special_teams", "Special Teams"),
)
GRADE_KEYS = tuple(key for key, _ in GRADE_COLUMNS)
BLOCK_END = "Team Reports"
_RECORD = re.compile(r"(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?")
#: A league-level mismatch rate above this means names and blocks are misaligned.
MAX_RECORD_MISMATCH_RATE = 0.2


class TeamGradesError(ValueError):
    """The sheet does not have the shape this parser is willing to trust."""


@dataclass(frozen=True)
class TeamGradeRow:
    name: str
    wins: int
    losses: int
    points_for: int
    points_against: int
    grades: dict[str, float]

    @property
    def games(self) -> int:
        return self.wins + self.losses


def _number(text: str, what: str, team: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise TeamGradesError(f"{team}: {what} {text!r} is not a number") from None


def _parse_stream(stream: list[str], column: int) -> list[TeamGradeRow]:
    start = next((i for i, cell in enumerate(stream) if _RECORD.fullmatch(cell)), None)
    if start is None:
        return []
    names, cells = stream[:start], stream[start:]
    blocks: list[list[str]] = [[]]
    for cell in cells:
        if cell == BLOCK_END:
            blocks.append([])
        else:
            blocks[-1].append(cell)
    if blocks[-1]:
        raise TeamGradesError(
            f"column {column + 1}: {len(blocks[-1])} cells follow the last '{BLOCK_END}' "
            "(the table was cut off)")
    blocks.pop()
    if len(blocks) != len(names):
        raise TeamGradesError(
            f"column {column + 1}: {len(names)} team names but {len(blocks)} data blocks")
    if len(set(names)) != len(names):
        raise TeamGradesError(f"column {column + 1}: duplicate team names")

    rows = []
    for name, block in zip(names, blocks):
        if len(block) != 3 + len(GRADE_KEYS):
            raise TeamGradesError(
                f"{name}: expected {3 + len(GRADE_KEYS)} cells, found {len(block)}")
        record = _RECORD.fullmatch(block[0])
        if not record or record.group(3):
            raise TeamGradesError(f"{name}: record {block[0]!r} is not 'W - L'")
        grades = {key: _number(value, key, name) for key, value in zip(GRADE_KEYS, block[3:])}
        bad = {key: value for key, value in grades.items() if not 0 <= value <= 100}
        if bad:
            raise TeamGradesError(f"{name}: grade out of range {bad}")
        rows.append(TeamGradeRow(
            name=name, wins=int(record.group(1)), losses=int(record.group(2)),
            points_for=int(_number(block[1], "points for", name)),
            points_against=int(_number(block[2], "points against", name)), grades=grades))
    return rows


def parse_team_grades(text: str) -> list[list[TeamGradeRow]]:
    """One list of rows per data column in the sheet (usually: college, then NFL)."""
    table = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    width = max((len(row) for row in table), default=0)
    streams = [[row[i].strip() for row in table if len(row) > i and row[i].strip()]
               for i in range(width)]
    parsed = [rows for column, stream in enumerate(streams)
              if (rows := _parse_stream(stream, column))]
    if not parsed:
        raise TeamGradesError("no team blocks found: expected team names, then "
                              "'W - L, PF, PA, 13 grades, Team Reports' per team")
    return parsed


# ------------------------------------------------------------------ storage

def _grade_values(row: TeamGradeRow) -> tuple[float, ...]:
    return tuple(row.grades[key] for key in GRADE_KEYS)



def import_nfl(repository, rows: list[TeamGradeRow], *, season: int, week: int) -> dict[str, Any]:
    """Store NFL team grades for (season, week); returns a report, raises on misalignment."""
    repository.initialize()
    with closing(repository._connect()) as connection:
        abbreviations = {r["name"]: r["abbreviation"]
                         for r in connection.execute("SELECT name,abbreviation FROM teams")}
        unmapped = [row.name for row in rows if row.name not in abbreviations]
        if unmapped:
            raise TeamGradesError(f"NFL team names not recognised: {unmapped}")
        actual: dict[str, list[int]] = {}
        for g in connection.execute(
                """SELECT home_team,away_team,home_score,away_score FROM games
                   WHERE season=? AND completed=1 AND home_score IS NOT NULL""", (season,)):
            for team, pf, pa in ((g["home_team"], g["home_score"], g["away_score"]),
                                 (g["away_team"], g["away_score"], g["home_score"])):
                a = actual.setdefault(team, [0, 0, 0, 0])
                a[0] += pf > pa; a[1] += pf < pa; a[2] += pf; a[3] += pa
        check = _record_check(rows, lambda row: actual.get(abbreviations[row.name]))
        _enforce(check, "NFL")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("DELETE FROM nfl_pff_team_grades WHERE season=? AND week=?", (season, week))
        connection.executemany(
            f"""INSERT INTO nfl_pff_team_grades (season,week,team,wins,losses,points_for,
                  points_against,{','.join(GRADE_KEYS)},imported_at)
                VALUES (?,?,?,?,?,?,?,{','.join('?' * len(GRADE_KEYS))},?)""",
            [(season, week, abbreviations[r.name], r.wins, r.losses, r.points_for,
              r.points_against, *_grade_values(r), now) for r in rows])
        connection.commit()
    return {"league": "nfl", "season": season, "week": week, "teams": len(rows), **check}


def import_cfb(repository, rows: list[TeamGradeRow], *, season: int, week: int) -> dict[str, Any]:
    """Store college team grades for (season, week), matching schools like the PFF importer."""
    from sports_aggregator.cfb.pff import PFFImporter

    repository.initialize()
    importer = PFFImporter(repository)
    with closing(repository._connect()) as connection:
        lookup = importer._team_lookup(connection)
        resolved = {row.name: importer._resolve_team(row.name, lookup) for row in rows}
        unmapped = [name for name, team in resolved.items() if team is None]
        if unmapped:
            raise TeamGradesError(f"college team names not recognised: {unmapped}")
        actual: dict[int, list[int]] = {}
        for g in connection.execute(
                """SELECT home_team_id,away_team_id,home_points,away_points FROM games
                   WHERE season=? AND completed=1 AND home_points IS NOT NULL
                     AND away_points IS NOT NULL""", (season,)):
            for team, pf, pa in ((g["home_team_id"], g["home_points"], g["away_points"]),
                                 (g["away_team_id"], g["away_points"], g["home_points"])):
                a = actual.setdefault(team, [0, 0, 0, 0])
                a[0] += pf > pa; a[1] += pf < pa; a[2] += pf; a[3] += pa
        check = _record_check(rows, lambda row: actual.get(resolved[row.name]["team_id"]))
        _enforce(check, "college")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("DELETE FROM pff_team_grades WHERE season=? AND week=?", (season, week))
        connection.executemany(
            f"""INSERT INTO pff_team_grades (season,week,cfbd_team_id,team_name,wins,losses,
                  points_for,points_against,{','.join(GRADE_KEYS)},imported_at)
                VALUES (?,?,?,?,?,?,?,?,{','.join('?' * len(GRADE_KEYS))},?)""",
            [(season, week, resolved[r.name]["team_id"], resolved[r.name]["school"], r.wins,
              r.losses, r.points_for, r.points_against, *_grade_values(r), now) for r in rows])
        connection.commit()
    return {"league": "cfb", "season": season, "week": week, "teams": len(rows), **check}


def _record_check(rows: list[TeamGradeRow], actual_for) -> dict[str, Any]:
    """Compare each team's record and points with our stored results.

    A sheet whose names and blocks are shifted against each other cannot match;
    one that is aligned matches except where we are missing a game.
    """
    compared = matched = 0
    mismatched: list[str] = []
    for row in rows:
        actual = actual_for(row)
        if actual is None:
            continue
        compared += 1
        if (row.wins, row.losses, row.points_for, row.points_against) == tuple(actual):
            matched += 1
        else:
            mismatched.append(row.name)
    return {"compared": compared, "matched": matched, "mismatched": mismatched}


def _enforce(check: dict[str, Any], league: str) -> None:
    compared = check["compared"]
    if compared >= 8 and len(check["mismatched"]) / compared > MAX_RECORD_MISMATCH_RATE:
        raise TeamGradesError(
            f"{league}: {len(check['mismatched'])} of {compared} teams disagree with our stored "
            "results on record or points -- the team names and data blocks are probably "
            f"misaligned. Nothing was stored. First few: {check['mismatched'][:5]}")


# ------------------------------------------------------------------- reads

def latest_nfl_team_grades(repository, season: int, team: str | None = None) -> list[dict[str, Any]]:
    """Most recent stored snapshot per team (or for one team)."""
    repository.initialize()
    sql = """SELECT g.* FROM nfl_pff_team_grades g
             WHERE g.season=? AND g.week=(SELECT MAX(week) FROM nfl_pff_team_grades WHERE season=g.season)"""
    params: list[Any] = [season]
    if team:
        sql += " AND g.team=?"
        params.append(team)
    with closing(repository._connect()) as connection:
        return [dict(row) for row in connection.execute(sql + " ORDER BY g.team", params)]


def latest_cfb_team_grades(repository, season: int, team_id: int | None = None) -> list[dict[str, Any]]:
    repository.initialize()
    sql = """SELECT g.* FROM pff_team_grades g
             WHERE g.season=? AND g.week=(SELECT MAX(week) FROM pff_team_grades WHERE season=g.season)"""
    params: list[Any] = [season]
    if team_id is not None:
        sql += " AND g.cfbd_team_id=?"
        params.append(team_id)
    with closing(repository._connect()) as connection:
        return [dict(row) for row in connection.execute(sql + " ORDER BY g.team_name", params)]


def team_grades_table(pool: list[dict[str, Any]], team_id: Any, id_key: str):
    """A rank-annotated table of one team's 13 PFF grades, or None when it has none.

    `pool` is the league's latest snapshot, which is what the rank is taken over.
    """
    from sports_aggregator.nfl.ranking import rank_within
    from sports_aggregator.tables import Column, Table

    mine = next((row for row in pool if row[id_key] == team_id), None)
    if mine is None:
        return None
    rows = []
    for key, label in GRADE_COLUMNS:
        rank = rank_within(pool, id_key=id_key, value_key=key).get(team_id) or {}
        rows.append({"grade": label, "value": mine[key],
                     "rank": f"#{rank['rank']} of {rank['of']}" if rank else None,
                     "rank_sort": rank.get("rank")})
    table = Table([
        Column("grade", "PFF grade", "text", emphasis=True),
        Column("value", "Grade", "f1"),
        Column("rank", "Rank", "text", align="right", sort="number"),
    ], rows, dense=True, sortable=False)
    return {"table": table, "week": mine["week"], "record": f"{mine['wins']}-{mine['losses']}"}


# --------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    """python -m sports_aggregator.pff_team_grades FILE --season 2026 --nfl-week 4 --cfb-week 5"""
    import argparse
    import json
    import os

    from sports_aggregator.cfb.repository import CFBRepository
    from sports_aggregator.nfl.repository import NFLRepository

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("file")
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--nfl-week", type=int, required=True)
    parser.add_argument("--cfb-week", type=int, required=True)
    args = parser.parse_args(argv)
    with open(args.file, encoding="utf-8-sig", newline="") as handle:
        report = import_all(handle.read(), season=args.season, nfl_week=args.nfl_week,
                            cfb_week=args.cfb_week,
                            nfl=NFLRepository(os.getenv("NFL_DATABASE_PATH", "instance/nfl.sqlite3")),
                            cfb=CFBRepository(os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")))
    print(json.dumps(report, indent=2))
    return 0


def import_all(text: str, *, season: int, nfl_week: int, cfb_week: int, nfl, cfb) -> list[dict[str, Any]]:
    """Parse once, then store each league's block set. Nothing is stored if parsing fails."""
    columns = parse_team_grades(text)
    nfl_names = _nfl_names(nfl)
    reports = []
    for rows in columns:
        if all(row.name in nfl_names for row in rows):
            reports.append(import_nfl(nfl, rows, season=season, week=nfl_week))
        else:
            reports.append(import_cfb(cfb, rows, season=season, week=cfb_week))
    return reports


def _nfl_names(repository) -> set[str]:
    repository.initialize()
    with closing(repository._connect()) as connection:
        return {row["name"] for row in connection.execute("SELECT name FROM teams")}


if __name__ == "__main__":
    raise SystemExit(main())
