"""Import licensed PFF CSV snapshots without treating them as live CFBD data."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from sports_aggregator.cfb.models import normalize_alias
from sports_aggregator.cfb.repository import CFBRepository


DATASETS = {
    "defense_coverage_summary.csv": ("coverage", "grades_coverage_defense", "snap_counts_coverage"),
    "defense_summary (6).csv": ("defense", "grades_defense", "snap_counts_defense"),
    "offense_blocking (7).csv": ("blocking", "grades_offense", "snap_counts_offense"),
    "passing_summary (1).csv": ("passing", "grades_pass", "dropbacks"),
    "pass_rush_summary.csv": ("pass_rush", "grades_pass_rush_defense", "snap_counts_pass_rush"),
    "receiving_summary (12).csv": ("receiving", "grades_pass_route", "routes"),
    "rushing_summary (11).csv": ("rushing", "grades_run", "attempts"),
}

# Optional regular-season detail exports. They enrich player cards and assignment
# logic without replacing the seven stable summary datasets.
SUPPLEMENTAL_DATASETS = {
    "defense_coverage_scheme (1).csv": (
        "coverage_scheme", (("man_grades_coverage_defense", "man_snap_counts_coverage"),
                            ("zone_grades_coverage_defense", "zone_snap_counts_coverage"))),
    "passing_depth (4).csv": (
        "passing_depth", (("behind_los_grades_pass", "behind_los_attempts"),
                           ("short_grades_pass", "short_attempts"),
                           ("medium_grades_pass", "medium_attempts"),
                           ("deep_grades_pass", "deep_attempts"))),
    "receiving_scheme (5).csv": (
        "receiving_scheme", (("man_grades_pass_route", "man_routes"),
                              ("zone_grades_pass_route", "zone_routes"))),
    "return_summary (1).csv": (
        "returns", (("grades_return", "total_attempts"),)),
    "run_defense_summary (1).csv": (
        "run_defense_detail", (("grades_run_defense", "snap_counts_run"),)),
    "receiving_concept.csv": (
        "receiving_concept", (("screen_grades_pass_route", "screen_routes"),
                              ("slot_grades_pass_route", "slot_routes"))),
    "receiving_depth.csv": (
        "receiving_depth", (("behind_los_grades_pass_route", "behind_los_routes"),
                            ("short_grades_pass_route", "short_routes"),
                            ("medium_grades_pass_route", "medium_routes"),
                            ("deep_grades_pass_route", "deep_routes"))),
    # PFF publishes this one with no grade column at all -- coverage snap and
    # target counts specific to the slot, nothing evaluative. See
    # _weighted_metric's None-grade_field handling.
    "slot_coverage.csv": (
        "slot_coverage", ((None, "coverage_snaps"),)),
}

DATASET_POSITION_GROUPS = {
    "coverage": {"SECONDARY", "LB"},
    "defense": {"SECONDARY", "LB", "EDGE", "INTERIOR_DL"},
    "blocking": {"OL", "TE"},
    "passing": {"QB"},
    "pass_rush": {"EDGE", "INTERIOR_DL", "LB"},
    "receiving": {"WR", "TE", "RB"},
    "rushing": {"RB", "QB"},
}

# PFF's export uses compact display names. These are deterministic aliases, not
# fuzzy guesses. Unlisted names must resolve through a CFBD alias or remain open.
PFF_TEAM_OVERRIDES = {
    "ARIZONA ST": "Arizona State", "ARK STATE": "Arkansas State",
    "BALL ST": "Ball State", "BOISE ST": "Boise State",
    "BOSTON COL": "Boston College", "BOWL GREEN": "Bowling Green",
    "C MICHIGAN": "Central Michigan", "COAST CAR": "Coastal Carolina",
    "COLO STATE": "Colorado State", "DOMINION": "Old Dominion",
    "E CAROLINA": "East Carolina", "E MICHIGAN": "Eastern Michigan",
    "FLORIDA ST": "Florida State", "FRESNO ST": "Fresno State",
    "GA SOUTHRN": "Georgia Southern", "GA STATE": "Georgia State",
    "GA TECH": "Georgia Tech", "HAWAII": "Hawai'i",
    "JAMES MAD": "James Madison", "JVILLE ST": "Jacksonville State",
    "KANSAS ST": "Kansas State", "KENNESAW": "Kennesaw State",
    "LA LAFAYET": "Louisiana", "LA MONROE": "UL Monroe",
    "LA TECH": "Louisiana Tech", "MIAMI FL": "Miami",
    "MICH STATE": "Michigan State", "MIDDLE TN": "Middle Tennessee",
    "MISS STATE": "Mississippi State", "MO STATE": "Missouri State",
    "N CAROLINA": "North Carolina", "N ILLINOIS": "Northern Illinois",
    "N TEXAS": "North Texas", "NEW MEX ST": "New Mexico State",
    "NWESTERN": "Northwestern", "OKLA STATE": "Oklahoma State",
    "OREGON ST": "Oregon State", "S ALABAMA": "South Alabama",
    "S CAROLINA": "South Carolina", "S DIEGO ST": "San Diego State",
    "S JOSE ST": "SJSU", "SM HOUSTON": "Sam Houston",
    "SO MISS": "Southern Miss", "TEXAS ST": "Texas State",
    "UMASS": "Massachusetts", "UTAH ST": "Utah State",
    "VA TECH": "Virginia Tech", "W KENTUCKY": "Western Kentucky",
    "W MICHIGAN": "Western Michigan", "W VIRGINIA": "West Virginia",
    "WASH STATE": "Washington State",
}


@dataclass(frozen=True, slots=True)
class PFFImportReport:
    season: int
    roster_season: int
    files: int
    rows: int
    players: int
    linked_players: int
    candidate_transfers: int
    unresolved_players: int
    unresolved_teams: tuple[str, ...]
    supplemental_files: int = 0
    supplemental_rows: int = 0


def _number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position_group(position: str) -> str:
    value = (position or "UNKNOWN").upper()
    if value in {"C", "G", "T", "LT", "LG", "RG", "RT", "OL"}: return "OL"
    if value in {"DI", "DT", "NT", "DL"}: return "INTERIOR_DL"
    if value in {"ED", "EDGE", "DE"}: return "EDGE"
    if value in {"LB", "ILB", "OLB"}: return "LB"
    if value in {"CB", "S", "FS", "SS", "DB"}: return "SECONDARY"
    if value in {"HB", "FB", "RB"}: return "RB"
    if value in {"WR", "TE"}: return value
    return value


def _primary_grade(row: dict[str, str], dataset: str, grade_field: str) -> float | None:
    if dataset != "blocking":
        return _number(row.get(grade_field))
    components = [value for value in (
        _number(row.get("grades_pass_block")), _number(row.get("grades_run_block"))
    ) if value is not None]
    return sum(components) / len(components) if components else None


def _weighted_metric(
    row: dict[str, str], fields: tuple[tuple[str | None, str], ...]
) -> tuple[float | None, float | None]:
    """Usage-weight a grade across scheme/depth splits without inventing data.

    A pair's grade_field may be None for a dataset PFF publishes with no grade
    column at all (slot_coverage is snap/target counts only) -- usage still
    sums across whatever fields are given, it just never contributes a grade.
    """
    values = []
    usage_only = 0.0
    for grade_field, usage_field in fields:
        usage = _number(row.get(usage_field))
        if grade_field is None:
            if usage is not None:
                usage_only += usage
            continue
        grade = _number(row.get(grade_field))
        if grade is not None and usage is not None and usage > 0:
            values.append((grade, usage))
    if not values:
        return None, (usage_only or None)
    usage = sum(weight for _, weight in values)
    return sum(grade * weight for grade, weight in values) / usage, usage


class PFFImporter:
    """Loads a historical snapshot and links only identities supported by evidence."""

    def __init__(self, repository: CFBRepository) -> None:
        self.repository = repository

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.repository.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _team_lookup(self, connection: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
        lookup: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in connection.execute(
            "SELECT t.team_id,t.school,a.normalized_alias FROM team_aliases a JOIN teams t USING(team_id)"
        ):
            lookup[row["normalized_alias"]].append(row)
        return lookup

    @staticmethod
    def _resolve_team(name: str, lookup: dict[str, list[sqlite3.Row]]) -> sqlite3.Row | None:
        canonical = PFF_TEAM_OVERRIDES.get(name.strip().upper(), name)
        matches = lookup.get(normalize_alias(canonical), [])
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _match_player(
        name: str, team: str | None, roster_by_name: dict[str, list[sqlite3.Row]]
    ) -> tuple[str | None, str | None, str, float]:
        matches = roster_by_name.get(normalize_alias(name), [])
        same_team = [row for row in matches if team and row["team"] == team]
        if len(same_team) == 1:
            return same_team[0]["player_id"], None, "exact_name_same_team", 1.0
        if len(matches) == 1:
            return None, matches[0]["player_id"], "possible_transfer", 0.7
        if matches:
            return None, None, "ambiguous_name", 0.0
        return None, None, "unresolved", 0.0

    def import_directory(self, directory: str | Path, *, season: int, roster_season: int) -> PFFImportReport:
        directory = Path(directory)
        missing = [name for name in DATASETS if not (directory / name).is_file()]
        if missing:
            raise FileNotFoundError(f"Missing expected PFF files: {', '.join(missing)}")
        self.repository.initialize()
        imported_at = datetime.now(timezone.utc).isoformat()
        rows_seen = 0
        supplemental_rows = 0
        supplemental_files = 0
        player_ids: set[str] = set()
        unresolved_teams: set[str] = set()
        groups: dict[tuple[str, int | None, str, str], list[tuple[float, float]]] = defaultdict(list)

        with closing(self._connect()) as connection:
            team_lookup = self._team_lookup(connection)
            roster_by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for player in connection.execute("SELECT * FROM players WHERE season=?", (roster_season,)):
                roster_by_name[player["normalized_name"]].append(player)

            connection.execute("DELETE FROM pff_player_metrics WHERE season=?", (season,))
            connection.execute("DELETE FROM pff_players WHERE season=?", (season,))
            connection.execute("DELETE FROM pff_position_groups WHERE season=?", (season,))

            for filename, (dataset, grade_field, usage_field) in DATASETS.items():
                source_path = directory / filename
                # The historical OL collection includes a more complete, later
                # 2025 export. Prefer it while retaining the established filename
                # as a required fallback for portable/test snapshots.
                preferred = directory / "oline_data" / f"ol_{season}.csv"
                if dataset == "blocking" and preferred.is_file():
                    source_path = preferred
                corrected_pass_rush = directory / "pass_rush_summary (2).csv"
                if dataset == "pass_rush" and corrected_pass_rush.is_file():
                    source_path = corrected_pass_rush
                with source_path.open("r", encoding="utf-8-sig", newline="") as source:
                    for row in csv.DictReader(source):
                        rows_seen += 1
                        pff_id = str(row.get("player_id") or "").strip()
                        if not pff_id:
                            continue
                        player_ids.add(pff_id)
                        player_name = str(row.get("player") or "").strip()
                        pff_team = str(row.get("team_name") or "").strip()
                        team = self._resolve_team(pff_team, team_lookup)
                        if team is None:
                            unresolved_teams.add(pff_team)
                        team_name = team["school"] if team else None
                        linked, candidate, match_status, confidence = self._match_player(
                            player_name, team_name, roster_by_name
                        )
                        position_group = _position_group(str(row.get("position") or ""))
                        grade = (_primary_grade(row, dataset, grade_field)
                                 if position_group in DATASET_POSITION_GROUPS[dataset] else None)
                        usage = _number(row.get(usage_field))
                        games = int(_number(row.get("player_game_count")) or 0)
                        interest = (grade * (0.5 + 0.5 * min(games / 8, 1))
                                    * (0.5 + 0.5 * min((usage or 0) / 100, 1))
                                    if grade is not None else None)
                        connection.execute(
                            """INSERT INTO pff_players VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(season,pff_player_id) DO UPDATE SET
                               player_name=excluded.player_name,normalized_name=excluded.normalized_name,
                               position=excluded.position,pff_team_name=excluded.pff_team_name,
                               cfbd_team_id=excluded.cfbd_team_id,cfbd_team=excluded.cfbd_team,
                               cfbd_player_id=COALESCE(excluded.cfbd_player_id,pff_players.cfbd_player_id),
                               candidate_cfbd_player_id=COALESCE(excluded.candidate_cfbd_player_id,pff_players.candidate_cfbd_player_id),
                               match_status=CASE WHEN excluded.match_confidence>pff_players.match_confidence THEN excluded.match_status ELSE pff_players.match_status END,
                               match_confidence=MAX(excluded.match_confidence,pff_players.match_confidence),
                               interest_score=CASE
                                 WHEN pff_players.interest_score IS NULL THEN excluded.interest_score
                                 WHEN excluded.interest_score IS NULL THEN pff_players.interest_score
                                 ELSE MAX(excluded.interest_score,pff_players.interest_score)
                               END,updated_at=excluded.updated_at""",
                            (season, pff_id, player_name, normalize_alias(player_name), row.get("position"),
                             pff_team, team["team_id"] if team else None, team_name, linked, candidate,
                             match_status, confidence, interest, imported_at),
                        )
                        connection.execute(
                            "INSERT INTO pff_player_metrics VALUES(?,?,?,?,?,?,?,?,?)",
                            (season, pff_id, dataset, str(source_path.relative_to(directory)), games, grade, usage,
                             json.dumps(row, separators=(",", ":")), imported_at),
                        )
                        if grade is not None:
                            group_key = (pff_team, team["team_id"] if team else None,
                                         position_group, dataset)
                            groups[group_key].append((grade, usage or 0.0))

            for (pff_team, team_id, position_group, dataset), values in groups.items():
                total_usage = sum(usage for _, usage in values)
                weighted = (sum(grade * usage for grade, usage in values) / total_usage
                            if total_usage else sum(grade for grade, _ in values) / len(values))
                connection.execute(
                    "INSERT INTO pff_position_groups VALUES(?,?,?,?,?,?,?,?)",
                    (season, team_id, pff_team, position_group, dataset,
                     round(weighted, 2), len(values), total_usage),
                )

            connection.execute(
                """DELETE FROM pff_supplemental_metrics
                   WHERE season=? AND dataset!='blocking_history'""", (season,))
            for filename, (dataset, fields) in SUPPLEMENTAL_DATASETS.items():
                source_path = directory / filename
                if not source_path.is_file():
                    continue
                supplemental_files += 1
                with source_path.open("r", encoding="utf-8-sig", newline="") as source:
                    for row in csv.DictReader(source):
                        pff_id = str(row.get("player_id") or "").strip()
                        if not pff_id:
                            continue
                        grade, usage = _weighted_metric(row, fields)
                        connection.execute(
                            """INSERT INTO pff_supplemental_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(season,pff_player_id,dataset,context) DO UPDATE SET
                               player_name=excluded.player_name,position=excluded.position,
                               event_team=excluded.event_team,source_file=excluded.source_file,
                               game_count=excluded.game_count,primary_grade=excluded.primary_grade,
                               usage_count=excluded.usage_count,metrics_json=excluded.metrics_json,
                               imported_at=excluded.imported_at""",
                            (season, pff_id, str(row.get("player") or "").strip(),
                             row.get("position"), row.get("team_name"), dataset,
                             "REGULAR_SEASON_DETAIL", filename,
                             int(_number(row.get("player_game_count")) or 0), grade, usage,
                             json.dumps(row, separators=(",", ":")), imported_at),
                        )
                        supplemental_rows += 1

            # Older college OL files add career context for players whose stable
            # PFF IDs appear in the current snapshot. They never alter current
            # team units or interest scores.
            history_dir = directory / "oline_data"
            if history_dir.is_dir():
                for source_path in sorted(history_dir.glob("ol_*.csv")):
                    try:
                        metric_season = int(source_path.stem.removeprefix("ol_"))
                    except ValueError:
                        continue
                    if metric_season >= season:
                        continue
                    connection.execute(
                        """DELETE FROM pff_supplemental_metrics
                           WHERE season=? AND dataset='blocking_history'
                           AND context='REGULAR_SEASON'""", (metric_season,))
                    supplemental_files += 1
                    with source_path.open("r", encoding="utf-8-sig", newline="") as source:
                        for row in csv.DictReader(source):
                            pff_id = str(row.get("player_id") or "").strip()
                            if not pff_id:
                                continue
                            grade = _primary_grade(row, "blocking", "grades_offense")
                            usage = _number(row.get("snap_counts_offense"))
                            connection.execute(
                                """INSERT INTO pff_supplemental_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (metric_season, pff_id, str(row.get("player") or "").strip(),
                                 row.get("position"), row.get("team_name"), "blocking_history",
                                 "REGULAR_SEASON", str(source_path.relative_to(directory)),
                                 int(_number(row.get("player_game_count")) or 0), grade, usage,
                                 json.dumps(row, separators=(",", ":")), imported_at),
                            )
                            supplemental_rows += 1

            counts = connection.execute(
                """SELECT COUNT(*) players,
                   SUM(cfbd_player_id IS NOT NULL) linked,
                   SUM(match_status='possible_transfer') candidates,
                   SUM(match_status IN ('unresolved','ambiguous_name')) unresolved
                   FROM pff_players WHERE season=?""", (season,)
            ).fetchone()
            connection.execute(
                """INSERT INTO pff_imports(season,roster_season,imported_at,source_directory,
                   files,rows,linked_players,candidate_transfers,unresolved_players,unresolved_teams_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (season, roster_season, imported_at, str(directory), len(DATASETS), rows_seen,
                 counts["linked"] or 0, counts["candidates"] or 0, counts["unresolved"] or 0,
                 json.dumps(sorted(unresolved_teams))),
            )
            connection.commit()

        return PFFImportReport(
            season, roster_season, len(DATASETS), rows_seen, counts["players"],
            counts["linked"] or 0, counts["candidates"] or 0,
            counts["unresolved"] or 0, tuple(sorted(unresolved_teams)),
            supplemental_files, supplemental_rows,
        )


def pff_summary(repository: CFBRepository, season: int) -> dict[str, Any]:
    repository.initialize()
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.row_factory = sqlite3.Row
        counts = connection.execute(
            """SELECT COUNT(*) players,SUM(cfbd_player_id IS NOT NULL) linked,
               SUM(match_status='possible_transfer') candidates,
               SUM(match_status IN ('unresolved','ambiguous_name')) unresolved
               FROM pff_players WHERE season=?""", (season,)
        ).fetchone()
        groups = [dict(row) for row in connection.execute(
            """SELECT * FROM pff_position_groups WHERE season=? AND weighted_grade IS NOT NULL
               AND player_count>=2 AND usage_count>=100 AND position_group!='QB'
               ORDER BY weighted_grade DESC LIMIT 50""", (season,)
        )]
        players = [dict(row) for row in connection.execute(
            """SELECT * FROM pff_players WHERE season=? AND interest_score IS NOT NULL
               ORDER BY interest_score DESC LIMIT 50""", (season,)
        )]
        supplemental = connection.execute(
            "SELECT COUNT(*) FROM pff_supplemental_metrics WHERE season=?", (season,)
        ).fetchone()[0]
    return {"season": season, "players": counts["players"] or 0,
            "linked": counts["linked"] or 0, "candidates": counts["candidates"] or 0,
            "unresolved": counts["unresolved"] or 0, "top_players": players,
            "top_position_groups": groups, "supplemental_metrics": supplemental}
