"""Team-game field-position and special-teams actuals (Milestone 10).

Kick and punt rows describe the pre-kick spot, not the receiving team's final
field position.  Starts are therefore reconstructed from the first competitive
scrimmage snap of the receiving possession.  This also incorporates return and
coverage penalties instead of trusting a brittle play-text parser.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.team_game_pace import _drive_classification

METRIC_VERSION = "team-game-special-teams-v1"
PBP_METRIC_VERSION = "pbp-v1"


@schema_once("team_game_special_teams")
def initialize(repository) -> None:
    from sports_aggregator.cfb.play_by_play import initialize as initialize_pbp
    initialize_pbp(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_team_game_special_teams (
          game_id INTEGER NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
          metric_version TEXT NOT NULL,
          offensive_starts INTEGER NOT NULL, start_yards_to_goal_total REAL,
          average_start_yards_to_goal REAL,
          kickoff_returns INTEGER NOT NULL, kickoff_start_yards_to_goal_total REAL,
          average_start_after_kickoff REAL,
          punt_returns INTEGER NOT NULL, punt_start_yards_to_goal_total REAL,
          average_start_after_punt REAL,
          punts INTEGER NOT NULL, net_punt_yards_total REAL, average_net_punt_yards REAL,
          punts_inside_20 INTEGER NOT NULL, punt_inside_20_rate REAL,
          opponent_punt_start_yards_to_goal_total REAL, average_opponent_start_after_punt REAL,
          kickoffs INTEGER NOT NULL, opponent_kickoff_start_yards_to_goal_total REAL,
          average_opponent_start_after_kickoff REAL,
          field_goal_attempts INTEGER NOT NULL, field_goals_made INTEGER NOT NULL,
          field_goal_distance_total REAL, average_field_goal_distance REAL,
          field_goal_accuracy REAL, built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,metric_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_team_game_special_teams_team
          ON cfb_team_game_special_teams(team,metric_version,game_id);
        """)
        connection.commit()


