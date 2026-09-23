"""Meaningful-drive outcome actuals for the expected-points layer.

Outcomes are mutually exclusive and offense-oriented. Defensive and return
touchdowns are not credited to the offense. Expected points later uses the
empirical TD/FG probabilities here rather than regressing final scores.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.team_game_pace import _drive_classification
from sports_aggregator.cfb.team_game_scoring import (
    _FUMBLE_LOST_TYPES, _INTERCEPTION_TYPES, _is_offensive_touchdown,
)

METRIC_VERSION = "team-game-drive-outcomes-v1"
PBP_METRIC_VERSION = "pbp-v1"


@schema_once("team_game_drive_outcomes")
def initialize(repository) -> None:
    from sports_aggregator.cfb.play_by_play import initialize as initialize_pbp
    initialize_pbp(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_team_game_drive_outcomes (
          game_id INTEGER NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
          metric_version TEXT NOT NULL, meaningful_drives INTEGER NOT NULL,
          touchdowns INTEGER NOT NULL, field_goals INTEGER NOT NULL,
          turnovers INTEGER NOT NULL, punts INTEGER NOT NULL,
          turnovers_on_downs INTEGER NOT NULL, end_half_drives INTEGER NOT NULL,
          other_drives INTEGER NOT NULL, offensive_points REAL NOT NULL,
          touchdowns_per_drive REAL, field_goals_per_drive REAL,
          turnovers_per_drive REAL, punts_per_drive REAL,
          points_per_drive REAL, built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,metric_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_team_game_drive_outcomes_team
          ON cfb_team_game_drive_outcomes(team,metric_version,game_id);
        """)
        connection.commit()


def _outcome(rows: list[dict[str, Any]]) -> str:
    competitive = [row for row in rows if not row.get("garbage_time")]
    if any(_is_offensive_touchdown(row) for row in competitive):
        return "touchdowns"
    if any("field goal good" in str(row.get("play_type") or "").casefold()
           for row in competitive):
        return "field_goals"
    if any(str(row.get("play_type") or "").casefold()
           in (_INTERCEPTION_TYPES | _FUMBLE_LOST_TYPES) for row in competitive):
        return "turnovers"
    if any("punt" in str(row.get("play_type") or "").casefold()
           for row in competitive):
        return "punts"
    scrimmage = [row for row in competitive if row.get("rush_pass") in {"rush", "pass"}]
    if scrimmage:
        last = scrimmage[-1]
        if int(last.get("down") or 0) == 4 and not int(last.get("success") or 0):
            return "turnovers_on_downs"
    text = " ".join(str(row.get("play_text") or "").casefold() for row in rows)
    if any(token in text for token in ("end of half", "end period", "end of game",
                                       "end of regulation")):
        return "end_half_drives"
    return "other_drives"


