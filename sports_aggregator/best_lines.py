"""The best available number across the tracked sportsbooks, for a game at a moment in time.

Picks are judged against the best line a bettor could actually have taken: for a pick on a side, the most
favorable spread among the tracked books, and for an over or under the most favorable total. Ties on the number
go to the better price. Both leagues use this module; each supplies its own fallback rows for books the odds
tracker does not cover (CFBD's DraftKings and ESPN BET quotes for college, ESPN's DraftKings feed for the NFL).

Moneylines work the same way: the best price on offer for a side is the highest American price (+135 beats +130,
-120 beats -130), because that is the one that pays most.

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
        columns = row.keys()
        out.append({
            "book": key, "name": book_name(key), "source": "odds-api",
            "home_spread": row["home_spread"], "home_price": row["home_spread_price"], "away_price": row["away_spread_price"],
            "total": row["total"], "over_price": row["over_price"], "under_price": row["under_price"],
            "home_moneyline": row["home_moneyline"] if "home_moneyline" in columns else None,
            "away_moneyline": row["away_moneyline"] if "away_moneyline" in columns else None,
            "updated": row["book_updated_at"] or row["captured_at"], "captured_at": row["captured_at"],
            "opened_spread": opener["home_spread"], "opened_total": opener["total"],
        })
    return out


def _col(row: sqlite3.Row, name: str) -> Any:
    """A column's value, or None when this copy of the table does not have it."""
    return row[name] if name in row.keys() else None


def cfbd_rows(connection: sqlite3.Connection, game_id: Any) -> list[dict[str, Any]]:
    """College: CFBD's current DraftKings and ESPN BET quotes (no prices, no history), for books the tracker lacks."""
    try:
        rows = connection.execute(
            "SELECT * FROM game_lines WHERE game_id=? ORDER BY fetched_at", (game_id,)).fetchall()
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
            "home_moneyline": _col(row, "home_moneyline"), "away_moneyline": _col(row, "away_moneyline"),
            "updated": row["fetched_at"], "captured_at": row["fetched_at"],
            "opened_spread": row["spread_open"], "opened_total": row["over_under_open"],
        }
    return list(out.values())


def espn_rows(connection: sqlite3.Connection, game_id: Any) -> list[dict[str, Any]]:
    """NFL: ESPN's DraftKings line (stored the nflverse way, positive = home favored, so the sign flips here)."""
    try:
        rows = connection.execute(
            "SELECT * FROM espn_market_lines WHERE game_id=?", (str(game_id),)).fetchall()
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
            "under_price": None, "home_moneyline": _col(row, "current_home_moneyline"),
            "away_moneyline": _col(row, "current_away_moneyline"),
            "updated": row["fetched_at"], "captured_at": row["fetched_at"],
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
    for side, column in (("home_ml", "home_moneyline"), ("away_ml", "away_moneyline")):
        priced = [r for r in rows if _number(r.get(column)) is not None]
        if priced:
            top = max(priced, key=lambda r: float(r[column]))
            result[side] = {"price": float(top[column]), "book": top["book"], "name": top["name"],
                            "updated": top.get("updated")}
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


def side_moneyline(best: dict[str, Any] | None, side: str | None) -> tuple[float | None, str | None]:
    """The best moneyline price for `side` ('home'/'away') and the book that offers it."""
    entry = (best or {}).get(f"{side}_ml" if side in ("home", "away") else "")
    if not isinstance(entry, dict) or entry.get("price") is None:
        return None, None
    return float(entry["price"]), entry.get("name") or entry.get("book")


def signature(best: dict[str, Any] | None) -> list[Any] | None:
    """What about the best line should make a frozen pick refresh when it changes: numbers and books, not timestamps."""
    if not best:
        return None
    return [[best[k].get("spread", best[k].get("total")), best[k].get("book")]
            if isinstance(best.get(k), dict) else None for k in ("home", "away", "over", "under")]


# ------------------------------------------------------------------ for display

def _formatted_spread(home_spread: float | None, home: str, away: str) -> str | None:
    """'Georgia Southern -3.5': the favorite and its number, the way CFBD formats a quote."""
    if home_spread is None:
        return None
    if home_spread < 0:
        return f"{home} {home_spread:+.1f}"
    if home_spread > 0:
        return f"{away} {-home_spread:+.1f}"
    return "Pick'em"


def _cfbd_book(provider: Any) -> str | None:
    return CFBD_BOOKS.get(str(provider or "").strip().casefold())


def table_providers(providers: list[dict[str, Any]], tracked: Iterable[dict[str, Any]], home: str, away: str
                    ) -> list[dict[str, Any]]:
    """CFBD's provider rows plus the tracked books, shaped like `game_lines` rows, for the Book by book table.

    DISPLAY ONLY. `game_lines` consensus is an input to the engines, so it is never fed from here. For a book both
    have (DraftKings) the tracker's fresher reading replaces CFBD's but keeps CFBD's opening numbers and moneylines,
    so movement still runs from the true open; a book only the tracker has (FanDuel, BetMGM, BetRivers) moves from the
    first line the tracker saw. Books only CFBD has (Bovada) are kept as they were.
    """
    tracked = [row for row in tracked if row.get("source") == "odds-api"]
    if not tracked:
        return list(providers)
    cfbd_by_book = {}
    for provider in providers:
        key = _cfbd_book(provider.get("provider"))
        if key:
            cfbd_by_book.setdefault(key, provider)
    merged = [p for p in providers if _cfbd_book(p.get("provider")) not in {row["book"] for row in tracked}]
    for row in tracked:
        old = cfbd_by_book.get(row["book"]) or {}
        spread, total = _number(row.get("home_spread")), _number(row.get("total"))
        spread_open = _number(old.get("spread_open")) if old.get("spread_open") is not None else _number(row.get("opened_spread"))
        total_open = _number(old.get("over_under_open")) if old.get("over_under_open") is not None else _number(row.get("opened_total"))
        merged.append({
            "home_moneyline": None, "away_moneyline": None, "home_win_prob": None,
            **{k: v for k, v in old.items() if k in ("home_moneyline", "away_moneyline", "home_win_prob")},
            "provider": row["name"], "spread": spread, "formatted_spread": _formatted_spread(spread, home, away),
            "spread_open": spread_open, "over_under": total, "over_under_open": total_open,
            "spread_move": round(spread - spread_open, 1) if spread is not None and spread_open is not None else None,
            "total_move": round(total - total_open, 1) if total is not None and total_open is not None else None,
            "fetched_at": row.get("captured_at") or row.get("updated"), "source": "odds-api",
        })
        if _number(row.get("home_moneyline")) is not None:      # the tracker's reading is the fresher one
            merged[-1]["home_moneyline"] = _number(row["home_moneyline"])
        if _number(row.get("away_moneyline")) is not None:
            merged[-1]["away_moneyline"] = _number(row["away_moneyline"])
    return sorted(merged, key=lambda p: str(p.get("provider") or "").casefold())


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
            "best_home_ml": bool(best and "home_ml" in best and _number(row.get("home_moneyline")) == best["home_ml"]["price"]),
            "best_away_ml": bool(best and "away_ml" in best and _number(row.get("away_moneyline")) == best["away_ml"]["price"]),
        })
    return {"rows": out, "best": best, "has_data": bool(out)}
