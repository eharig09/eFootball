"""Precomputed team-game drive counts and pace, built from in-house pbp-v1.

`pace.py` already measures tempo and pass tendency by score state, and
`coordinator_pace.py` already counts real drives per game -- both correctly,
but only as request-time functions over raw play-by-play. Neither persists a
per-team-game row, and neither separates a real possession from a
kneel-down or an overtime drive. This table does both, so a drive/pace
estimator has a stable, joinable, trend-able actuals table to backtest
against instead of rescanning cfb_plays per game.

A "meaningful" drive excludes overtime (period >= 5 faces neither a play
clock nor real field position pressure, so it is tracked separately as
ot_drives and never blended into pace) and victory-kneel drives: a drive
where every play carrying a down is a kneel. Detection is off `play_text`
(`... kneels at the ...`), not `play_type`, because CFBD tags some kneels as
`Rush` and others as `Uncategorized` -- `play_type` alone misses half of
them. A drive that already converted first downs and kneels out the final
snap keeps its earlier plays; only an all-kneel drive is dropped.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb.repository import schema_once

METRIC_VERSION = "team-game-pace-v1"
PBP_METRIC_VERSION = "pbp-v1"

#: Same rule pace.py and coordinator_pace.py use: a gap to the previous snap
#: on the same drive longer than this is a period/timeout/review artifact,
#: not tempo.
_MAX_INTERVAL_SECONDS = 60


@schema_once("team_game_pace")
def initialize(repository) -> None:
    from sports_aggregator.cfb.play_by_play import initialize as initialize_pbp
    initialize_pbp(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_team_game_pace (
          game_id INTEGER NOT NULL,
          team TEXT NOT NULL,
          opponent TEXT NOT NULL,
          metric_version TEXT NOT NULL,
          raw_drives INTEGER NOT NULL,
          meaningful_drives INTEGER NOT NULL,
          ot_drives INTEGER NOT NULL,
          three_and_out_drives INTEGER NOT NULL,
          scrimmage_plays INTEGER NOT NULL,
          pass_plays INTEGER,
          rush_plays INTEGER,
          pass_yards INTEGER,
          rush_yards INTEGER,
          yards_per_dropback REAL,
          yards_per_rush REAL,
          plays_per_meaningful_drive REAL,
          seconds_per_play REAL,
          neutral_seconds_per_play REAL,
          pass_rate REAL,
          neutral_pass_rate REAL,
          success_rate REAL,
          explosive_rate REAL,
          first_down_rate REAL,
          three_and_out_rate REAL,
          tempo_intervals INTEGER NOT NULL,
          neutral_tempo_intervals INTEGER NOT NULL,
          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,metric_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_team_game_pace_team
          ON cfb_team_game_pace(team,metric_version,game_id);
        """)
        # `pass_plays`/`rush_plays` were added after this table's first
        # release -- migrate an already-existing one the same way
        # `play_detail.py` backfills its own additions.
        existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(cfb_team_game_pace)")}
        for column, sql_type in (
            ("pass_plays", "INTEGER"), ("rush_plays", "INTEGER"),
            ("pass_yards", "INTEGER"), ("rush_yards", "INTEGER"),
            ("yards_per_dropback", "REAL"), ("yards_per_rush", "REAL"),
        ):
            if column not in existing:
                connection.execute(f"ALTER TABLE cfb_team_game_pace ADD COLUMN {column} {sql_type}")
        connection.commit()


def _game_seconds_remaining(period: int | None, minutes: int | None, seconds: int | None) -> int | None:
    if period is None or minutes is None or seconds is None:
        return None
    clock = int(minutes) * 60 + int(seconds)
    if period <= 4:
        return max(0, (4 - period) * 900 + clock)
    return max(0, clock)


