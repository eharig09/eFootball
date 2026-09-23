"""Per-attempt passing detail from CFBD, and the middle/outside splits it enables.

CFBD publishes where a pass went -- left, middle or right, short or deep -- along
with air yards and yards after catch, one row per attempt. Before this the same
fields were recovered by parsing play text, which reached 1.4% of attempts; the
endpoint reaches 94-96% once a season has coverage.

Every row carries the provider's own play id, and those match `cfb_plays`
exactly, so our in-house ep-v2 EPA attaches to an attempt without any name or
clock matching. That join is the point of storing the attempts rather than the
provider's season aggregates: the aggregates carry air yards and yards after
catch but no direction split, and no EPA at all.

Coverage is uneven and the caller is expected to say so. 2025 has nothing before
week 8 and 90-96% from week 9; 2026 onward should be near-complete as games are
played. Every aggregate here reports how many attempts it is built from, because
a middle-of-the-field EPA over four throws is not a number anyone should read.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Iterable

from sports_aggregator.cfb.repository import CFBRepository, schema_once


#: The EPA model the report and the matchup page read. See `pbp_cli`.
MODEL_VERSION = "ep-v2"

#: Below this an attempt split is noise, not a tendency. A team throws to the
#: middle roughly a fifth of the time, so a game gives single figures and only a
#: season's worth of attempts supports a comparison.
MIN_GAME_ATTEMPTS = 4
MIN_SEASON_ATTEMPTS = 25

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfbd_passing_plays (
  play_id TEXT PRIMARY KEY,
  game_id INTEGER NOT NULL,
  season INTEGER NOT NULL,
  week INTEGER,
  offense TEXT NOT NULL,
  defense TEXT NOT NULL,
  passer TEXT,
  passer_id TEXT,
  target TEXT,
  target_id TEXT,
  down INTEGER,
  distance INTEGER,
  start_yards_to_goal INTEGER,
  pass_direction TEXT,
  pass_depth TEXT,
  pass_location TEXT,
  air_yards REAL,
  yards_after_catch REAL,
  total_yards REAL,
  outcome TEXT,
  parse_status TEXT,
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passing_plays_game ON cfbd_passing_plays(game_id);
CREATE INDEX IF NOT EXISTS idx_passing_plays_offense ON cfbd_passing_plays(season, offense);
CREATE INDEX IF NOT EXISTS idx_passing_plays_defense ON cfbd_passing_plays(season, defense);
CREATE INDEX IF NOT EXISTS idx_passing_plays_passer ON cfbd_passing_plays(season, passer_id);
"""


@schema_once("passing_plays")
def initialize(repository: CFBRepository) -> None:
    # The splits below join to cfb_play_epa and cfb_play_metrics, so the modules
    # that own those tables are initialized here too. Declaring the dependency
    # is what `team_game_advanced` does, and it keeps a first read on a fresh
    # database from failing on a table nobody has created yet.
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
        str(offense), str(defense), item.get("passer"), identifier(item.get("passerId")),
        item.get("target"), identifier(item.get("targetId")),
        item.get("down"), item.get("distance"), item.get("startYardsToGoal"),
        item.get("passDirection"), item.get("passDepth"), item.get("passLocation"),
        item.get("airYards"), item.get("yardsAfterCatch"), item.get("totalYards"),
        item.get("outcome"), item.get("parseStatus"), now,
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
            "INSERT OR REPLACE INTO cfbd_passing_plays VALUES (%s)" % ",".join("?" * 22), rows)
    classified = sum(1 for row in rows if row[13])
    return {"stored": len(rows), "classified": classified,
            "coverage": round(classified / len(rows), 4)}


def sync_week(repository: CFBRepository, client, *, season: int, week: int,
              force: bool = False) -> dict[str, Any]:
    raw = client.get("/passing/plays", {"year": int(season), "week": int(week)}, force=force)
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