def build(repository, *, from_season: int | None = None,
          to_season: int | None = None,
          metric_version: str = METRIC_VERSION) -> dict[str, Any]:
    initialize(repository)
    drives = _drive_classification(
        repository, from_season=from_season, to_season=to_season,
        metric_version=metric_version)
    clauses = ["m.metric_version=?", "p.drive_id IS NOT NULL"]
    params: list[Any] = [PBP_METRIC_VERSION]
    if from_season is not None:
        clauses.append("p.season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("p.season<=?"); params.append(int(to_season))
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    with repository._reader() as connection:
        for row in connection.execute(f"""
          SELECT p.game_id,p.drive_id,p.offense,p.defense,p.play_number,p.period,
                 p.clock_minutes,p.clock_seconds,p.down,p.distance,p.yards_gained,
                 p.scoring,p.play_type,p.play_text,m.rush_pass,m.success,m.garbage_time
          FROM cfb_plays p JOIN cfb_play_metrics m USING(play_id)
          WHERE {' AND '.join(clauses)} ORDER BY p.game_id,p.drive_number,p.play_number
        """, params):
            grouped[(int(row["game_id"]), str(row["drive_id"]))].append(dict(row))

    counts: dict[tuple[int, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    opponents = {}
    for key, rows in grouped.items():
        info = drives.get(key)
        if not info or not info["meaningful"]:
            continue
        team = str(info["offense"]); opponent = str(rows[0].get("defense") or "")
        opponents[(key[0], team)] = opponent
        counts[(key[0], team)]["meaningful_drives"] += 1
        counts[(key[0], team)][_outcome(rows)] += 1

    now = datetime.now(timezone.utc).isoformat()
    output = []
    for (game_id, team), count in counts.items():
        drives_n = count["meaningful_drives"]
        touchdowns = count["touchdowns"]; field_goals = count["field_goals"]
        points = touchdowns * 7.0 + field_goals * 3.0
        output.append((
            game_id, team, opponents.get((game_id, team), ""), metric_version, drives_n,
            touchdowns, field_goals, count["turnovers"], count["punts"],
            count["turnovers_on_downs"], count["end_half_drives"], count["other_drives"],
            points, touchdowns / drives_n, field_goals / drives_n,
            count["turnovers"] / drives_n, count["punts"] / drives_n,
            points / drives_n, now,
        ))
    with closing(repository._connect()) as connection:
        if from_season is not None or to_season is not None:
            filters, values = [], []
            if from_season is not None:
                filters.append("season>=?"); values.append(int(from_season))
            if to_season is not None:
                filters.append("season<=?"); values.append(int(to_season))
            connection.execute(f"""DELETE FROM cfb_team_game_drive_outcomes
              WHERE metric_version=? AND game_id IN
              (SELECT game_id FROM games WHERE {' AND '.join(filters)})""",
              (metric_version, *values))
        else:
            connection.execute(
                "DELETE FROM cfb_team_game_drive_outcomes WHERE metric_version=?",
                (metric_version,))
        connection.executemany(
            "INSERT INTO cfb_team_game_drive_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            output)
        connection.commit()
    return {"metric_version": metric_version, "from_season": from_season,
            "to_season": to_season, "team_game_rows": len(output)}


def game_summary(repository, game_id: int,
                 *, metric_version: str = METRIC_VERSION) -> list[dict[str, Any]]:
    initialize(repository)
    with repository._reader() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM cfb_team_game_drive_outcomes WHERE game_id=? AND metric_version=? ORDER BY team",
            (int(game_id), metric_version))]


#: Outcome counts summed by season_summary(), in the order a reader expects
#: to see a drive resolve: score, then how it was given away or ended.
_OUTCOME_COLUMNS = ("touchdowns", "field_goals", "turnovers", "punts",
                    "turnovers_on_downs", "end_half_drives", "other_drives")


def season_summary(repository, team: str, season: int, *,
                   metric_version: str = METRIC_VERSION) -> dict[str, Any] | None:
    """This team's drive outcomes summed across every game recorded this season."""
    initialize(repository)
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT o.* FROM cfb_team_game_drive_outcomes o
               JOIN games g ON g.game_id = o.game_id
               WHERE o.team=? AND g.season=? AND o.metric_version=?""",
            (str(team), int(season), metric_version)).fetchall()
    if not rows:
        return None
    totals = {"meaningful_drives": 0, **{key: 0 for key in _OUTCOME_COLUMNS}}
    for row in rows:
        totals["meaningful_drives"] += row["meaningful_drives"] or 0
        for key in _OUTCOME_COLUMNS:
            totals[key] += row[key] or 0
    denominator = totals["meaningful_drives"] or 1
    shares = {key: round(100 * totals[key] / denominator, 1) for key in _OUTCOME_COLUMNS}
    return {"games": len(rows), "meaningful_drives": totals["meaningful_drives"],
            "counts": {key: totals[key] for key in _OUTCOME_COLUMNS}, "shares": shares}