def _drive_classification(repository, *, from_season: int | None, to_season: int | None,
                          metric_version: str) -> dict[tuple[int, str], dict[str, Any]]:
    """One row per drive: is it meaningful, OT, a three-and-out; who ran it.

    Streamed and classified in Python rather than folded into a single SQL
    aggregate so the same classification rules feed both this function and
    the play-level pass computed in `_play_rates`, without restating them
    twice in SQL.
    """
    clauses = ["p.drive_id IS NOT NULL"]
    params: list[Any] = []
    if from_season is not None:
        clauses.append("p.season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("p.season<=?"); params.append(int(to_season))
    sql = f"""
      WITH ranked AS (
        SELECT p.game_id, p.drive_id, p.offense, p.down, p.play_text, p.play_type, p.period,
               ROW_NUMBER() OVER (PARTITION BY p.game_id,p.drive_id ORDER BY p.play_number DESC) AS rn
        FROM cfb_plays p
        WHERE {' AND '.join(clauses)}
      )
      SELECT game_id, drive_id, offense,
             MIN(period) AS period,
             SUM(CASE WHEN down IS NOT NULL THEN 1 ELSE 0 END) AS down_plays,
             SUM(CASE WHEN down IS NOT NULL AND play_text LIKE '%kneel%' THEN 1 ELSE 0 END) AS kneel_plays,
             MAX(CASE WHEN rn=1 THEN play_type END) AS end_play_type
      FROM ranked
      GROUP BY game_id, drive_id
    """
    drives: dict[tuple[int, str], dict[str, Any]] = {}
    with closing(repository._connect()) as connection:
        for row in connection.execute(sql, params):
            game_id, drive_id = int(row["game_id"]), str(row["drive_id"])
            period = row["period"]
            down_plays = int(row["down_plays"] or 0)
            kneel_plays = int(row["kneel_plays"] or 0)
            meaningful = bool(period is not None and period <= 4
                              and down_plays > 0 and kneel_plays < down_plays)
            drives[(game_id, drive_id)] = {
                "offense": str(row["offense"]),
                "period": period,
                "down_plays": down_plays,
                "meaningful": meaningful,
                "ot": bool(period is not None and period >= 5),
                "end_play_type": str(row["end_play_type"] or ""),
            }

        # Backfill drive-level plays/points from the already-derived table
        # rather than recomputing them here.
        dm_clauses = [f"metric_version=?"]
        dm_params: list[Any] = [PBP_METRIC_VERSION]
        if from_season is not None or to_season is not None:
            game_clauses = []
            if from_season is not None:
                game_clauses.append("season>=?"); dm_params.append(int(from_season))
            if to_season is not None:
                game_clauses.append("season<=?"); dm_params.append(int(to_season))
            dm_clauses.append(f"game_id IN (SELECT game_id FROM games WHERE {' AND '.join(game_clauses)})")
        for row in connection.execute(
            f"SELECT game_id,drive_id,points FROM cfb_drive_metrics WHERE {' AND '.join(dm_clauses)}",
            dm_params,
        ):
            key = (int(row["game_id"]), str(row["drive_id"]))
            info = drives.get(key)
            if info is not None:
                info["points"] = int(row["points"] or 0)
    return drives


def _three_and_out(info: dict[str, Any]) -> bool:
    if not info["meaningful"] or info.get("points", 0) != 0 or info["down_plays"] > 3:
        return False
    end_type = info["end_play_type"].casefold()
    return "interception" not in end_type and "fumble" not in end_type


def _play_rates(repository, drives: dict[tuple[int, str], dict[str, Any]], *,
                from_season: int | None, to_season: int | None,
                metric_version: str) -> dict[tuple[int, str], dict[str, Any]]:
    """Pass rate, success, explosiveness, first downs and tempo, meaningful drives only."""
    clauses = ["m.metric_version=?", "m.rush_pass IN ('rush','pass')", "COALESCE(m.garbage_time,0)=0"]
    params: list[Any] = [metric_version]
    if from_season is not None:
        clauses.append("p.season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("p.season<=?"); params.append(int(to_season))
    sql = f"""
      SELECT p.game_id,p.offense,p.drive_id,p.play_number,p.period,p.clock_minutes,p.clock_seconds,
             p.offense_score,p.defense_score,p.down,p.distance,p.yards_gained,
             m.rush_pass,m.success,m.explosive
      FROM cfb_plays p JOIN cfb_play_metrics m USING(play_id)
      WHERE {' AND '.join(clauses)}
      ORDER BY p.game_id,p.drive_id,p.play_number
    """
    teams: dict[tuple[int, str], dict[str, Any]] = defaultdict(lambda: {
        "scrimmage_plays": 0, "pass_plays": 0,
        "success_sum": 0, "success_count": 0,
        "explosive_sum": 0,
        "first_down_sum": 0, "first_down_count": 0,
        "pass_yards": 0, "rush_yards": 0,
        "interval_seconds": 0.0, "intervals": 0,
        "neutral_interval_seconds": 0.0, "neutral_intervals": 0,
        "neutral_plays": 0, "neutral_pass_plays": 0,
    })
    previous_remaining: dict[tuple[int, str], int] = {}
    with closing(repository._connect()) as connection:
        for row in connection.execute(sql, params):
            game_id, drive_id = int(row["game_id"]), str(row["drive_id"])
            info = drives.get((game_id, drive_id))
            if info is None or not info["meaningful"]:
                continue
            team_key = (game_id, str(row["offense"]))
            bucket = teams[team_key]
            is_pass = row["rush_pass"] == "pass"
            bucket["scrimmage_plays"] += 1
            bucket["pass_plays"] += int(is_pass)
            if row["success"] is not None:
                bucket["success_sum"] += int(row["success"])
                bucket["success_count"] += 1
            if row["explosive"] is not None:
                bucket["explosive_sum"] += int(row["explosive"])
            down, distance, gained = row["down"], row["distance"], row["yards_gained"]
            if gained is not None:
                bucket["pass_yards" if is_pass else "rush_yards"] += int(gained)
            if down is not None and distance is not None and gained is not None:
                bucket["first_down_count"] += 1
                bucket["first_down_sum"] += int(gained >= distance)

            margin = int(row["offense_score"] or 0) - int(row["defense_score"] or 0)
            neutral = abs(margin) <= 8
            if neutral:
                bucket["neutral_plays"] += 1
                bucket["neutral_pass_plays"] += int(is_pass)

            remaining = _game_seconds_remaining(row["period"], row["clock_minutes"], row["clock_seconds"])
            drive_key = (game_id, drive_id)
            prior = previous_remaining.get(drive_key)
            if remaining is not None and prior is not None:
                interval = prior - remaining
                if 0 < interval <= _MAX_INTERVAL_SECONDS:
                    bucket["interval_seconds"] += interval
                    bucket["intervals"] += 1
                    if neutral:
                        bucket["neutral_interval_seconds"] += interval
                        bucket["neutral_intervals"] += 1
            if remaining is not None:
                previous_remaining[drive_key] = remaining
    return teams


def build(repository, *, from_season: int | None = None, to_season: int | None = None,
         metric_version: str = METRIC_VERSION) -> dict[str, Any]:
    """Rebuild cfb_team_game_pace for a season range."""
    initialize(repository)
    drives = _drive_classification(repository, from_season=from_season, to_season=to_season,
                                   metric_version=metric_version)
    rates = _play_rates(repository, drives, from_season=from_season, to_season=to_season,
                        metric_version=PBP_METRIC_VERSION)

    drive_totals: dict[tuple[int, str], dict[str, int]] = defaultdict(lambda: {
        "raw": 0, "meaningful": 0, "ot": 0, "three_and_out": 0,
    })
    for (game_id, _drive_id), info in drives.items():
        key = (game_id, info["offense"])
        totals = drive_totals[key]
        totals["raw"] += 1
        totals["meaningful"] += int(info["meaningful"])
        totals["ot"] += int(info["ot"])
        totals["three_and_out"] += int(_three_and_out(info))

    with closing(repository._connect()) as connection:
        opponents: dict[int, dict[str, str]] = {}
        game_ids = {game_id for game_id, _ in drive_totals}
        if game_ids:
            placeholders = ",".join("?" for _ in game_ids)
            for row in connection.execute(
                f"SELECT game_id,home_team,away_team FROM games WHERE game_id IN ({placeholders})",
                list(game_ids),
            ):
                opponents[int(row["game_id"])] = {"home": row["home_team"], "away": row["away_team"]}

        now = datetime.now(timezone.utc).isoformat()
        output = []
        for (game_id, team), totals in drive_totals.items():
            teams_at_game = opponents.get(game_id) or {}
            if teams_at_game.get("home") == team:
                opponent = teams_at_game.get("away") or ""
            elif teams_at_game.get("away") == team:
                opponent = teams_at_game.get("home") or ""
            else:
                opponent = ""
            rate = rates.get((game_id, team), {})
            scrimmage_plays = int(rate.get("scrimmage_plays", 0))
            meaningful = totals["meaningful"]
            success_count = int(rate.get("success_count", 0))
            first_down_count = int(rate.get("first_down_count", 0))
            intervals = int(rate.get("intervals", 0))
            neutral_intervals = int(rate.get("neutral_intervals", 0))
            pass_plays = int(rate.get("pass_plays", 0))
            rush_plays = scrimmage_plays - pass_plays
            pass_yards = int(rate.get("pass_yards", 0))
            rush_yards = int(rate.get("rush_yards", 0))
            output.append((
                game_id, team, opponent, metric_version,
                totals["raw"], meaningful, totals["ot"], totals["three_and_out"],
                scrimmage_plays, pass_plays, rush_plays,
                pass_yards, rush_yards,
                (pass_yards / pass_plays) if pass_plays else None,
                (rush_yards / rush_plays) if rush_plays else None,
                (scrimmage_plays / meaningful) if meaningful else None,
                (rate.get("interval_seconds", 0) / intervals) if intervals else None,
                (rate.get("neutral_interval_seconds", 0) / neutral_intervals) if neutral_intervals else None,
                (pass_plays / scrimmage_plays) if scrimmage_plays else None,
                (rate.get("neutral_pass_plays", 0) / rate["neutral_plays"]) if rate.get("neutral_plays") else None,
                (rate.get("success_sum", 0) / success_count) if success_count else None,
                (rate.get("explosive_sum", 0) / scrimmage_plays) if scrimmage_plays else None,
                (rate.get("first_down_sum", 0) / first_down_count) if first_down_count else None,
                (totals["three_and_out"] / meaningful) if meaningful else None,
                intervals, neutral_intervals, now,
            ))

        if from_season is not None or to_season is not None:
            game_clauses = []
            season_params: list[Any] = []
            if from_season is not None:
                game_clauses.append("season>=?"); season_params.append(int(from_season))
            if to_season is not None:
                game_clauses.append("season<=?"); season_params.append(int(to_season))
            connection.execute(f"""DELETE FROM cfb_team_game_pace
              WHERE metric_version=? AND game_id IN (SELECT game_id FROM games WHERE {' AND '.join(game_clauses)})""",
              (metric_version, *season_params))
        else:
            connection.execute("DELETE FROM cfb_team_game_pace WHERE metric_version=?", (metric_version,))

        connection.executemany("""INSERT INTO cfb_team_game_pace(
          game_id,team,opponent,metric_version,raw_drives,meaningful_drives,ot_drives,three_and_out_drives,
          scrimmage_plays,pass_plays,rush_plays,pass_yards,rush_yards,yards_per_dropback,yards_per_rush,
          plays_per_meaningful_drive,seconds_per_play,neutral_seconds_per_play,
          pass_rate,neutral_pass_rate,success_rate,explosive_rate,first_down_rate,three_and_out_rate,
          tempo_intervals,neutral_tempo_intervals,built_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)
        connection.commit()

    return {
        "metric_version": metric_version,
        "from_season": from_season,
        "to_season": to_season,
        "team_game_rows": len(output),
    }


def game_summary(repository, game_id: int, *, metric_version: str = METRIC_VERSION) -> list[dict[str, Any]]:
    initialize(repository)
    with repository._reader() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM cfb_team_game_pace WHERE game_id=? AND metric_version=? ORDER BY team",
            (int(game_id), metric_version)).fetchall()]


def team_weekly_trend(repository, team: str, season: int, *,
                      metric_version: str = METRIC_VERSION) -> list[dict[str, Any]]:
    initialize(repository)
    with repository._reader() as connection:
        rows = connection.execute("""
          SELECT g.week, a.meaningful_drives, a.scrimmage_plays, a.plays_per_meaningful_drive,
                 a.seconds_per_play, a.neutral_seconds_per_play, a.pass_rate, a.neutral_pass_rate,
                 a.success_rate, a.explosive_rate, a.first_down_rate, a.three_and_out_rate
          FROM cfb_team_game_pace a
          JOIN games g ON g.game_id = a.game_id
          WHERE a.team = ? AND g.season = ? AND a.metric_version = ?
          ORDER BY g.week
        """, (str(team), int(season), metric_version)).fetchall()
    return [dict(row) for row in rows]


def team_weekly_counting_stats(repository, team: str, season: int, *,
                               metric_version: str = METRIC_VERSION,
                               drive_metric_version: str | None = None) -> list[dict[str, Any]]:
    """Raw weekly volume: plays, yards, and scoring-drive outcomes.

    Pulled from cfb_team_game_pace (plays/yards, always present once pace is
    built) left-joined to cfb_team_game_drive_outcomes (TD/FG/turnover/punt
    counts, a separate, optional build) so a week missing the drive-outcomes
    build still reports plays and yards rather than being dropped entirely.
    """
    from sports_aggregator.cfb.team_game_drive_outcomes import (
        METRIC_VERSION as DEFAULT_DRIVE_METRIC_VERSION, initialize as initialize_drive_outcomes,
    )
    initialize(repository)
    initialize_drive_outcomes(repository)
    drive_version = drive_metric_version or DEFAULT_DRIVE_METRIC_VERSION
    with repository._reader() as connection:
        rows = connection.execute("""
          SELECT g.week, p.scrimmage_plays, p.pass_plays, p.rush_plays,
                 p.pass_yards, p.rush_yards, (p.pass_yards + p.rush_yards) total_yards,
                 d.touchdowns, d.field_goals, d.turnovers, d.punts
          FROM cfb_team_game_pace p
          JOIN games g ON g.game_id = p.game_id
          LEFT JOIN cfb_team_game_drive_outcomes d
            ON d.game_id = p.game_id AND d.team = p.team AND d.metric_version = ?
          WHERE p.team = ? AND g.season = ? AND p.metric_version = ?
          ORDER BY g.week
        """, (drive_version, str(team), int(season), metric_version)).fetchall()
    return [dict(row) for row in rows]
