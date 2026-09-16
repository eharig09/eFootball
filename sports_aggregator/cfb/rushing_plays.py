"""Per-attempt rushing detail from CFBD, mirroring passing_plays.py's shape.

CFBD's /rushing/plays publishes a direction -- left, middle or right -- and
the rusher's own id for each carry, one row per attempt, the same way
/passing/plays does for throws. There is no further gap-width split (no
"end"/"tackle"/"guard" the way nflverse's run_location + run_gap combine
into a 7-cell chart for NFL); three cells is what CFBD's own charting
supports, so that is what this renders rather than inventing a finer split
the source data cannot back up.

Sacks and QB kneels are excluded: a sack is a broken pass play, not a
rushing decision, and a kneel is clock management, not offense -- counting
either would understate a real rushing attempt's average value the same way
including garbage time would.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Iterable

from sports_aggregator.cfb.repository import CFBRepository, schema_once


#: The EPA model the report and the matchup page read. See `pbp_cli`.
MODEL_VERSION = "ep-v2"

#: Below this a direction split is noise, not a tendency -- the same
#: reasoning and thresholds passing_plays.py uses for its own splits.
MIN_GAME_ATTEMPTS = 4
MIN_SEASON_ATTEMPTS = 25

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfbd_rushing_plays (
  play_id TEXT PRIMARY KEY,
  game_id INTEGER NOT NULL,
  season INTEGER NOT NULL,
  week INTEGER,
  offense TEXT NOT NULL,
  defense TEXT NOT NULL,
  rusher TEXT,
  rusher_id TEXT,
  down INTEGER,
  distance INTEGER,
  start_yards_to_goal INTEGER,
  rush_direction TEXT,
  rushing_yards REAL,
  is_touchdown INTEGER NOT NULL DEFAULT 0,
  is_sack INTEGER NOT NULL DEFAULT 0,
  is_kneel INTEGER NOT NULL DEFAULT 0,
  parse_status TEXT,
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rushing_plays_game ON cfbd_rushing_plays(game_id);
CREATE INDEX IF NOT EXISTS idx_rushing_plays_offense ON cfbd_rushing_plays(season, offense);
CREATE INDEX IF NOT EXISTS idx_rushing_plays_defense ON cfbd_rushing_plays(season, defense);
CREATE INDEX IF NOT EXISTS idx_rushing_plays_rusher ON cfbd_rushing_plays(season, rusher_id);
"""


@schema_once("rushing_plays")
def initialize(repository: CFBRepository) -> None:
    # Mirrors passing_plays.initialize(): the splits below join to
    # cfb_play_epa and cfb_play_metrics, so those modules are initialized
    # here too rather than assuming a caller already has.
    from sports_aggregator.cfb.expected_points_v2 import initialize as initialize_epa
    from sports_aggregator.cfb.play_by_play import initialize as initialize_plays
    repository.initialize()
    initialize_plays(repository)
    initialize_epa(repository)
    with closing(repository._connect()) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