#: One bucket of attempts: how many, what they were worth, and how they got there.
_BUCKET_SQL = """
  SELECT p.pass_direction AS direction,
         COUNT(*) AS attempts,
         SUM(CASE WHEN p.outcome='completion' THEN 1 ELSE 0 END) AS completions,
         AVG(e.epa) AS epa_per_attempt,
         SUM(e.epa) AS total_epa,
         AVG(p.air_yards) AS adot,
         AVG(p.yards_after_catch) AS yac,
         SUM(CASE WHEN p.air_yards IS NOT NULL THEN 1 ELSE 0 END) AS air_yards_available,
         SUM(CASE WHEN p.yards_after_catch IS NOT NULL THEN 1 ELSE 0 END) AS yac_available
  FROM cfbd_passing_plays p
  JOIN cfb_play_epa e ON e.play_id = p.play_id AND e.model_version = ?
  LEFT JOIN cfb_play_metrics m ON m.play_id = p.play_id
  WHERE p.pass_direction IS NOT NULL AND e.epa IS NOT NULL
    AND COALESCE(m.garbage_time, 0) = 0
    AND {scope}
  GROUP BY p.pass_direction
"""


def _buckets(connection, scope: str, params: tuple) -> dict[str, Any]:
    rows = connection.execute(_BUCKET_SQL.format(scope=scope), params).fetchall()
    middle = {"attempts": 0, "completions": 0, "total_epa": 0.0,
              "air_yards_available": 0, "yac_available": 0, "adot": None, "yac": None}
    outside = dict(middle)
    for row in rows:
        target = middle if row["direction"] == "middle" else outside
        weight = row["attempts"]
        target["attempts"] += weight
        target["completions"] += row["completions"] or 0
        target["total_epa"] += row["total_epa"] or 0.0
        target["air_yards_available"] += row["air_yards_available"] or 0
        target["yac_available"] += row["yac_available"] or 0
        for key, value in (("adot", row["adot"]), ("yac", row["yac"])):
            if value is None:
                continue
            # Weighted by attempts so left and right combine honestly.
            current = target[key]
            target[key] = value * weight if current is None else current + value * weight

    def finish(bucket: dict[str, Any]) -> dict[str, Any]:
        attempts = bucket["attempts"]
        if not attempts:
            return {"attempts": 0, "epa_per_attempt": None, "completion_rate": None,
                    "adot": None, "yac": None, "air_yards_available": 0, "yac_available": 0}
        return {
            "attempts": attempts,
            "epa_per_attempt": bucket["total_epa"] / attempts,
            "completion_rate": bucket["completions"] / attempts,
            "adot": (bucket["adot"] / attempts) if bucket["adot"] is not None else None,
            "yac": (bucket["yac"] / attempts) if bucket["yac"] is not None else None,
            "air_yards_available": bucket["air_yards_available"],
            "yac_available": bucket["yac_available"],
        }

    done_middle, done_outside = finish(middle), finish(outside)
    total = done_middle["attempts"] + done_outside["attempts"]
    return {"middle": done_middle, "outside": done_outside,
            "attempts": total,
            "middle_share": (done_middle["attempts"] / total) if total else None}


def game_splits(repository: CFBRepository, game_id: int, *,
                model_version: str = MODEL_VERSION) -> dict[str, dict[str, Any]]:
    """Middle/outside passing for both teams in one game, thrown and allowed."""
    initialize(repository)
    teams: dict[str, dict[str, Any]] = {}
    with repository._reader() as connection:
        sides = connection.execute(
            "SELECT DISTINCT offense, defense FROM cfbd_passing_plays WHERE game_id=?",
            (int(game_id),)).fetchall()
        for side in sides:
            for team, role in ((side["offense"], "offense"), (side["defense"], "defense")):
                entry = teams.setdefault(str(team), {})
                if role in entry:
                    continue
                column = "p.offense" if role == "offense" else "p.defense"
                entry[role] = _buckets(
                    connection, f"p.game_id = ? AND {column} = ?",
                    (model_version, int(game_id), str(team)))
    return teams


