"""Team-game turnover and red-zone actuals for projection Milestone 9.

The table deliberately contains outcomes, not forecasts.  It reuses the
meaningful-drive definition from ``team_game_pace`` (regulation, at least one
real snap, not an all-kneel possession) and the shared PBP garbage-time flag.
Only opponent-recovered fumbles count as fumbles lost, and defensive return
touchdowns are never credited to the offense.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.team_game_pace import _drive_classification

METRIC_VERSION = "team-game-scoring-v1"
PBP_METRIC_VERSION = "pbp-v1"

_INTERCEPTION_TYPES = {
    "interception", "pass interception return", "interception return touchdown",
}
_FUMBLE_LOST_TYPES = {"fumble recovery (opponent)", "fumble return touchdown"}


@schema_once("team_game_scoring")
def initialize(repository) -> None:
    from sports_aggregator.cfb.play_by_play import initialize as initialize_pbp
    initialize_pbp(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_team_game_scoring (
          game_id INTEGER NOT NULL,
          team TEXT NOT NULL,
          opponent TEXT NOT NULL,
          metric_version TEXT NOT NULL,
          meaningful_drives INTEGER NOT NULL,
          competitive_plays INTEGER NOT NULL,
          giveaways INTEGER NOT NULL,
          interceptions INTEGER NOT NULL,
          fumbles_lost INTEGER NOT NULL,
          giveaway_rate REAL,
          red_zone_trips INTEGER NOT NULL,
          red_zone_touchdowns INTEGER NOT NULL,
          red_zone_field_goal_attempts INTEGER NOT NULL,
          red_zone_td_rate REAL,
          red_zone_pass_plays INTEGER NOT NULL,
          red_zone_rush_plays INTEGER NOT NULL,
          red_zone_pass_rate REAL,
          goal_to_go_trips INTEGER NOT NULL,
          goal_to_go_touchdowns INTEGER NOT NULL,
          goal_to_go_td_rate REAL,
          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,metric_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_team_game_scoring_team
          ON cfb_team_game_scoring(team,metric_version,game_id);
        """)
        connection.commit()


def _is_offensive_touchdown(row: dict[str, Any]) -> bool:
    if not row.get("scoring") or row.get("rush_pass") not in {"rush", "pass"}:
        return False
    play_type = str(row.get("play_type") or "").casefold()
    return not any(token in play_type for token in (
        "interception", "fumble return", "punt return", "kickoff return",
        "blocked", "defensive", "safety",
    ))


