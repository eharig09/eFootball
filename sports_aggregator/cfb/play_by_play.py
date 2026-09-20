"""Normalized play-by-play storage and versioned in-house football metrics.

Raw CFBD play context is retained so metric definitions can be changed and the
entire season can be reprocessed without refetching the provider. Provider PPA
is stored only as a benchmark; it is never presented as an in-house EPA value.

Version 1 intentionally derives metrics that are identifiable directly from a
play: success, explosiveness, down type, havoc, field position and drive
finishing inputs. Expected-points/EPA fields are reserved for a separately
fitted state-value model so we do not relabel CFBD PPA as our own work.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
import re
from typing import Any, Iterable

from sports_aggregator.cfb.cfbd import FINISHED_WEEK_TTL, LIVE_WEEK_TTL
from sports_aggregator.cfb.repository import schema_once

METRIC_VERSION = "pbp-v1"
HAVOC_WORDS = ("sack", "interception", "fumble", "tackle for loss")


@schema_once("play_by_play")
def initialize(repository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_plays (
          play_id TEXT PRIMARY KEY,
          game_id INTEGER NOT NULL,
          drive_id TEXT,
          drive_number INTEGER,
          play_number INTEGER,
          offense TEXT NOT NULL,
          defense TEXT NOT NULL,
          home_team TEXT,
          away_team TEXT,
          period INTEGER,
          clock_minutes INTEGER,
          clock_seconds INTEGER,
          offense_score INTEGER,
          defense_score INTEGER,
          yardline INTEGER,
          yards_to_goal INTEGER,
          down INTEGER,
          distance INTEGER,
          yards_gained INTEGER,
          scoring INTEGER NOT NULL DEFAULT 0,
          play_type TEXT,
          play_text TEXT,
          provider_ppa REAL,
          wallclock TEXT,
          season INTEGER NOT NULL,
          week INTEGER,
          raw_json TEXT NOT NULL,
          imported_at TEXT NOT NULL,
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_plays_game_order
          ON cfb_plays(game_id,drive_number,play_number);
        CREATE INDEX IF NOT EXISTS idx_cfb_plays_season_week
          ON cfb_plays(season,week,game_id);
        CREATE INDEX IF NOT EXISTS idx_cfb_plays_offense
          ON cfb_plays(season,offense);
        -- Every question asked of a team's offence gets asked of its defence,
        -- and without this the defence half fell back to the season prefix of
        -- the index above and tested `defense` on all 180,000 plays in the
        -- season: 475ms against 6.9ms, four times per matchup page.
        CREATE INDEX IF NOT EXISTS idx_cfb_plays_defense
          ON cfb_plays(season,defense);

        CREATE TABLE IF NOT EXISTS cfb_play_metrics (
          play_id TEXT NOT NULL,
          metric_version TEXT NOT NULL,
          rush_pass TEXT,
          down_type TEXT,
          success INTEGER,
          explosive INTEGER,
          havoc INTEGER,
          garbage_time INTEGER,
          scoring_opportunity INTEGER,
          line_yards REAL,
          ep_before REAL,
          ep_after REAL,
          own_epa REAL,
          derived_at TEXT NOT NULL,
          PRIMARY KEY(play_id,metric_version),
          FOREIGN KEY(play_id) REFERENCES cfb_plays(play_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_play_metrics_version
          ON cfb_play_metrics(metric_version,play_id);

        CREATE TABLE IF NOT EXISTS cfb_drive_metrics (
          game_id INTEGER NOT NULL,
          drive_id TEXT NOT NULL,
          metric_version TEXT NOT NULL,
          offense TEXT,
          defense TEXT,
          plays INTEGER,
          scrimmage_plays INTEGER,
          yards INTEGER,
          start_yards_to_goal INTEGER,
          end_yards_to_goal INTEGER,
          points INTEGER,
          scoring_opportunity INTEGER,
          success_rate REAL,
          explosive_plays INTEGER,
          havoc_allowed INTEGER,
          derived_at TEXT NOT NULL,
          PRIMARY KEY(game_id,drive_id,metric_version)
        );
        """)
        connection.commit()


