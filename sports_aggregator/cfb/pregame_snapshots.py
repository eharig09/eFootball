"""Immutable staged snapshots of what the app knew before kickoff.

A postgame comparison is only honest if it reads values captured before the
result existed. Snapshots are therefore append-only by (game, stage): T-24H,
T-3H and FINAL. Re-running a refresh never rewrites an already captured stage.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from typing import Any

from sports_aggregator.cfb.lines import game_lines
from sports_aggregator.cfb.matchups import game_matchup_report
from sports_aggregator.cfb.player_matchups import player_matchups
from sports_aggregator.cfb.external import fpi_for_game, weather_for_game
from sports_aggregator.cfb.repository import schema_once

LOGGER = logging.getLogger(__name__)

SNAPSHOT_VERSION = "pregame-v1"


@schema_once("pregame_snapshots")
def initialize(repository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_pregame_snapshots (
          game_id INTEGER NOT NULL,
          stage TEXT NOT NULL,
          snapshot_version TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          kickoff_at TEXT NOT NULL,
          hours_to_kick REAL NOT NULL,
          payload_json TEXT NOT NULL,
          PRIMARY KEY(game_id,stage,snapshot_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_pregame_snapshot_kickoff
          ON cfb_pregame_snapshots(kickoff_at,stage);
        """)
        connection.commit()