def build(repository, *, from_season: int | None = None,
          to_season: int | None = None,
          metric_version: str = METRIC_VERSION) -> dict[str, Any]:
    """Rebuild team-game scoring-opportunity actuals for a season range."""
    initialize(repository)
    drives = _drive_classification(
        repository, from_season=from_season, to_season=to_season,
        metric_version=metric_version,
    )
    clauses = ["m.metric_version=?", "p.drive_id IS NOT NULL"]
    params: list[Any] = [PBP_METRIC_VERSION]
    if from_season is not None:
        clauses.append("p.season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("p.season<=?"); params.append(int(to_season))

    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    with closing(repository._connect()) as connection:
        for row in connection.execute(f"""
          SELECT p.game_id,p.drive_id,p.offense,p.defense,p.play_number,p.down,p.distance,
                 p.yards_to_goal,p.scoring,p.play_type,m.rush_pass,m.garbage_time
          FROM cfb_plays p JOIN cfb_play_metrics m USING(play_id)
          WHERE {' AND '.join(clauses)}
          ORDER BY p.game_id,p.drive_id,p.play_number
        """, params):
            grouped[(int(row["game_id"]), str(row["drive_id"]))].append(dict(row))

    totals: dict[tuple[int, str], dict[str, int]] = defaultdict(lambda: {
        "meaningful_drives": 0, "competitive_plays": 0,
        "giveaways": 0, "interceptions": 0, "fumbles_lost": 0,
        "red_zone_trips": 0, "red_zone_touchdowns": 0,
        "red_zone_field_goal_attempts": 0,
        "red_zone_pass_plays": 0, "red_zone_rush_plays": 0,
        "goal_to_go_trips": 0, "goal_to_go_touchdowns": 0,
    })
    opponents: dict[tuple[int, str], str] = {}
    for key, rows in grouped.items():
        info = drives.get(key)
        if not info or not info["meaningful"] or not rows:
            continue
        game_id = key[0]
        team = str(info["offense"])
        opponent = str(rows[0].get("defense") or "")
        opponents[(game_id, team)] = opponent
        bucket = totals[(game_id, team)]
        bucket["meaningful_drives"] += 1
        competitive = [row for row in rows if not row.get("garbage_time")]
        scrimmage = [row for row in competitive if row.get("rush_pass") in {"rush", "pass"}]
        bucket["competitive_plays"] += len(scrimmage)

        interceptions = sum(
            str(row.get("play_type") or "").casefold() in _INTERCEPTION_TYPES
            for row in competitive)
        fumbles_lost = sum(
            str(row.get("play_type") or "").casefold() in _FUMBLE_LOST_TYPES
            for row in competitive)
        bucket["interceptions"] += interceptions
        bucket["fumbles_lost"] += fumbles_lost
        bucket["giveaways"] += interceptions + fumbles_lost

        red_zone = [row for row in competitive
                    if row.get("yards_to_goal") is not None and row["yards_to_goal"] <= 20]
        goal_to_go = [row for row in red_zone
                      if row.get("down") is not None and row.get("distance") is not None
                      and row["distance"] >= row["yards_to_goal"]]
        offensive_td = any(_is_offensive_touchdown(row) for row in competitive)
        if red_zone:
            bucket["red_zone_trips"] += 1
            bucket["red_zone_touchdowns"] += int(offensive_td)
            bucket["red_zone_field_goal_attempts"] += int(any(
                "field goal" in str(row.get("play_type") or "").casefold()
                for row in competitive))
            bucket["red_zone_pass_plays"] += sum(row.get("rush_pass") == "pass" for row in red_zone)
            bucket["red_zone_rush_plays"] += sum(row.get("rush_pass") == "rush" for row in red_zone)
        if goal_to_go:
            bucket["goal_to_go_trips"] += 1
            bucket["goal_to_go_touchdowns"] += int(offensive_td)

    now = datetime.now(timezone.utc).isoformat()
    output = []
    for (game_id, team), bucket in totals.items():
        plays = bucket["competitive_plays"]
        rz_plays = bucket["red_zone_pass_plays"] + bucket["red_zone_rush_plays"]
        trips = bucket["red_zone_trips"]
        gtg = bucket["goal_to_go_trips"]
        output.append((
            game_id, team, opponents.get((game_id, team), ""), metric_version,
            bucket["meaningful_drives"], plays,
            bucket["giveaways"], bucket["interceptions"], bucket["fumbles_lost"],
            bucket["giveaways"] / plays if plays else None,
            trips, bucket["red_zone_touchdowns"], bucket["red_zone_field_goal_attempts"],
            bucket["red_zone_touchdowns"] / trips if trips else None,
            bucket["red_zone_pass_plays"], bucket["red_zone_rush_plays"],
            bucket["red_zone_pass_plays"] / rz_plays if rz_plays else None,
            gtg, bucket["goal_to_go_touchdowns"],
            bucket["goal_to_go_touchdowns"] / gtg if gtg else None,
            now,
        ))

    with closing(repository._connect()) as connection:
        if from_season is not None or to_season is not None:
            filters, season_params = [], []
            if from_season is not None:
                filters.append("season>=?"); season_params.append(int(from_season))
            if to_season is not None:
                filters.append("season<=?"); season_params.append(int(to_season))
            connection.execute(f"""DELETE FROM cfb_team_game_scoring
              WHERE metric_version=? AND game_id IN
                (SELECT game_id FROM games WHERE {' AND '.join(filters)})""",
                (metric_version, *season_params))
        else:
            connection.execute(
                "DELETE FROM cfb_team_game_scoring WHERE metric_version=?", (metric_version,))
        connection.executemany("""INSERT INTO cfb_team_game_scoring(
          game_id,team,opponent,metric_version,meaningful_drives,competitive_plays,
          giveaways,interceptions,fumbles_lost,giveaway_rate,red_zone_trips,
          red_zone_touchdowns,red_zone_field_goal_attempts,red_zone_td_rate,
          red_zone_pass_plays,red_zone_rush_plays,red_zone_pass_rate,
          goal_to_go_trips,goal_to_go_touchdowns,goal_to_go_td_rate,built_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)
        connection.commit()
    return {"metric_version": metric_version, "from_season": from_season,
            "to_season": to_season, "team_game_rows": len(output)}


def game_summary(repository, game_id: int,
                 *, metric_version: str = METRIC_VERSION) -> list[dict[str, Any]]:
    initialize(repository)
    with repository._reader() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM cfb_team_game_scoring WHERE game_id=? AND metric_version=? ORDER BY team",
            (int(game_id), metric_version)).fetchall()]
