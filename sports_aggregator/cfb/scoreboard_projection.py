"""The engine's projected score for each game, stored so the scoreboard can show it.

The number is the one the matchup page shows (`matchup_research.engine_display_score`: a calibrated
total and a calibrated margin, combined into per-side points). Computing it takes a few tenths of a
second per game, so a 60-game Saturday cannot be computed while a visitor waits; the refresh
computes it and the scoreboard reads it. Reads never create or write the table, because a read that
writes invalidates the rendered-page cache.

Upcoming games are recomputed at most every `RECOMPUTE_HOURS` (what they depend on only moves when
a game finishes or a line moves). Once a game has kicked off its projection is frozen: the stored
number is then the genuine pre-game forecast, shown beside the result, not a hindsight rewrite.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from dotenv import load_dotenv

LOGGER = logging.getLogger(__name__)

RECOMPUTE_HOURS = 12.0
HORIZON_DAYS = 9          # upcoming games projected this far ahead
LOOKBACK_DAYS = 10        # finished games with no stored projection get one this far back
GAME_LIMIT = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfb_scoreboard_projections (
    game_id INTEGER PRIMARY KEY,
    season INTEGER NOT NULL,
    away_points REAL NOT NULL,
    home_points REAL NOT NULL,
    total REAL NOT NULL,
    margin REAL NOT NULL,
    computed_at TEXT NOT NULL,
    frozen INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
);
"""

Compute = Callable[[Any, dict[str, Any]], "dict[str, float] | None"]


def initialize(repository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


def compute_projection(repository, game: dict[str, Any]) -> dict[str, float] | None:
    """The matchup page's displayed score for `game`, as of its kickoff."""
    from sports_aggregator.cfb.game_projection import project_matchup
    from sports_aggregator.cfb.matchup_research import engine_display_score

    projection = project_matchup(
        repository, game["home_team"], game["away_team"],
        as_of_date=game.get("start_date") or datetime.now(timezone.utc).isoformat(),
        game_id=game.get("game_id"))
    score = engine_display_score(repository, game, projection)
    if score is None:
        return None
    return {key: float(score[key]) for key in ("away", "home", "total", "margin")}


def _kickoff(game: dict[str, Any]) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(game["start_date"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def refresh(repository, *, season: int, now: datetime | None = None,
            horizon_days: int = HORIZON_DAYS, lookback_days: int = LOOKBACK_DAYS,
            recompute_hours: float = RECOMPUTE_HOURS, compute: Compute = compute_projection
            ) -> dict[str, Any]:
    initialize(repository)
    current = now or datetime.now(timezone.utc)
    start = (current - timedelta(days=lookback_days)).isoformat()
    end = (current + timedelta(days=horizon_days)).isoformat()
    with closing(repository._connect()) as connection:
        connection.row_factory = sqlite3.Row
        games = [dict(row) for row in connection.execute(
            """SELECT * FROM games WHERE season=? AND start_date IS NOT NULL
                 AND start_date>=? AND start_date<=? ORDER BY start_date LIMIT ?""",
            (season, start.replace("+00:00", "Z"), end.replace("+00:00", "Z"), GAME_LIMIT))]
        stored = {int(r["game_id"]): dict(r) for r in connection.execute(
            "SELECT game_id,computed_at,frozen FROM cfb_scoreboard_projections WHERE season=?", (season,))}
    report = {"season": season, "considered": len(games), "computed": 0, "frozen": 0,
              "fresh": 0, "unavailable": 0, "failures": []}
    stale_before = current - timedelta(hours=recompute_hours)
    for game in games:
        game_id = int(game["game_id"])
        kickoff = _kickoff(game)
        row = stored.get(game_id)
        started = bool(game.get("completed")) or (kickoff is not None and kickoff <= current)
        if row and row["frozen"]:
            continue
        if row and started:
            _set_frozen(repository, game_id)           # the last pre-game number becomes the record
            report["frozen"] += 1
            continue
        if row and not started:
            computed = datetime.fromisoformat(str(row["computed_at"]))
            if computed.tzinfo is None:
                computed = computed.replace(tzinfo=timezone.utc)
            if computed > stale_before:
                report["fresh"] += 1
                continue
        try:
            result = compute(repository, game)
        except Exception as exc:  # one game lacking data must not sink the rest
            LOGGER.exception("Scoreboard projection failed game=%s", game_id)
            report["failures"].append({"game_id": game_id, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        if result is None:
            report["unavailable"] += 1
            continue
        with closing(repository._connect()) as connection:
            connection.execute(
                """INSERT OR REPLACE INTO cfb_scoreboard_projections
                   (game_id,season,away_points,home_points,total,margin,computed_at,frozen)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (game_id, season, result["away"], result["home"], result["total"], result["margin"],
                 current.isoformat(), 1 if started else 0))
            connection.commit()
        report["computed"] += 1
    return report


def _set_frozen(repository, game_id: int) -> None:
    with closing(repository._connect()) as connection:
        connection.execute("UPDATE cfb_scoreboard_projections SET frozen=1 WHERE game_id=?", (game_id,))
        connection.commit()


def projections_for_games(repository, game_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Stored projections by game id. Read-only: a database that has never run the refresh simply
    has none, and this must not create the table."""
    if not game_ids:
        return {}
    marks = ",".join("?" for _ in game_ids)
    try:
        with repository._reader() as connection:
            rows = connection.execute(
                f"""SELECT game_id,away_points,home_points,total,margin,frozen,computed_at
                    FROM cfb_scoreboard_projections WHERE game_id IN ({marks})""",
                [int(g) for g in game_ids]).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(r["game_id"]): {"away": r["away_points"], "home": r["home_points"],
                                "total": r["total"], "margin": r["margin"],
                                "frozen": bool(r["frozen"]), "computed_at": r["computed_at"]}
            for r in rows}


def main(argv: list[str] | None = None) -> int:
    from sports_aggregator.cfb.repository import CFBRepository
    load_dotenv()
    parser = argparse.ArgumentParser(description="Store the engine's projected score for upcoming games")
    parser.add_argument("--season", type=int, default=datetime.now().year)
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)
    repository = CFBRepository(args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    report = refresh(repository, season=args.season)
    print(json.dumps(report, sort_keys=True))
    return 1 if report["failures"] and not report["computed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
