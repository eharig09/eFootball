"""Betting lines as market opinion, stored one row per provider.

A line is the most liquid public forecast of a game, which makes it a useful
check on our own models -- and a genuinely different kind of information from a
report or a grade. It is stored the way it arrives: one row per provider, with
opening and current numbers kept apart so line movement stays visible. Nothing
is ever collapsed on the way in, so the providers can always be read back.

Where a view has room for one number and not five -- a scoreboard card, a slate
table -- the providers are averaged, and the disagreement travels with the
average rather than being dropped: `spread_range` and `total_range` say how far
apart the books are, and the count says how many were averaged. When DraftKings
and Bovada disagree that gap is information, and a reader who wants the books
themselves gets them from `game_lines`.

Nothing here is presented as a prediction by this application, and no betting
advice is derived from it.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Iterable

from sports_aggregator.cfb.repository import CFBRepository, _numeric, schema_once


LINES_SCHEMA = """
CREATE TABLE IF NOT EXISTS game_lines (
 game_id INTEGER NOT NULL, season INTEGER NOT NULL, provider TEXT NOT NULL,
 spread REAL, spread_open REAL, over_under REAL, over_under_open REAL,
 home_moneyline INTEGER, away_moneyline INTEGER,
 formatted_spread TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL,
 PRIMARY KEY(game_id,provider)
);
CREATE INDEX IF NOT EXISTS idx_game_lines_game ON game_lines(game_id);
-- Consensus (provider-average) spread/total, appended only when it moves, so a game's
-- line can be drawn over time. game_lines itself keeps just the opener and the latest.
CREATE TABLE IF NOT EXISTS game_line_history (
 game_id INTEGER NOT NULL, captured_at TEXT NOT NULL,
 spread REAL, over_under REAL, books INTEGER NOT NULL,
 PRIMARY KEY(game_id,captured_at)
);
"""


@schema_once("lines")
def initialize(repository: CFBRepository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript(LINES_SCHEMA)


def store_lines(repository: CFBRepository, season: int, payload: Iterable[dict[str, Any]]) -> int:
    """Persist every provider quote attached to a game."""
    initialize(repository)
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for game in payload:
        game_id = game.get("id")
        if game_id is None:
            continue
        for line in game.get("lines") or []:
            provider = str(line.get("provider") or "").strip()
            if not provider:
                continue
            rows.append((
                int(game_id), season, provider,
                _numeric(line.get("spread")), _numeric(line.get("spreadOpen")),
                _numeric(line.get("overUnder")), _numeric(line.get("overUnderOpen")),
                line.get("homeMoneyline"), line.get("awayMoneyline"),
                str(line.get("formattedSpread") or ""), now,
            ))
    with closing(repository._connect()) as connection:
        connection.executemany(
            """INSERT INTO game_lines VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(game_id,provider) DO UPDATE SET
               spread=excluded.spread,spread_open=excluded.spread_open,
               over_under=excluded.over_under,over_under_open=excluded.over_under_open,
               home_moneyline=excluded.home_moneyline,away_moneyline=excluded.away_moneyline,
               formatted_spread=excluded.formatted_spread,fetched_at=excluded.fetched_at""",
            rows)
        _record_line_history(connection, {row[0] for row in rows}, now)
        connection.commit()
    return len(rows)


def _record_line_history(connection, game_ids: set[int], captured_at: str) -> None:
    """Append the consensus line for each game whose spread or total moved since the last reading."""
    ids = sorted(game_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        current = connection.execute(
            f"""SELECT game_id,AVG(spread),AVG(over_under),COUNT(*) FROM game_lines
                WHERE game_id IN ({marks}) AND (spread IS NOT NULL OR over_under IS NOT NULL)
                GROUP BY game_id""", chunk).fetchall()
        latest = {row[0]: (row[1], row[2]) for row in connection.execute(
            f"""SELECT h.game_id,h.spread,h.over_under FROM game_line_history h
                JOIN (SELECT game_id,MAX(captured_at) m FROM game_line_history
                      WHERE game_id IN ({marks}) GROUP BY game_id) last
                  ON last.game_id=h.game_id AND last.m=h.captured_at""", chunk)}
        fresh = []
        for game_id, spread, total, books in current:
            spread = None if spread is None else round(spread, 2)
            total = None if total is None else round(total, 2)
            if latest.get(game_id) != (spread, total):
                fresh.append((game_id, captured_at, spread, total, books))
        connection.executemany("INSERT OR IGNORE INTO game_line_history VALUES(?,?,?,?,?)", fresh)


def line_history(repository: CFBRepository, game_id: int) -> list[dict[str, Any]]:
    """Consensus readings for one game, oldest first."""
    initialize(repository)
    with repository._reader() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT captured_at,spread,over_under,books FROM game_line_history WHERE game_id=? "
            "ORDER BY captured_at", (game_id,))]


def _moneyline_implied(odds: int | None) -> float | None:
    """American odds to a raw implied probability -- still carries the vig."""
    if odds is None:
        return None
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def _devigged_home_prob(home_odds: int | None, away_odds: int | None) -> float | None:
    """Normalize both sides' implied probabilities to sum to 100%.

    Removes the bookmaker's margin without needing to know its shape: summing
    the raw implied probabilities of both sides of a two-way market and
    dividing by that sum is the standard way to back the vig out.
    """
    home, away = _moneyline_implied(home_odds), _moneyline_implied(away_odds)
    if home is None or away is None or home + away <= 0:
        return None
    return home / (home + away)


def game_lines(repository: CFBRepository, game_id: int) -> dict[str, Any]:
    """Every provider quote for one game, with movement and disagreement."""
    initialize(repository)
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM game_lines WHERE game_id=? ORDER BY provider", (game_id,))]
    for row in rows:
        if row["spread"] is not None and row["spread_open"] is not None:
            row["spread_move"] = round(row["spread"] - row["spread_open"], 1)
        else:
            row["spread_move"] = None
        if row["over_under"] is not None and row["over_under_open"] is not None:
            row["total_move"] = round(row["over_under"] - row["over_under_open"], 1)
        else:
            row["total_move"] = None
        row["home_win_prob"] = _devigged_home_prob(row["home_moneyline"], row["away_moneyline"])
    spreads = [row["spread"] for row in rows if row["spread"] is not None]
    totals = [row["over_under"] for row in rows if row["over_under"] is not None]
    spreads_open = [row["spread_open"] for row in rows if row["spread_open"] is not None]
    totals_open = [row["over_under_open"] for row in rows if row["over_under_open"] is not None]
    home_probs = [row["home_win_prob"] for row in rows if row["home_win_prob"] is not None]
    return {
        "providers": rows,
        "count": len(rows),
        # Disagreement between books is information, so it is reported rather
        # than collapsed into a single consensus number.
        "spread_range": (round(max(spreads) - min(spreads), 1) if len(spreads) > 1 else 0.0),
        "total_range": (round(max(totals) - min(totals), 1) if len(totals) > 1 else 0.0),
        "consensus_spread": (round(sum(spreads) / len(spreads), 1) if spreads else None),
        "consensus_total": (round(sum(totals) / len(totals), 1) if totals else None),
        "consensus_spread_open": (round(sum(spreads_open) / len(spreads_open), 1) if spreads_open else None),
        "consensus_total_open": (round(sum(totals_open) / len(totals_open), 1) if totals_open else None),
        "consensus_home_win_prob": (round(100 * sum(home_probs) / len(home_probs), 1) if home_probs else None),
    }


def lines_by_game(repository: CFBRepository, season: int) -> dict[int, dict[str, Any]]:
    """Consensus spread and total per game, for slate-level views."""
    initialize(repository)
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT game_id,AVG(spread) spread,AVG(over_under) total,COUNT(*) books
               FROM game_lines WHERE season=? GROUP BY game_id""", (season,)).fetchall()
    return {
        row["game_id"]: {
            "spread": round(row["spread"], 1) if row["spread"] is not None else None,
            "total": round(row["total"], 1) if row["total"] is not None else None,
            "books": row["books"],
        }
        for row in rows
    }