def team_season_splits(repository: CFBRepository, team: str, season: int, *,
                       model_version: str = MODEL_VERSION,
                       through_week: int | None = None) -> dict[str, Any]:
    """Season-to-date middle/outside passing for one team, thrown and allowed."""
    initialize(repository)
    bounds = "" if through_week is None else " AND p.week <= ?"
    extra: tuple = () if through_week is None else (int(through_week),)
    with repository._reader() as connection:
        offense = _buckets(
            connection, f"p.season = ? AND p.offense = ?{bounds}",
            (model_version, int(season), str(team), *extra))
        defense = _buckets(
            connection, f"p.season = ? AND p.defense = ?{bounds}",
            (model_version, int(season), str(team), *extra))
    return {"team": team, "season": int(season), "offense": offense, "defense": defense}


def _receiver_position_rows(grouped: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for position, values in grouped.items():
        epa_plays = values["epa_plays"]
        rows.append({
            "position": position, "players": len(values["players"]),
            "targets": values["targets"], "receptions": values["receptions"],
            "yards": values["yards"], "touchdowns": values["touchdowns"],
            "epa_per_attempt": values["epa"] / epa_plays if epa_plays else None,
        })
    return sorted(rows, key=lambda item: (-item["targets"], item["position"]))


def team_season_field(repository: CFBRepository, team: str, season: int, *,
                      role: str = "offense", model_version: str = MODEL_VERSION,
                      ) -> dict[str, Any]:
    """Depth-by-direction passing profile, including receiver output by zone.

    For role="defense", each zone also carries `allowed_by_position`: a
    season-wide, every-opponent tally of what offensive positions have beaten
    this defense in that zone -- the honest defensive summary, since the
    receiver list already shows each name's own position inline and does not
    need a second, redundant tally on the offense side.
    """
    initialize(repository)
    column = "p.offense" if role == "offense" else "p.defense"
    with repository._reader() as connection:
        rows = connection.execute(
            f"""SELECT p.pass_direction,p.air_yards,p.outcome,p.target,p.target_id,
                       p.total_yards,e.epa,COALESCE(c.scoring,0) scoring,c.play_text,
                       pl.position
                FROM cfbd_passing_plays p
                LEFT JOIN cfb_play_epa e ON e.play_id=p.play_id AND e.model_version=?
                LEFT JOIN cfb_play_metrics m ON m.play_id=p.play_id
                LEFT JOIN cfb_plays c ON c.play_id=p.play_id
                LEFT JOIN players pl ON pl.player_id=p.target_id AND pl.season=p.season
                WHERE p.season=? AND {column}=? AND p.pass_direction IS NOT NULL
                  AND p.air_yards IS NOT NULL AND COALESCE(m.garbage_time,0)=0""",
            (model_version, int(season), str(team))).fetchall()
    zones = {(depth, direction): {"attempts": 0, "completions": 0, "yards": 0.0,
                                  "epa": 0.0, "epa_plays": 0, "receivers": {}, "positions": {}}
             for depth, _low, _high in DEPTH_BANDS for direction in ("left", "middle", "right")}
    for row in rows:
        direction = str(row["pass_direction"] or "").casefold()
        if direction not in {"left", "middle", "right"}:
            continue
        depth = _band(float(row["air_yards"]))
        zone = zones[(depth, direction)]
        zone["attempts"] += 1
        complete = row["outcome"] == "completion"
        zone["completions"] += int(complete)
        zone["yards"] += float(row["total_yards"] or 0)
        if row["epa"] is not None:
            zone["epa"] += float(row["epa"]); zone["epa_plays"] += 1
        target = str(row["target"] or "").strip()
        position = str(row["position"] or "UNK").upper()
        if target:
            receiver = zone["receivers"].setdefault(target, {
                "name": target, "player_id": row["target_id"], "position": position,
                "receptions": 0, "yards": 0, "touchdowns": 0, "targets": 0})
            receiver["targets"] += 1
            if complete:
                receiver["receptions"] += 1
                receiver["yards"] += round(float(row["total_yards"] or 0))
                touchdown = bool(row["scoring"]) and "touchdown" in str(row["play_text"] or "").casefold()
                receiver["touchdowns"] += int(touchdown)
            if role == "defense":
                group = zone["positions"].setdefault(position, {
                    "players": set(), "targets": 0, "receptions": 0, "yards": 0,
                    "touchdowns": 0, "epa": 0.0, "epa_plays": 0,
                })
                group["players"].add(str(row["target_id"] or target))
                group["targets"] += 1
                group["receptions"] += int(complete)
                group["yards"] += round(float(row["total_yards"] or 0)) if complete else 0
                group["touchdowns"] += int(touchdown) if complete else 0
                if row["epa"] is not None:
                    group["epa"] += float(row["epa"]); group["epa_plays"] += 1
    output = {}
    for key, zone in zones.items():
        receivers = sorted(zone.pop("receivers").values(),
                           key=lambda item: (-item["receptions"], -item["yards"], item["name"]))
        positions = zone.pop("positions")
        allowed_by_position = (_receiver_position_rows(positions)
                               if role == "defense" else [])
        output[key] = {**zone,
                       "epa_per_attempt": zone["epa"] / zone["epa_plays"] if zone["epa_plays"] else None,
                       "receivers": receivers[:5], "allowed_by_position": allowed_by_position}
    return {"team": team, "season": int(season), "role": role, "zones": output,
            "attempts": len(rows)}


#: Red zone: the same yardline_100<=20 convention the NFL side uses. End zone:
#: the target itself reaches or crosses the goal line (start_yards_to_goal
#: minus air_yards <= 0) -- an incompletion or interception thrown into the
#: end zone counts, not just a score, matching the NFL side's exact
#: definition and reusing the same start_yards_to_goal/air_yards columns
#: already stored rather than needing a new one.
SITUATIONS = (
    ("red_zone", "Red zone", "p.start_yards_to_goal<=20"),
    ("end_zone", "End zone", "p.start_yards_to_goal-p.air_yards<=0"),
)


def team_season_situational(repository: CFBRepository, team: str, season: int, *,
                            role: str = "offense", model_version: str = MODEL_VERSION,
                            ) -> dict[str, dict[str, Any]]:
    """Red-zone and end-zone passing splits, same shape as one team_season_field
    zone (attempts/completions/yards/epa/receivers/allowed_by_position) so the
    same template partial can render both."""
    initialize(repository)
    column = "p.offense" if role == "offense" else "p.defense"
    situations = {}
    with repository._reader() as connection:
        for key, _label, condition in SITUATIONS:
            rows = connection.execute(
                f"""SELECT p.outcome,p.target,p.target_id,p.total_yards,e.epa,
                           COALESCE(c.scoring,0) scoring,c.play_text,pl.position
                    FROM cfbd_passing_plays p
                    LEFT JOIN cfb_play_epa e ON e.play_id=p.play_id AND e.model_version=?
                    LEFT JOIN cfb_play_metrics m ON m.play_id=p.play_id
                    LEFT JOIN cfb_plays c ON c.play_id=p.play_id
                    LEFT JOIN players pl ON pl.player_id=p.target_id AND pl.season=p.season
                    WHERE p.season=? AND {column}=? AND p.air_yards IS NOT NULL
                      AND p.start_yards_to_goal IS NOT NULL AND COALESCE(m.garbage_time,0)=0
                      AND {condition}""",
                (model_version, int(season), str(team))).fetchall()
            zone = {"attempts": 0, "completions": 0, "yards": 0.0, "epa": 0.0, "epa_plays": 0,
                   "receivers": {}, "positions": {}}
            for row in rows:
                zone["attempts"] += 1
                complete = row["outcome"] == "completion"
                zone["completions"] += int(complete)
                zone["yards"] += float(row["total_yards"] or 0)
                if row["epa"] is not None:
                    zone["epa"] += float(row["epa"]); zone["epa_plays"] += 1
                target = str(row["target"] or "").strip()
                position = str(row["position"] or "UNK").upper()
                if target:
                    receiver = zone["receivers"].setdefault(target, {
                        "name": target, "player_id": row["target_id"], "position": position,
                        "receptions": 0, "yards": 0, "touchdowns": 0, "targets": 0})
                    receiver["targets"] += 1
                    if complete:
                        receiver["receptions"] += 1
                        receiver["yards"] += round(float(row["total_yards"] or 0))
                        touchdown = (bool(row["scoring"])
                                    and "touchdown" in str(row["play_text"] or "").casefold())
                        receiver["touchdowns"] += int(touchdown)
                    if role == "defense":
                        group = zone["positions"].setdefault(position, {
                            "players": set(), "targets": 0, "receptions": 0,
                            "yards": 0, "touchdowns": 0, "epa": 0.0,
                            "epa_plays": 0,
                        })
                        group["players"].add(str(row["target_id"] or target))
                        group["targets"] += 1
                        group["receptions"] += int(complete)
                        group["yards"] += (round(float(row["total_yards"] or 0))
                                           if complete else 0)
                        group["touchdowns"] += int(touchdown) if complete else 0
                        if row["epa"] is not None:
                            group["epa"] += float(row["epa"]); group["epa_plays"] += 1
            receivers = sorted(zone.pop("receivers").values(),
                              key=lambda item: (-item["receptions"], -item["yards"], item["name"]))
            positions = zone.pop("positions")
            allowed_by_position = (_receiver_position_rows(positions)
                                   if role == "defense" else [])
            situations[key] = {**zone,
                               "epa_per_attempt": zone["epa"] / zone["epa_plays"] if zone["epa_plays"] else None,
                               "receivers": receivers[:8], "allowed_by_position": allowed_by_position}
    return situations


def passer_career_field(repository: CFBRepository, player_id: str, *,
                        model_version: str = MODEL_VERSION) -> dict[str, Any]:
    """Depth-by-direction passing profile across every season this passer threw.

    Same zone shape as `team_season_field`, scoped to one passer's `passer_id`
    with no season filter, so a career chart uses the exact same query pattern
    (and the exact same visual) as the team-vs-defense matchup field does.
    """
    initialize(repository)
    identifier = str(player_id)
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT p.season,p.pass_direction,p.air_yards,p.outcome,p.target,p.target_id,
                      p.total_yards,e.epa,COALESCE(c.scoring,0) scoring,c.play_text
               FROM cfbd_passing_plays p
               LEFT JOIN cfb_play_epa e ON e.play_id=p.play_id AND e.model_version=?
               LEFT JOIN cfb_play_metrics m ON m.play_id=p.play_id
               LEFT JOIN cfb_plays c ON c.play_id=p.play_id
               WHERE p.passer_id=? AND p.pass_direction IS NOT NULL
                 AND p.air_yards IS NOT NULL AND COALESCE(m.garbage_time,0)=0""",
            (model_version, identifier)).fetchall()
    zones = {(depth, direction): {"attempts": 0, "completions": 0, "yards": 0.0,
                                  "epa": 0.0, "epa_plays": 0, "receivers": {}}
             for depth, _low, _high in DEPTH_BANDS for direction in ("left", "middle", "right")}
    seasons: set[int] = set()
    for row in rows:
        direction = str(row["pass_direction"] or "").casefold()
        if direction not in {"left", "middle", "right"}:
            continue
        seasons.add(int(row["season"]))
        depth = _band(float(row["air_yards"]))
        zone = zones[(depth, direction)]
        zone["attempts"] += 1
        complete = row["outcome"] == "completion"
        zone["completions"] += int(complete)
        zone["yards"] += float(row["total_yards"] or 0)
        if row["epa"] is not None:
            zone["epa"] += float(row["epa"]); zone["epa_plays"] += 1
        target = str(row["target"] or "").strip()
        if target:
            receiver = zone["receivers"].setdefault(target, {
                "name": target, "player_id": row["target_id"], "receptions": 0,
                "yards": 0, "touchdowns": 0, "targets": 0})
            receiver["targets"] += 1
            if complete:
                receiver["receptions"] += 1
                receiver["yards"] += round(float(row["total_yards"] or 0))
                touchdown = bool(row["scoring"]) and "touchdown" in str(row["play_text"] or "").casefold()
                receiver["touchdowns"] += int(touchdown)
    output = {}
    for key, zone in zones.items():
        receivers = sorted(zone.pop("receivers").values(),
                           key=lambda item: (-item["receptions"], -item["yards"], item["name"]))
        output[key] = {**zone,
                       "epa_per_attempt": zone["epa"] / zone["epa_plays"] if zone["epa_plays"] else None,
                       "receivers": receivers[:5]}
    return {"player_id": identifier, "zones": output, "attempts": len(rows),
            "seasons": sorted(seasons)}


def matchup_field(repository: CFBRepository, game: dict[str, Any], *,
                  model_version: str = MODEL_VERSION) -> list[dict[str, Any]]:
    """Both passing offenses against the defense each will face."""
    season = int(game.get("season") or 0)
    away, home = str(game.get("away_team") or ""), str(game.get("home_team") or "")
    profiles = {team: {role: team_season_field(repository, team, season, role=role,
                                                model_version=model_version)
                       for role in ("offense", "defense")} for team in (away, home)}
    panels = []
    for attacker, defender in ((away, home), (home, away)):
        zones = []
        for depth, _low, _high in reversed(DEPTH_BANDS):
            for direction in ("left", "middle", "right"):
                offense = profiles[attacker]["offense"]["zones"][(depth, direction)]
                defense = profiles[defender]["defense"]["zones"][(depth, direction)]
                off_epa, def_epa = offense["epa_per_attempt"], defense["epa_per_attempt"]
                # def_epa is EPA/attempt earned by whoever THREW into this zone
                # against this defense -- the same "value to the offense" scale
                # as off_epa, not an inverted defensive-quality score. A high
                # def_epa means the defense allows a lot here, which favors the
                # attacker, so the two signals are blended, not subtracted.
                edge = (off_epa + def_epa) / 2 if off_epa is not None and def_epa is not None else None
                zones.append({"depth": depth, "direction": direction, "offense": offense,
                              "defense": defense, "edge": edge})
        panels.append({"attacker": attacker, "defender": defender, "zones": zones,
                       "attempts": profiles[attacker]["offense"]["attempts"],
                       "allowed_attempts": profiles[defender]["defense"]["attempts"]})
    return panels


def matchup_situational(repository: CFBRepository, game: dict[str, Any], *,
                        model_version: str = MODEL_VERSION) -> list[dict[str, Any]]:
    """Red-zone/end-zone passing splits for both offenses against the defense
    each will face -- the same "high-value opportunity" read as the depth/
    direction field, restricted to the situations that decide games."""
    season = int(game.get("season") or 0)
    away, home = str(game.get("away_team") or ""), str(game.get("home_team") or "")
    situational = {team: {role: team_season_situational(repository, team, season, role=role,
                                                         model_version=model_version)
                          for role in ("offense", "defense")} for team in (away, home)}
    panels = []
    for attacker, defender in ((away, home), (home, away)):
        rows = []
        for key, label, _condition in SITUATIONS:
            offense = situational[attacker]["offense"][key]
            defense = situational[defender]["defense"][key]
            if not offense["attempts"] and not defense["attempts"]:
                continue
            off_epa, def_epa = offense["epa_per_attempt"], defense["epa_per_attempt"]
            edge = (off_epa + def_epa) / 2 if off_epa is not None and def_epa is not None else None
            rows.append({"key": key, "label": label, "offense": offense, "defense": defense,
                        "edge": edge})
        panels.append({"attacker": attacker, "defender": defender, "rows": rows})
    return panels


def coverage(repository: CFBRepository, season: int) -> dict[str, Any]:
    """How much of a season is classified, for callers that must disclose it."""
    initialize(repository)
    with repository._reader() as connection:
        row = connection.execute(
            """SELECT COUNT(*) AS attempts,
                      SUM(CASE WHEN pass_direction IS NOT NULL THEN 1 ELSE 0 END) AS classified,
                      COUNT(DISTINCT game_id) AS games,
                      MIN(week) AS first_week, MAX(week) AS last_week
               FROM cfbd_passing_plays WHERE season=?""", (int(season),)).fetchone()
    attempts = row["attempts"] or 0
    classified = row["classified"] or 0
    return {"season": int(season), "attempts": attempts, "classified": classified,
            "games": row["games"] or 0, "first_week": row["first_week"],
            "last_week": row["last_week"],
            "coverage": round(classified / attempts, 4) if attempts else 0.0}


#: Air-yard bands, so a passer's depth profile reads the way a chart would.
#: Behind the line is its own thing rather than "short": a screen is a run that
#: starts with a throw, and averaging it into short passing hides both.
DEPTH_BANDS = (("behind_line", None, 0.0), ("short", 0.0, 10.0),
               ("intermediate", 10.0, 20.0), ("deep", 20.0, None))


def _band(air_yards: float) -> str:
    for name, low, high in DEPTH_BANDS:
        if (low is None or air_yards >= low) and (high is None or air_yards < high):
            return name
    return "deep"


def passer_profile(repository: CFBRepository, player_id: str, season: int, *,
                   model_version: str = MODEL_VERSION) -> dict[str, Any]:
    """One quarterback's season: where he throws, how far, and what it returns.

    Built from the stored attempts rather than CFBD's own passer aggregates,
    which carry air yards and yards after catch but neither the direction split
    nor EPA. The passer id is the provider's, so nothing here depends on
    matching a name out of play text.
    """
    initialize(repository)
    identifier = str(player_id)
    with repository._reader() as connection:
        splits = _buckets(
            connection, "p.season = ? AND p.passer_id = ?",
            (model_version, int(season), identifier))
        rows = connection.execute(
            """SELECT p.air_yards, p.yards_after_catch, p.outcome, p.pass_direction,
                      p.passer, p.offense, e.epa
               FROM cfbd_passing_plays p
               LEFT JOIN cfb_play_epa e
                 ON e.play_id = p.play_id AND e.model_version = ?
               LEFT JOIN cfb_play_metrics m ON m.play_id = p.play_id
               WHERE p.season = ? AND p.passer_id = ?
                 AND COALESCE(m.garbage_time, 0) = 0""",
            (model_version, int(season), identifier)).fetchall()

    depth = {name: 0 for name, _low, _high in DEPTH_BANDS}
    attempts = completions = air_available = yac_available = 0
    air_total = yac_total = 0.0
    epa_total = 0.0
    epa_available = 0
    name = team = None
    for row in rows:
        attempts += 1
        name = name or row["passer"]
        team = team or row["offense"]
        if row["outcome"] == "completion":
            completions += 1
        if row["air_yards"] is not None:
            air_available += 1
            air_total += float(row["air_yards"])
            depth[_band(float(row["air_yards"]))] += 1
        if row["yards_after_catch"] is not None:
            yac_available += 1
            yac_total += float(row["yards_after_catch"])
        if row["epa"] is not None:
            epa_available += 1
            epa_total += float(row["epa"])
    return {
        "player_id": identifier, "player": name, "team": team, "season": int(season),
        "attempts": attempts, "completions": completions,
        "completion_rate": (completions / attempts) if attempts else None,
        "air_yards": air_total if air_available else None,
        "adot": (air_total / air_available) if air_available else None,
        "yards_after_catch": yac_total if yac_available else None,
        "yac_per_completion": (yac_total / yac_available) if yac_available else None,
        "epa": epa_total if epa_available else None,
        "epa_per_attempt": (epa_total / epa_available) if epa_available else None,
        # Availability, not attempts: CFBD publishes air yards on a subset, and a
        # depth profile drawn from a third of the throws should say so.
        "air_yards_available": air_available, "yac_available": yac_available,
        "epa_available": epa_available,
        "depth": depth, "direction": splits,
    }


def passer_weekly_trend(repository: CFBRepository, player_id: str, season: int, *,
                        model_version: str = MODEL_VERSION) -> list[dict[str, Any]]:
    """One quarterback's season broken out by week, for a trend chart.

    Reuses `passer_profile`'s own filtering (garbage-time excluded, same
    model_version) but groups by week instead of collapsing the season into
    one line, so a week with two attempts is a real, if noisy, point on the
    chart rather than being averaged away.
    """
    initialize(repository)
    identifier = str(player_id)
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT p.week, p.game_id, p.defense, p.outcome, p.total_yards, e.epa
               FROM cfbd_passing_plays p
               LEFT JOIN cfb_play_epa e ON e.play_id = p.play_id AND e.model_version = ?
               LEFT JOIN cfb_play_metrics m ON m.play_id = p.play_id
               WHERE p.season = ? AND p.passer_id = ?
                 AND COALESCE(m.garbage_time, 0) = 0""",
            (model_version, int(season), identifier)).fetchall()
    weeks: dict[int, dict[str, Any]] = {}
    for row in rows:
        week = row["week"]
        if week is None:
            continue
        bucket = weeks.setdefault(int(week), {"attempts": 0, "completions": 0, "interceptions": 0,
                                              "yards": 0.0, "epa": 0.0, "epa_plays": 0,
                                              "game_id": row["game_id"], "opponent": row["defense"]})
        bucket["attempts"] += 1
        if row["outcome"] == "completion":
            bucket["completions"] += 1
        elif row["outcome"] == "interception":
            bucket["interceptions"] += 1
        bucket["yards"] += float(row["total_yards"] or 0)
        if row["epa"] is not None:
            bucket["epa"] += float(row["epa"]); bucket["epa_plays"] += 1
    output = []
    for week in sorted(weeks):
        bucket = weeks[week]
        attempts = bucket["attempts"]
        output.append({
            "week": week, "attempts": attempts,
            "completions": bucket["completions"], "interceptions": bucket["interceptions"],
            "yards": round(bucket["yards"], 0),
            "game_id": bucket["game_id"], "opponent": bucket["opponent"],
            "completion_rate": (bucket["completions"] / attempts) if attempts else None,
            "yards_per_attempt": (bucket["yards"] / attempts) if attempts else None,
            "epa_per_attempt": (bucket["epa"] / bucket["epa_plays"]) if bucket["epa_plays"] else None,
        })
    return output