def _int(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _clock(clock: Any) -> tuple[int | None, int | None]:
    if isinstance(clock, dict):
        return _int(clock.get("minutes")), _int(clock.get("seconds"))
    if clock:
        match = re.search(r"(\d+):(\d+)", str(clock))
        if match:
            return int(match.group(1)), int(match.group(2))
    return None, None


def _rush_pass(play_type: Any, play_text: Any) -> str | None:
    text = f"{play_type or ''} {play_text or ''}".casefold()
    if "sack" in text or "pass" in text:
        return "pass"
    if any(word in text for word in ("rush", "run", "rushing")):
        return "rush"
    return None


def _down_type(down: int | None, distance: int | None) -> str | None:
    if down is None or distance is None or down < 1 or down > 4:
        return None
    passing = (down == 2 and distance >= 8) or (down >= 3 and distance >= 5)
    return "passing" if passing else "standard"


def _success(down: int | None, distance: int | None, gained: int | None) -> int | None:
    if down is None or distance is None or gained is None or distance <= 0:
        return None
    threshold = {1: .50, 2: .70, 3: 1.00, 4: 1.00}.get(down)
    return None if threshold is None else int(gained >= distance * threshold)


def _line_yards(rush_pass: str | None, gained: int | None) -> float | None:
    if rush_pass != "rush" or gained is None:
        return None
    y = float(gained)
    if y < 0:
        return y * 1.20
    if y <= 4:
        return y
    if y <= 10:
        return 4 + (y - 4) * .50
    return 7.0


def _garbage(period: Any, offense_score: Any, defense_score: Any) -> int:
    period_i = _int(period) or 0
    margin = abs((_int(offense_score) or 0) - (_int(defense_score) or 0))
    return int((period_i >= 4 and margin >= 22) or (period_i == 3 and margin >= 29))


def normalize_play(raw: dict[str, Any], *, season: int, week: int,
                   retain_raw: bool = True) -> dict[str, Any]:
    minutes, seconds = _clock(raw.get("clock"))
    return {
        "play_id": str(raw.get("id") or "").strip(),
        "game_id": _int(raw.get("gameId")),
        "drive_id": str(raw.get("driveId") or "").strip() or None,
        "drive_number": _int(raw.get("driveNumber")),
        "play_number": _int(raw.get("playNumber")),
        "offense": str(raw.get("offense") or "").strip(),
        "defense": str(raw.get("defense") or "").strip(),
        "home_team": raw.get("home"), "away_team": raw.get("away"),
        "period": _int(raw.get("period")), "clock_minutes": minutes, "clock_seconds": seconds,
        "offense_score": _int(raw.get("offenseScore")), "defense_score": _int(raw.get("defenseScore")),
        "yardline": _int(raw.get("yardline")), "yards_to_goal": _int(raw.get("yardsToGoal")),
        "down": _int(raw.get("down")), "distance": _int(raw.get("distance")),
        "yards_gained": _int(raw.get("yardsGained")), "scoring": int(bool(raw.get("scoring"))),
        "play_type": raw.get("playType"), "play_text": raw.get("playText"),
        "provider_ppa": _float(raw.get("ppa")), "wallclock": raw.get("wallclock"),
        "season": int(season), "week": int(week),
        "raw_json": (
            json.dumps(raw, separators=(",", ":"), ensure_ascii=False)
            if retain_raw else "{}"
        ),
    }


def replace_week_plays(repository, raw_plays: Iterable[dict[str, Any]], *, season: int, week: int,
                       retain_raw: bool = True) -> int:
    initialize(repository)
    imported = datetime.now(timezone.utc).isoformat()
    rows = [
        normalize_play(row, season=season, week=week, retain_raw=retain_raw)
        for row in raw_plays
    ]
    rows = [row for row in rows if row["play_id"] and row["game_id"] and row["offense"] and row["defense"]]
    with closing(repository._connect()) as connection:
        connection.execute("DELETE FROM cfb_plays WHERE season=? AND week=?", (season, week))
        connection.executemany("""INSERT INTO cfb_plays(
          play_id,game_id,drive_id,drive_number,play_number,offense,defense,home_team,away_team,
          period,clock_minutes,clock_seconds,offense_score,defense_score,yardline,yards_to_goal,
          down,distance,yards_gained,scoring,play_type,play_text,provider_ppa,wallclock,season,week,
          raw_json,imported_at) VALUES(
          :play_id,:game_id,:drive_id,:drive_number,:play_number,:offense,:defense,:home_team,:away_team,
          :period,:clock_minutes,:clock_seconds,:offense_score,:defense_score,:yardline,:yards_to_goal,
          :down,:distance,:yards_gained,:scoring,:play_type,:play_text,:provider_ppa,:wallclock,:season,:week,
          :raw_json,:imported_at)""", [{**row, "imported_at": imported} for row in rows])
        connection.commit()
    derive_week(repository, season=season, week=week)
    return len(rows)


def derive_week(repository, *, season: int, week: int, metric_version: str = METRIC_VERSION) -> dict[str, int]:
    initialize(repository)
    now = datetime.now(timezone.utc).isoformat()
    with closing(repository._connect()) as connection:
        plays = [dict(row) for row in connection.execute(
            """SELECT * FROM cfb_plays WHERE season=? AND week=?
               ORDER BY game_id,drive_number,play_number""", (season, week)).fetchall()]
        play_ids = [row["play_id"] for row in plays]
        if play_ids:
            placeholders = ",".join("?" for _ in play_ids)
            connection.execute(
                f"DELETE FROM cfb_play_metrics WHERE metric_version=? AND play_id IN ({placeholders})",
                (metric_version, *play_ids))

        metric_rows = []
        for row in plays:
            rp = _rush_pass(row.get("play_type"), row.get("play_text"))
            text = f"{row.get('play_type') or ''} {row.get('play_text') or ''}".casefold()
            gained = _int(row.get("yards_gained")); ytg = _int(row.get("yards_to_goal"))
            metric_rows.append((
                row["play_id"], metric_version, rp,
                _down_type(_int(row.get("down")), _int(row.get("distance"))),
                _success(_int(row.get("down")), _int(row.get("distance")), gained) if rp else None,
                int((rp == "rush" and (gained or 0) >= 12) or (rp == "pass" and (gained or 0) >= 20)) if rp else None,
                int(any(word in text for word in HAVOC_WORDS)) if rp else None,
                _garbage(row.get("period"), row.get("offense_score"), row.get("defense_score")),
                int(ytg is not None and ytg <= 40), _line_yards(rp, gained),
                None, None, None, now,
            ))
        connection.executemany("INSERT INTO cfb_play_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", metric_rows)

        connection.execute("""DELETE FROM cfb_drive_metrics WHERE metric_version=?
          AND game_id IN (SELECT DISTINCT game_id FROM cfb_plays WHERE season=? AND week=?)""",
          (metric_version, season, week))
        grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        metrics_by_id = {row[0]: row for row in metric_rows}
        for row in plays:
            if row.get("drive_id"):
                grouped[(row["game_id"], row["drive_id"])].append(row)
        drive_rows = []
        for (game_id, drive_id), group in grouped.items():
            scrimmage = [(play, metrics_by_id[play["play_id"]]) for play in group
                         if play["play_id"] in metrics_by_id and metrics_by_id[play["play_id"]][2] in {"rush", "pass"}]
            successes = [m[4] for _, m in scrimmage if m[4] is not None]
            start, end = group[0], group[-1]
            points = 0
            for play in group:
                if play.get("scoring"):
                    text = str(play.get("play_type") or "").casefold()
                    if "field goal" in text:
                        points += 3
                    elif "two" in text:
                        points += 2
                    elif "touchdown" in text:
                        points += 6
            drive_rows.append((
                game_id, drive_id, metric_version, start.get("offense"), start.get("defense"),
                len(group), len(scrimmage), sum((_int(p.get("yards_gained")) or 0) for p in group),
                start.get("yards_to_goal"), end.get("yards_to_goal"), points,
                int(any(p.get("yards_to_goal") is not None and p["yards_to_goal"] <= 40 for p in group)),
                (sum(successes) / len(successes)) if successes else None,
                sum(int(m[5] or 0) for _, m in scrimmage), sum(int(m[6] or 0) for _, m in scrimmage), now,
            ))
        connection.executemany("INSERT INTO cfb_drive_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", drive_rows)
        connection.commit()
    return {"plays": len(metric_rows), "drives": len(drive_rows)}


def sync_recent_plays(repository, client, *, season: int, recent_weeks: int = 2,
                      force: bool = False) -> dict[str, Any]:
    """Fetch only completed/current weeks, keeping the Render workload bounded."""
    initialize(repository)
    with closing(repository._connect()) as connection:
        week_rows = connection.execute(
            """SELECT DISTINCT week FROM games WHERE season=? AND completed=1 AND week IS NOT NULL
               ORDER BY week DESC LIMIT ?""", (season, max(1, int(recent_weeks)))).fetchall()
        weeks = [int(row[0]) for row in week_rows]
    result = {"season": season, "weeks": [], "plays": 0}
    if not weeks:
        return result
    newest = max(weeks)
    for week in sorted(weeks):
        ttl = LIVE_WEEK_TTL if week == newest else FINISHED_WEEK_TTL
        raw = client.get(
            "/plays",
            {"year": season, "week": int(week), "seasonType": "both", "classification": "fbs"},
            cache_ttl_seconds=ttl, force=force,
        )
        count = replace_week_plays(repository, raw, season=season, week=int(week))
        result["weeks"].append(int(week)); result["plays"] += count
    return result


def game_advanced_summary(repository, game_id: int, *, metric_version: str = METRIC_VERSION) -> dict[str, Any]:
    initialize(repository)
    with closing(repository._connect()) as connection:
        rows = [dict(row) for row in connection.execute("""
          SELECT p.offense,p.defense,m.rush_pass,m.down_type,m.success,m.explosive,m.havoc,
                 m.garbage_time,m.scoring_opportunity,m.line_yards,p.yards_gained,p.yards_to_goal,
                 p.down,p.distance,p.provider_ppa
          FROM cfb_plays p JOIN cfb_play_metrics m USING(play_id)
          WHERE p.game_id=? AND m.metric_version=?""", (game_id, metric_version)).fetchall()]
        drives = [dict(row) for row in connection.execute(
            "SELECT * FROM cfb_drive_metrics WHERE game_id=? AND metric_version=? ORDER BY drive_id",
            (game_id, metric_version)).fetchall()]
    by_team: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_team[row["offense"]].append(row)
    teams = {}
    for team, items in by_team.items():
        competitive = [r for r in items if not r.get("garbage_time") and r.get("rush_pass")]
        success = [r["success"] for r in competitive if r.get("success") is not None]
        pass_plays = [r for r in competitive if r.get("rush_pass") == "pass"]
        rush_plays = [r for r in competitive if r.get("rush_pass") == "rush"]
        line_rows = [r for r in rush_plays if r.get("line_yards") is not None]
        ppa_rows = [r for r in competitive if r.get("provider_ppa") is not None]
        teams[team] = {
            "plays": len(competitive),
            "success_rate": sum(success)/len(success) if success else None,
            "explosive_rate": sum(int(r.get("explosive") or 0) for r in competitive)/len(competitive) if competitive else None,
            "pass_success_rate": sum(int(r.get("success") or 0) for r in pass_plays)/len(pass_plays) if pass_plays else None,
            "rush_success_rate": sum(int(r.get("success") or 0) for r in rush_plays)/len(rush_plays) if rush_plays else None,
            "havoc_allowed_rate": sum(int(r.get("havoc") or 0) for r in competitive)/len(competitive) if competitive else None,
            "line_yards_per_rush": sum(float(r["line_yards"]) for r in line_rows)/len(line_rows) if line_rows else None,
            "provider_ppa_per_play": sum(float(r["provider_ppa"]) for r in ppa_rows)/len(ppa_rows) if ppa_rows else None,
        }
    return {"game_id": game_id, "metric_version": metric_version, "teams": teams,
            "drives": drives, "own_epa_status": "reserved_for_fitted_expected_points_model"}