def _row(item: dict[str, Any], now: str) -> tuple | None:
    play_id = item.get("playId")
    game_id = item.get("gameId")
    if play_id is None or game_id is None:
        return None
    offense = item.get("offense")
    defense = item.get("defense")
    if not offense or not defense:
        return None

    def identifier(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    return (
        str(play_id), int(game_id), int(item.get("season") or 0), item.get("week"),
        str(offense), str(defense), item.get("rusher"), identifier(item.get("rusherId")),
        item.get("down"), item.get("distance"), item.get("startYardsToGoal"),
        item.get("rushDirection"), item.get("rushingYards"),
        int(bool(item.get("isRushingTouchdown"))), int(bool(item.get("isSack"))),
        int(bool(item.get("isKneel"))), item.get("parseStatus"), now,
    )


def store_attempts(repository: CFBRepository, attempts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Upsert attempts. Re-running a week corrects rows rather than duplicating."""
    initialize(repository)
    now = datetime.now(timezone.utc).isoformat()
    rows = [row for row in (_row(item, now) for item in attempts) if row]
    if not rows:
        return {"stored": 0, "classified": 0}
    with repository.transaction() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO cfbd_rushing_plays VALUES (%s)" % ",".join("?" * 18), rows)
    classified = sum(1 for row in rows if row[11])
    return {"stored": len(rows), "classified": classified,
            "coverage": round(classified / len(rows), 4)}


def sync_week(repository: CFBRepository, client, *, season: int, week: int,
              force: bool = False) -> dict[str, Any]:
    raw = client.get("/rushing/plays", {"year": int(season), "week": int(week)}, force=force)
    result = store_attempts(repository, raw)
    result.update({"season": int(season), "week": int(week)})
    return result


def sync_season(repository: CFBRepository, client, *, season: int,
                weeks: Iterable[int] | None = None, force: bool = False) -> dict[str, Any]:
    """Every week of a season. The endpoint rejects a season-wide request."""
    totals = {"season": int(season), "stored": 0, "classified": 0, "weeks": [], "failures": []}
    for week in (weeks if weeks is not None else range(1, 17)):
        try:
            result = sync_week(repository, client, season=season, week=int(week), force=force)
        except Exception as exc:  # noqa: BLE001 - one bad week must not lose the rest
            totals["failures"].append({"week": int(week), "error": str(exc)[:200]})
            continue
        totals["stored"] += result["stored"]
        totals["classified"] += result["classified"]
        totals["weeks"].append(result)
    attempts = totals["stored"]
    totals["coverage"] = round(totals["classified"] / attempts, 4) if attempts else 0.0
    return totals


#: Where CFBD's own classification differs from a "real rushing decision":
#: neither a broken-play sack nor a clock-killing kneel should count toward a
#: direction's average value.
_REAL_RUSH = "COALESCE(p.is_sack,0)=0 AND COALESCE(p.is_kneel,0)=0"


def team_season_rushing(repository: CFBRepository, team: str, season: int, *,
                        role: str = "offense", model_version: str = MODEL_VERSION,
                        ) -> dict[str, Any]:
    """Direction-only rushing profile (CFBD has no gap-width split), including
    rusher output by zone and, for role="defense", a position-grouped
    allowed-by tally -- the same shape passing_plays.team_season_field uses,
    so the two render with the same template partial."""
    initialize(repository)
    column = "p.offense" if role == "offense" else "p.defense"
    with repository._reader() as connection:
        rows = connection.execute(
            f"""SELECT p.rush_direction,p.rushing_yards,p.is_touchdown,p.rusher,p.rusher_id,
                       e.epa,pl.position
                FROM cfbd_rushing_plays p
                LEFT JOIN cfb_play_epa e ON e.play_id=p.play_id AND e.model_version=?
                LEFT JOIN cfb_play_metrics m ON m.play_id=p.play_id
                LEFT JOIN players pl ON pl.player_id=p.rusher_id AND pl.season=p.season
                WHERE p.season=? AND {column}=? AND p.rush_direction IS NOT NULL
                  AND {_REAL_RUSH} AND COALESCE(m.garbage_time,0)=0""",
            (model_version, int(season), str(team))).fetchall()
    zones = {direction: {"attempts": 0, "yards": 0.0, "touchdowns": 0,
                         "epa": 0.0, "epa_plays": 0, "rushers": {}, "positions": {}}
             for direction in ("left", "middle", "right")}
    for row in rows:
        direction = str(row["rush_direction"] or "").casefold()
        if direction not in zones:
            continue
        zone = zones[direction]
        zone["attempts"] += 1
        zone["yards"] += float(row["rushing_yards"] or 0)
        zone["touchdowns"] += int(bool(row["is_touchdown"]))
        if row["epa"] is not None:
            zone["epa"] += float(row["epa"]); zone["epa_plays"] += 1
        rusher = str(row["rusher"] or "").strip()
        position = str(row["position"] or "UNK").upper()
        if rusher:
            entry = zone["rushers"].setdefault(rusher, {
                "name": rusher, "player_id": row["rusher_id"], "position": position,
                "attempts": 0, "yards": 0, "touchdowns": 0})
            entry["attempts"] += 1
            entry["yards"] += round(float(row["rushing_yards"] or 0))
            entry["touchdowns"] += int(bool(row["is_touchdown"]))
            if role == "defense":
                zone["positions"][position] = zone["positions"].get(position, 0) + 1
    output = {}
    for direction, zone in zones.items():
        rushers = sorted(zone.pop("rushers").values(),
                         key=lambda item: (-item["attempts"], -item["yards"], item["name"]))
        positions = zone.pop("positions")
        allowed_by_position = sorted(
            ({"position": position, "attempts": count} for position, count in positions.items()),
            key=lambda item: (-item["attempts"], item["position"])) if role == "defense" else []
        output[direction] = {**zone,
                             "epa_per_attempt": zone["epa"] / zone["epa_plays"] if zone["epa_plays"] else None,
                             "rushers": rushers[:5], "allowed_by_position": allowed_by_position}
    return {"team": team, "season": int(season), "role": role, "zones": output,
            "attempts": len(rows)}


def matchup_rushing(repository: CFBRepository, game: dict[str, Any], *,
                    model_version: str = MODEL_VERSION) -> list[dict[str, Any]]:
    """Both rushing offenses against the defense each will face."""
    season = int(game.get("season") or 0)
    away, home = str(game.get("away_team") or ""), str(game.get("home_team") or "")
    profiles = {team: {role: team_season_rushing(repository, team, season, role=role,
                                                 model_version=model_version)
                       for role in ("offense", "defense")} for team in (away, home)}
    panels = []
    for attacker, defender in ((away, home), (home, away)):
        zones = []
        for direction in ("left", "middle", "right"):
            offense = profiles[attacker]["offense"]["zones"][direction]
            defense = profiles[defender]["defense"]["zones"][direction]
            off_epa, def_epa = offense["epa_per_attempt"], defense["epa_per_attempt"]
            edge = (off_epa + def_epa) / 2 if off_epa is not None and def_epa is not None else None
            zones.append({"direction": direction, "offense": offense, "defense": defense, "edge": edge})
        panels.append({"attacker": attacker, "defender": defender, "zones": zones,
                       "attempts": profiles[attacker]["offense"]["attempts"],
                       "allowed_attempts": profiles[defender]["defense"]["attempts"]})
    return panels


def team_season_rushing_situational(repository: CFBRepository, team: str, season: int, *,
                                    role: str = "offense",
                                    model_version: str = MODEL_VERSION) -> dict[str, Any]:
    """Red-zone rushing -- no end-zone equivalent for a carry, the same
    asymmetry the NFL side's rushing situational split uses."""
    initialize(repository)
    column = "p.offense" if role == "offense" else "p.defense"
    with repository._reader() as connection:
        rows = connection.execute(
            f"""SELECT p.rushing_yards,p.is_touchdown,p.rusher,p.rusher_id,e.epa,pl.position
                FROM cfbd_rushing_plays p
                LEFT JOIN cfb_play_epa e ON e.play_id=p.play_id AND e.model_version=?
                LEFT JOIN cfb_play_metrics m ON m.play_id=p.play_id
                LEFT JOIN players pl ON pl.player_id=p.rusher_id AND pl.season=p.season
                WHERE p.season=? AND {column}=? AND p.start_yards_to_goal<=20
                  AND {_REAL_RUSH} AND COALESCE(m.garbage_time,0)=0""",
            (model_version, int(season), str(team))).fetchall()
    zone = {"attempts": 0, "yards": 0.0, "touchdowns": 0, "epa": 0.0, "epa_plays": 0,
           "rushers": {}, "positions": {}}
    for row in rows:
        zone["attempts"] += 1
        zone["yards"] += float(row["rushing_yards"] or 0)
        zone["touchdowns"] += int(bool(row["is_touchdown"]))
        if row["epa"] is not None:
            zone["epa"] += float(row["epa"]); zone["epa_plays"] += 1
        rusher = str(row["rusher"] or "").strip()
        position = str(row["position"] or "UNK").upper()
        if rusher:
            entry = zone["rushers"].setdefault(rusher, {
                "name": rusher, "player_id": row["rusher_id"], "position": position,
                "attempts": 0, "yards": 0, "touchdowns": 0})
            entry["attempts"] += 1
            entry["yards"] += round(float(row["rushing_yards"] or 0))
            entry["touchdowns"] += int(bool(row["is_touchdown"]))
            if role == "defense":
                zone["positions"][position] = zone["positions"].get(position, 0) + 1
    rushers = sorted(zone.pop("rushers").values(),
                     key=lambda item: (-item["attempts"], -item["yards"], item["name"]))
    positions = zone.pop("positions")
    allowed_by_position = sorted(
        ({"position": position, "attempts": count} for position, count in positions.items()),
        key=lambda item: (-item["attempts"], item["position"])) if role == "defense" else []
    return {**zone, "epa_per_attempt": zone["epa"] / zone["epa_plays"] if zone["epa_plays"] else None,
           "rushers": rushers[:8], "allowed_by_position": allowed_by_position}


def matchup_rushing_situational(repository: CFBRepository, game: dict[str, Any], *,
                                model_version: str = MODEL_VERSION) -> list[dict[str, Any]]:
    """Red-zone rushing for both offenses against the defense each will face."""
    season = int(game.get("season") or 0)
    away, home = str(game.get("away_team") or ""), str(game.get("home_team") or "")
    situational = {team: {role: team_season_rushing_situational(
        repository, team, season, role=role, model_version=model_version)
        for role in ("offense", "defense")} for team in (away, home)}
    panels = []
    for attacker, defender in ((away, home), (home, away)):
        offense = situational[attacker]["offense"]
        defense = situational[defender]["defense"]
        rows = []
        if offense["attempts"] or defense["attempts"]:
            off_epa, def_epa = offense["epa_per_attempt"], defense["epa_per_attempt"]
            edge = (off_epa + def_epa) / 2 if off_epa is not None and def_epa is not None else None
            rows.append({"key": "red_zone", "label": "Red zone", "offense": offense,
                        "defense": defense, "edge": edge})
        panels.append({"attacker": attacker, "defender": defender, "rows": rows})
    return panels


def coverage(repository: CFBRepository, season: int) -> dict[str, Any]:
    """How much of a season is classified, for callers that must disclose it."""
    initialize(repository)
    with repository._reader() as connection:
        row = connection.execute(
            """SELECT COUNT(*) AS attempts,
                      SUM(CASE WHEN rush_direction IS NOT NULL THEN 1 ELSE 0 END) AS classified,
                      COUNT(DISTINCT game_id) AS games,
                      MIN(week) AS first_week, MAX(week) AS last_week
               FROM cfbd_rushing_plays WHERE season=?""", (int(season),)).fetchone()
    attempts = row["attempts"] or 0
    classified = row["classified"] or 0
    return {"season": int(season), "attempts": attempts, "classified": classified,
            "games": row["games"] or 0, "first_week": row["first_week"],
            "last_week": row["last_week"],
            "coverage": round(classified / attempts, 4) if attempts else 0.0}