def _bucket() -> dict[str, float]:
    return defaultdict(float)


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
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(f"""
          SELECT p.game_id,p.drive_id,p.drive_number,p.play_number,p.offense,p.defense,
                 p.yards_to_goal,p.down,p.distance,p.play_type,p.play_text,p.scoring,
                 m.rush_pass,m.garbage_time
          FROM cfb_plays p JOIN cfb_play_metrics m USING(play_id)
          WHERE {' AND '.join(clauses)}
          ORDER BY p.game_id,p.drive_number,p.play_number
        """, params)]

    game_drives: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    drive_order: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        game_id, drive_id = int(row["game_id"]), str(row["drive_id"])
        if drive_id not in game_drives[game_id]:
            drive_order[game_id].append(drive_id)
        game_drives[game_id][drive_id].append(row)

    totals: dict[tuple[int, str], dict[str, float]] = defaultdict(_bucket)
    opponents: dict[tuple[int, str], str] = {}
    for game_id, ordered_ids in drive_order.items():
        drives_in_game = game_drives[game_id]
        # First meaningful offensive snap is the only trustworthy post-kick spot.
        starts: dict[str, tuple[str, str, float]] = {}
        for drive_id in ordered_ids:
            info = drives.get((game_id, drive_id))
            group = drives_in_game[drive_id]
            if not info or not info["meaningful"]:
                continue
            first = next((row for row in group
                          if not row.get("garbage_time")
                          and row.get("rush_pass") in {"rush", "pass"}
                          and row.get("yards_to_goal") is not None), None)
            if first:
                starts[drive_id] = (str(info["offense"]), str(first["defense"]),
                                    float(first["yards_to_goal"]))

        for index, drive_id in enumerate(ordered_ids):
            group = drives_in_game[drive_id]
            info = drives.get((game_id, drive_id))
            start = starts.get(drive_id)
            if start:
                team, opponent, start_ytg = start
                opponents[(game_id, team)] = opponent
                bucket = totals[(game_id, team)]
                bucket["offensive_starts"] += 1
                bucket["start_yards_to_goal_total"] += start_ytg
                first_type = str(group[0].get("play_type") or "").casefold()
                previous = drives_in_game[ordered_ids[index - 1]] if index else []
                previous_type = str(previous[-1].get("play_type") or "").casefold() if previous else ""
                if "kickoff" in first_type:
                    bucket["kickoff_returns"] += 1
                    bucket["kickoff_start_yards_to_goal_total"] += start_ytg
                elif "punt" in previous_type:
                    bucket["punt_returns"] += 1
                    bucket["punt_start_yards_to_goal_total"] += start_ytg

            # A punt belongs to the drive's offense. Match it to the next
            # meaningful opponent start rather than parsing return prose.
            punt = next((row for row in reversed(group)
                         if "punt" in str(row.get("play_type") or "").casefold()
                         and "touchdown" not in str(row.get("play_type") or "").casefold()), None)
            if punt and info:
                team = str(info["offense"]); opponent = str(punt.get("defense") or "")
                opponents[(game_id, team)] = opponent
                next_start = None
                for next_id in ordered_ids[index + 1:]:
                    candidate = starts.get(next_id)
                    if candidate and candidate[0] == opponent:
                        next_start = candidate[2]; break
                if next_start is not None and punt.get("yards_to_goal") is not None:
                    bucket = totals[(game_id, team)]
                    net = float(punt["yards_to_goal"]) + next_start - 100.0
                    bucket["punts"] += 1
                    bucket["net_punt_yards_total"] += net
                    bucket["punts_inside_20"] += int(next_start >= 80)
                    bucket["opponent_punt_start_yards_to_goal_total"] += next_start

            # Kickoff rows name the receiving side as offense and the kicking
            # side as defense. The same-drive first snap is its landing result.
            kickoff = next((row for row in group
                            if "kickoff" in str(row.get("play_type") or "").casefold()), None)
            if kickoff and start:
                receiving, kicking, start_ytg = start
                opponents[(game_id, kicking)] = receiving
                bucket = totals[(game_id, kicking)]
                bucket["kickoffs"] += 1
                bucket["opponent_kickoff_start_yards_to_goal_total"] += start_ytg

            for row in group:
                play_type = str(row.get("play_type") or "").casefold()
                if "field goal" not in play_type or info is None:
                    continue
                team = str(info["offense"]); opponent = str(row.get("defense") or "")
                opponents[(game_id, team)] = opponent
                bucket = totals[(game_id, team)]
                bucket["field_goal_attempts"] += 1
                bucket["field_goals_made"] += int("good" in play_type)
                if row.get("yards_to_goal") is not None:
                    bucket["field_goal_distance_total"] += float(row["yards_to_goal"]) + 17.0

    now = datetime.now(timezone.utc).isoformat()
    output = []
    for (game_id, team), b in totals.items():
        starts = int(b["offensive_starts"]); kos = int(b["kickoff_returns"])
        prs = int(b["punt_returns"]); punts = int(b["punts"]); kickoffs = int(b["kickoffs"])
        fga = int(b["field_goal_attempts"]); fgm = int(b["field_goals_made"])
        output.append((
            game_id, team, opponents.get((game_id, team), ""), metric_version,
            starts, b["start_yards_to_goal_total"], b["start_yards_to_goal_total"] / starts if starts else None,
            kos, b["kickoff_start_yards_to_goal_total"], b["kickoff_start_yards_to_goal_total"] / kos if kos else None,
            prs, b["punt_start_yards_to_goal_total"], b["punt_start_yards_to_goal_total"] / prs if prs else None,
            punts, b["net_punt_yards_total"], b["net_punt_yards_total"] / punts if punts else None,
            int(b["punts_inside_20"]), b["punts_inside_20"] / punts if punts else None,
            b["opponent_punt_start_yards_to_goal_total"],
            b["opponent_punt_start_yards_to_goal_total"] / punts if punts else None,
            kickoffs, b["opponent_kickoff_start_yards_to_goal_total"],
            b["opponent_kickoff_start_yards_to_goal_total"] / kickoffs if kickoffs else None,
            fga, fgm, b["field_goal_distance_total"],
            b["field_goal_distance_total"] / fga if fga else None,
            fgm / fga if fga else None, now,
        ))

    with closing(repository._connect()) as connection:
        if from_season is not None or to_season is not None:
            filters, values = [], []
            if from_season is not None:
                filters.append("season>=?"); values.append(int(from_season))
            if to_season is not None:
                filters.append("season<=?"); values.append(int(to_season))
            connection.execute(f"""DELETE FROM cfb_team_game_special_teams
              WHERE metric_version=? AND game_id IN
              (SELECT game_id FROM games WHERE {' AND '.join(filters)})""",
              (metric_version, *values))
        else:
            connection.execute("DELETE FROM cfb_team_game_special_teams WHERE metric_version=?",
                               (metric_version,))
        connection.executemany("""INSERT INTO cfb_team_game_special_teams VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)
        connection.commit()
    return {"metric_version": metric_version, "from_season": from_season,
            "to_season": to_season, "team_game_rows": len(output)}


def game_summary(repository, game_id: int,
                 *, metric_version: str = METRIC_VERSION) -> list[dict[str, Any]]:
    initialize(repository)
    with repository._reader() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM cfb_team_game_special_teams WHERE game_id=? AND metric_version=? ORDER BY team",
            (int(game_id), metric_version))]