def _utc(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stage(hours_to_kick: float, existing: set[str]) -> str | None:
    if hours_to_kick < 0:
        return None
    # Production intentionally refreshes on a low-frequency cadence to protect
    # the 512 MB web host. FINAL therefore means the latest scheduled pregame
    # state, usually inside ~2 hours, and the exact observed offset is stored.
    if hours_to_kick <= 2.25 and "FINAL" not in existing:
        return "FINAL"
    if hours_to_kick <= 3.0 and "T-3H" not in existing:
        return "T-3H"
    if hours_to_kick <= 24.0 and "T-24H" not in existing:
        return "T-24H"
    return None


def _safe(call, default):
    try:
        return call()
    except Exception:
        return default


def build_snapshot(repository, game: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, JSON-safe packet using only already stored data."""
    game_id = int(game["game_id"]); season = int(game["season"])
    home_id = int(game["home_team_id"]); away_id = int(game["away_team_id"])
    pff_season = _safe(lambda: repository.latest_pff_season(), season - 1)
    elo = _safe(lambda: repository.team_elo(season), {})
    market = _safe(lambda: game_lines(repository, game_id), {})
    fpi = _safe(lambda: fpi_for_game(repository, game_id), None)
    weather = _safe(lambda: weather_for_game(repository, game_id), None)
    pff_rows = _safe(lambda: repository.pff_matchups(home_id, away_id, pff_season), [])
    unit_report = _safe(
        lambda: game_matchup_report(pff_rows, game["away_team"], game["home_team"]), {})
    player_watches = _safe(lambda: player_matchups(repository, home_id, away_id, limit=10), [])
    home_metrics = _safe(lambda: repository.team_metrics(game["home_team"], season), {})
    away_metrics = _safe(lambda: repository.team_metrics(game["away_team"], season), {})
    home_quality = _safe(lambda: repository.team_quality_snapshot(home_id, season), {})
    away_quality = _safe(lambda: repository.team_quality_snapshot(away_id, season), {})
    home_depth = _safe(lambda: repository.team_depth_chart(home_id, season), {})
    away_depth = _safe(lambda: repository.team_depth_chart(away_id, season), {})

    def compact_watch(row: dict[str, Any]) -> dict[str, Any]:
        attacker = row.get("attacker") or {}; defender = row.get("defender") or {}
        return {
            "label": row.get("label"), "interest": row.get("interest"), "why": row.get("why"),
            "attacker": {"name": attacker.get("player_name"), "player_id": attacker.get("cfbd_player_id"),
                         "team": attacker.get("team"), "grade": attacker.get("grade")},
            "defender": {"name": defender.get("player_name"), "player_id": defender.get("cfbd_player_id"),
                         "team": defender.get("team"), "grade": defender.get("grade"),
                         "is_unit": defender.get("is_unit")},
        }

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "game": {
            "game_id": game_id, "season": season, "week": game.get("week"),
            "home_team": game.get("home_team"), "away_team": game.get("away_team"),
            "home_team_id": home_id, "away_team_id": away_id,
            "start_date": game.get("start_date"), "neutral_site": game.get("neutral_site"),
            "venue": game.get("venue"),
        },
        "market": market,
        "fpi": fpi,
        "weather": weather,
        "elo": {"home": elo.get(home_id), "away": elo.get(away_id)},
        "team_context": {
            "home_metrics": home_metrics, "away_metrics": away_metrics,
            "home_quality": home_quality, "away_quality": away_quality,
        },
        "depth_chart": {"home": home_depth, "away": away_depth},
        "unit_matchups": (unit_report or {}).get("matchups") or [],
        "player_watches": [compact_watch(row) for row in player_watches],
    }


def capture_due(repository, *, season: int, now: datetime | None = None) -> dict[str, Any]:
    initialize(repository)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    with closing(repository._connect()) as connection:
        games = [dict(row) for row in connection.execute(
            """SELECT * FROM games WHERE season=? AND completed=0 AND start_date IS NOT NULL
               ORDER BY start_date LIMIT 80""", (season,)).fetchall()]
        existing_rows = connection.execute(
            "SELECT game_id,stage FROM cfb_pregame_snapshots WHERE snapshot_version=?",
            (SNAPSHOT_VERSION,)).fetchall()
    existing: dict[int, set[str]] = {}
    for row in existing_rows:
        existing.setdefault(int(row[0]), set()).add(str(row[1]))

    captured: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for game in games:
        try:
            kickoff = _utc(game["start_date"])
        except (TypeError, ValueError):
            continue
        hours = (kickoff - current).total_seconds() / 3600
        if hours > 24 or hours < 0:
            continue
        stage = _stage(hours, existing.get(int(game["game_id"]), set()))
        if not stage:
            continue
        # One game's missing evidence must not sink the batch: this runs
        # unattended twice a day, and a stage skipped now cannot be recaptured
        # once kickoff passes.
        try:
            payload = build_snapshot(repository, game)
            with closing(repository._connect()) as connection:
                connection.execute("""INSERT OR IGNORE INTO cfb_pregame_snapshots
                  (game_id,stage,snapshot_version,captured_at,kickoff_at,hours_to_kick,payload_json)
                  VALUES(?,?,?,?,?,?,?)""", (
                    int(game["game_id"]), stage, SNAPSHOT_VERSION, current.isoformat(), kickoff.isoformat(),
                    round(hours, 3), json.dumps(payload, separators=(",", ":"), default=str),
                ))
                connection.commit()
        except Exception as exc:  # noqa: BLE001 -- a bad game is logged, not fatal
            LOGGER.exception("Pregame snapshot failed game=%s stage=%s", game.get("game_id"), stage)
            failures.append({"game_id": int(game["game_id"]), "stage": stage, "error": str(exc)})
            continue
        existing.setdefault(int(game["game_id"]), set()).add(stage)
        captured.append({"game_id": int(game["game_id"]), "stage": stage, "hours_to_kick": round(hours, 2)})
    return {"season": season, "captured": captured, "count": len(captured), "failures": failures}


def snapshots_for_game(repository, game_id: int) -> list[dict[str, Any]]:
    initialize(repository)
    order = {"T-24H": 1, "T-3H": 2, "FINAL": 3}
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT * FROM cfb_pregame_snapshots WHERE game_id=? AND snapshot_version=?""",
            (int(game_id), SNAPSHOT_VERSION)).fetchall()]
    for row in rows:
        try:
            row["payload"] = json.loads(row.pop("payload_json"))
        except (TypeError, json.JSONDecodeError):
            row["payload"] = {}
    rows.sort(key=lambda row: order.get(str(row.get("stage")), 99))
    return rows


def final_snapshot(repository, game_id: int) -> dict[str, Any] | None:
    rows = snapshots_for_game(repository, game_id)
    return next((row for row in reversed(rows) if row.get("stage") == "FINAL"), rows[-1] if rows else None)
