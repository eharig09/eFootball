"""The best available number across the tracked sportsbooks, for a game at a moment in time.

Picks are judged against the best line a bettor could actually have taken: for a pick on a side, the most
favorable spread among the tracked books, and for an over or under the most favorable total. Ties on the number
go to the better price. Both leagues use this module; each supplies its own fallback rows for books the odds
tracker does not cover (CFBD's DraftKings and ESPN BET quotes for college, ESPN's DraftKings feed for the NFL).

Everything here is shaped for a pick packet that gets frozen: plain dicts, JSON-safe, spreads already oriented to
the side they belong to (a team's spread is what that team gets: +5.5 means it can lose by five and cover).

The books are the ones the user chose to track: DraftKings, ESPN BET, FanDuel, BetMGM and BetRivers.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

BOOK_NAMES = {
    "draftkings": "DraftKings", "espnbet": "ESPN BET", "fanduel": "FanDuel",
    "betmgm": "BetMGM", "betrivers": "BetRivers",
}
TRACKED_BOOKS = frozenset(BOOK_NAMES)
#: CFBD labels the same book several ways across seasons
CFBD_BOOKS = {"draftkings": "draftkings", "draft kings": "draftkings", "espn bet": "espnbet"}

_NO_PRICE = float("-inf")


def book_name(key: str) -> str:
    return BOOK_NAMES.get(key, key)


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _price_key(price: Any) -> float:
    """American odds: higher is better for the bettor (-105 beats -115, +100 beats -110). Unknown ranks last."""
    number = _number(price)
    return _NO_PRICE if number is None else number


# ------------------------------------------------------------------ reading the tracker

def tracker_rows(connection: sqlite3.Connection, game_id: Any, *, as_of: str | None = None) -> list[dict[str, Any]]:
    """Each tracked book's latest line for a game, with the first line the tracker saw (its opener).

    `as_of` (ISO text) limits it to what was known by then, which is how a frozen pick captures its own moment.
    A database that has never run the tracker has no table, which reads as no rows, never as an error.
    """
    sql = "SELECT * FROM odds_api_lines WHERE game_id=?"
    params: list[Any] = [str(game_id)]
    if as_of:
        sql += " AND captured_at<=?"
        params.append(as_of)
    try:
        rows = connection.execute(sql + " ORDER BY line_id", params).fetchall()
    except sqlite3.OperationalError:
        return []
    first: dict[str, Any] = {}
    latest: dict[str, Any] = {}
    for row in rows:
        key = row["bookmaker"]
        if key not in TRACKED_BOOKS:
            continue
        first.setdefault(key, row)
        latest[key] = row
    out = []
    for key, row in latest.items():
        opener = first[key]
        out.append({
            "book": key, "name": book_name(key), "source": "odds-api",
            "home_spread": row["home_spread"], "home_price": row["home_spread_price"], "away_price": row["away_spread_price"],
            "total": row["total"], "over_price": row["over_price"], "under_price": row["under_price"],
            "updated": row["book_updated_at"] or row["captured_at"], "captured_at": row["captured_at"],
            "opened_spread": opener["home_spread"], "opened_total": opener["total"],
        })
    return out


def cfbd_rows(connection: sqlite3.Connection, game_id: Any) -> list[dict[str, Any]]:
    """College: CFBD's current DraftKings and ESPN BET quotes (no prices, no history), for books the tracker lacks."""
    try:
        rows = connection.execute(
            "SELECT provider, spread, spread_open, over_under, over_under_open, fetched_at FROM game_lines "
            "WHERE game_id=? ORDER BY fetched_at", (game_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = CFBD_BOOKS.get(str(row["provider"] or "").strip().casefold())
        if key is None or (row["spread"] is None and row["over_under"] is None):
            continue
        out[key] = {                                  # later fetches overwrite earlier ones; "Draft Kings" is DraftKings
            "book": key, "name": book_name(key), "source": "cfbd", "home_spread": row["spread"], "home_price": None,
            "away_price": None, "total": row["over_under"], "over_price": None, "under_price": None,
            "updated": row["fetched_at"], "captured_at": row["fetched_at"],
            "opened_spread": row["spread_open"], "opened_total": row["over_under_open"],
        }
    return list(out.values())


def espn_rows(connection: sqlite3.Connection, game_id: Any) -> list[dict[str, Any]]:
    """NFL: ESPN's DraftKings line (stored the nflverse way, positive = home favored, so the sign flips here)."""
    try:
        rows = connection.execute(
            "SELECT provider, open_spread, open_total, current_spread, current_total, fetched_at "
            "FROM espn_market_lines WHERE game_id=?", (str(game_id),)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        key = CFBD_BOOKS.get(str(row["provider"] or "").strip().casefold())
        if key is None or (row["current_spread"] is None and row["current_total"] is None):
            continue
        current, opened = _number(row["current_spread"]), _number(row["open_spread"])
        out.append({
            "book": key, "name": book_name(key), "source": "espn", "home_spread": None if current is None else -current,
            "home_price": None, "away_price": None, "total": row["current_total"], "over_price": None,
            "under_price": None, "updated": row["fetched_at"], "captured_at": row["fetched_at"],
            "opened_spread": None if opened is None else -opened, "opened_total": row["open_total"],
        })
    return out


def merge_fallback(rows: list[dict[str, Any]], fallback: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Add fallback books the tracker does not already supply (the tracker's reading is the fresher one)."""
    have = {row["book"] for row in rows}
    extra = [dict(row, name=row.get("name") or book_name(row["book"])) for row in (fallback or ())
             if row.get("book") in TRACKED_BOOKS and row["book"] not in have]
    return rows + extra


# ------------------------------------------------------------------ choosing the best

def _side_entry(row: dict[str, Any], number: float, price: Any) -> dict[str, Any]:
    return {"spread": number, "book": row["book"], "name": row["name"],
            "price": _number(price), "updated": row.get("updated")}


def best_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """{'home','away','over','under'} each the best number (then price) on offer, plus book count. None if no lines."""
    if not rows:
        return None
    result: dict[str, Any] = {}
    spreads = [r for r in rows if _number(r.get("home_spread")) is not None]
    if spreads:
        home = max(spreads, key=lambda r: (float(r["home_spread"]), _price_key(r.get("home_price"))))
        away = max(spreads, key=lambda r: (-float(r["home_spread"]), _price_key(r.get("away_price"))))
        result["home"] = _side_entry(home, float(home["home_spread"]), home.get("home_price"))
        result["away"] = _side_entry(away, -float(away["home_spread"]), away.get("away_price"))
    totals = [r for r in rows if _number(r.get("total")) is not None]
    if totals:
        over = max(totals, key=lambda r: (-float(r["total"]), _price_key(r.get("over_price"))))
        under = max(totals, key=lambda r: (float(r["total"]), _price_key(r.get("under_price"))))
        result["over"] = {"total": float(over["total"]), "book": over["book"], "name": over["name"],
                          "price": _number(over.get("over_price")), "updated": over.get("updated")}
        result["under"] = {"total": float(under["total"]), "book": under["book"], "name": under["name"],
                           "price": _number(under.get("under_price")), "updated": under.get("updated")}
    if not result:
        return None
    result["books"] = len({r["book"] for r in rows})
    if spreads:
        result["average_home_spread"] = round(sum(float(r["home_spread"]) for r in spreads) / len(spreads), 3)
    return result


def snapshot(connection: sqlite3.Connection, game_id: Any, *, as_of: str | None = None,
             fallback: Iterable[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The compact, frozen-packet form: the best of each side as of `as_of`, or None when nothing is known."""
    rows = merge_fallback(tracker_rows(connection, game_id, as_of=as_of), fallback)
    best = best_of(rows)
    if best is None:
        return None
    best["as_of"] = as_of or max((r.get("captured_at") or r.get("updated") or "" for r in rows), default=None) or None
    return best


def side_spread(best: dict[str, Any] | None, side: str | None) -> tuple[float | None, str | None]:
    """The spread a pick on `side` ('home'/'away') gets from the best line, and the book that offers it."""
    entry = (best or {}).get(side or "")
    if not isinstance(entry, dict) or entry.get("spread") is None:
        return None, None
    return float(entry["spread"]), entry.get("name") or entry.get("book")


def signature(best: dict[str, Any] | None) -> list[Any] | None:
    """What about the best line should make a frozen pick refresh when it changes: numbers and books, not timestamps."""
    if not best:
        return None
    return [[best[k].get("spread", best[k].get("total")), best[k].get("book")]
            if isinstance(best.get(k), dict) else None for k in ("home", "away", "over", "under")]


# ------------------------------------------------------------------ for display

def display_rows(connection: sqlite3.Connection, game_id: Any, *,
                 fallback: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Every tracked book beside the others for the game page, the best numbers flagged, with movement since opening."""
    rows = merge_fallback(tracker_rows(connection, game_id), fallback)
    best = best_of(rows)
    out = []
    for row in sorted(rows, key=lambda r: list(BOOK_NAMES).index(r["book"]) if r["book"] in BOOK_NAMES else 99):
        home = _number(row.get("home_spread"))
        total = _number(row.get("total"))
        opened = _number(row.get("opened_spread"))
        opened_total = _number(row.get("opened_total"))
        out.append({
            **row,
            "away_spread": -home if home is not None else None,
            "spread_move": (home - opened) if home is not None and opened is not None else None,
            "total_move": (total - opened_total) if total is not None and opened_total is not None else None,
            "best_home": bool(best and "home" in best and home == best["home"]["spread"]
                              and _price_key(row.get("home_price")) == _price_key(best["home"]["price"])),
            "best_away": bool(best and "away" in best and home is not None and -home == best["away"]["spread"]
                              and _price_key(row.get("away_price")) == _price_key(best["away"]["price"])),
            "best_over": bool(best and "over" in best and total == best["over"]["total"]
                              and _price_key(row.get("over_price")) == _price_key(best["over"]["price"])),
            "best_under": bool(best and "under" in best and total == best["under"]["total"]
                               and _price_key(row.get("under_price")) == _price_key(best["under"]["price"])),
        })
    return {"rows": out, "best": best, "has_data": bool(out)}