def team_weekly_completions(repository: CFBRepository, team: str, season: int) -> list[dict[str, Any]]:
    """Team-wide weekly pass completions -- an honest denominator for a
    receiver's reception share, since the team doesn't publish that count
    anywhere else (cfb_team_game_pace's pass_plays is attempts, not
    completions).

    Same garbage-time exclusion passer_weekly_trend applies to one passer,
    applied here across every attempt credited to the team's offense -- every
    QB who threw for this team that week, not just whichever one has a
    player page open.
    """
    initialize(repository)
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT p.week, p.outcome
               FROM cfbd_passing_plays p
               LEFT JOIN cfb_play_metrics m ON m.play_id = p.play_id
               WHERE p.season = ? AND p.offense = ?
                 AND COALESCE(m.garbage_time, 0) = 0""",
            (int(season), str(team))).fetchall()
    weeks: dict[int, int] = {}
    for row in rows:
        week = row["week"]
        if week is None:
            continue
        week = int(week)
        weeks.setdefault(week, 0)
        if row["outcome"] == "completion":
            weeks[week] += 1
    return [{"week": week, "completions": count} for week, count in sorted(weeks.items())]


def season_passers(repository: CFBRepository, team: str, season: int,
                   *, minimum: int = 20) -> list[dict[str, Any]]:
    """Everyone who threw for a team, most attempts first."""
    initialize(repository)
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT passer_id, passer, COUNT(*) AS attempts
               FROM cfbd_passing_plays
               WHERE season = ? AND offense = ? AND passer_id IS NOT NULL
               GROUP BY passer_id, passer HAVING attempts >= ?
               ORDER BY attempts DESC""",
            (int(season), str(team), int(minimum))).fetchall()
    return [dict(row) for row in rows]
