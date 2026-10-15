"""Track sportsbook spreads and totals from The Odds API inside the free tier's credit budget.

One /odds request returns every game that has a line for a sport, so credits are spent per *sweep*, not per
game: spreads + totals for one region costs 2 credits whatever the slate size. The free plan is 500 credits a
month (account-wide), so sweeps run only at a few scheduled slots a week, and three guards stand between a bug
and an empty quota: a calendar slot must be due, the free /events endpoint must show games coming up, and the
shared ledger plus the API's own remaining-credit header must leave room under a hard monthly cap.

Rows are appended only when a book's number moves, so a snapshot table reads as a line history. They are stored
beside the games they describe (cfb.sqlite3 / nfl.sqlite3); the credit ledger is one small file shared by both.

    python -m sports_aggregator.odds_api_tracker run-due          # what the scheduler calls; usually does nothing
    python -m sports_aggregator.odds_api_tracker sweep --league cfb --force
    python -m sports_aggregator.odds_api_tracker status
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import unicodedata
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://api.the-odds-api.com/v4"
SPORTS = {"cfb": "americanfootball_ncaaf", "nfl": "americanfootball_nfl"}
MARKETS = "spreads,totals"
REGIONS = "us"
#: The only books stored (The Odds API bookmaker keys). The request still asks for the whole US region, which costs
#: the same 2 credits, and the rest are dropped before they reach the database. espnbet is listed so it is kept if
#: the feed offers it; it was not in the live response when this was written.
TRACKED_BOOKS = frozenset({"draftkings", "espnbet", "fanduel", "betmgm", "betrivers"})
CALL_COST = 2                       # markets x regions
MONTHLY_CAP = 450                   # of 500: the rest is a cushion for manual checks and clock skew
MIN_REMAINING = 40                  # never call when the API itself reports fewer credits than this
LOOKAHEAD_DAYS = 8                  # a sweep needs a game this close (the /events check is free)
SLOT_WINDOW_HOURS = 3               # a slot can still fire this long after its time (scheduler jitter, restarts)
TIMEOUT = 30
EASTERN = ZoneInfo("America/New_York")

#: (weekday Mon=0..Sun=6, hour, minute) in Eastern time. College: openers Sunday night, midweek, then the three
#: Saturday waves so each wave's closing number is caught. NFL: midweek, Thursday night, Sunday windows, Monday.
SLOTS: dict[str, tuple[tuple[int, int, int], ...]] = {
    "cfb": ((6, 20, 0), (1, 12, 0), (3, 12, 0), (4, 12, 0), (5, 11, 0), (5, 15, 0), (5, 19, 0)),
    "nfl": ((2, 12, 0), (3, 17, 0), (5, 12, 0), (6, 11, 30), (6, 15, 30), (0, 17, 30)),
}

LINES_SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_api_lines (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    league TEXT NOT NULL,
    event_id TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    game_id TEXT,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    home_spread REAL, home_spread_price REAL, away_spread_price REAL,
    total REAL, over_price REAL, under_price REAL,
    book_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_odds_api_lines_event ON odds_api_lines(event_id, bookmaker, line_id);
CREATE INDEX IF NOT EXISTS idx_odds_api_lines_game ON odds_api_lines(game_id);
"""

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_api_calls (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at TEXT NOT NULL,
    league TEXT NOT NULL,
    cost INTEGER NOT NULL,
    remaining INTEGER,
    used INTEGER,
    events INTEGER,
    rows_stored INTEGER,
    matched_games INTEGER,
    status TEXT NOT NULL,
    message TEXT
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ paths and storage

def league_database(league: str) -> Path:
    default = "instance/cfb.sqlite3" if league == "cfb" else "instance/nfl.sqlite3"
    env = "CFB_DATABASE_PATH" if league == "cfb" else "NFL_DATABASE_PATH"
    return Path(os.getenv(env) or default)


def ledger_path() -> Path:
    explicit = os.getenv("ODDS_API_LEDGER_PATH")
    return Path(explicit) if explicit else league_database("cfb").parent / "odds_api_usage.sqlite3"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


# ------------------------------------------------------------------ budget

def month_used(ledger: sqlite3.Connection, now: datetime) -> int:
    """Credits this tool has spent since the first of the month (UTC), both leagues."""
    start = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = ledger.execute("SELECT COALESCE(SUM(cost),0) FROM odds_api_calls WHERE called_at>=? AND status='ok'",
                         (_iso(start),)).fetchone()
    return int(row[0])


def last_known_remaining(ledger: sqlite3.Connection, now: datetime) -> int | None:
    """The API's own count from the latest call this month, which also reflects credits spent elsewhere."""
    start = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = ledger.execute("SELECT remaining FROM odds_api_calls WHERE called_at>=? AND remaining IS NOT NULL "
                         "ORDER BY call_id DESC LIMIT 1", (_iso(start),)).fetchone()
    return int(row[0]) if row else None


def last_known_used(ledger: sqlite3.Connection, now: datetime) -> int | None:
    """The API's own credits-used count from the latest call this month (includes credits spent outside this tool)."""
    start = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = ledger.execute("SELECT used FROM odds_api_calls WHERE called_at>=? AND used IS NOT NULL "
                         "ORDER BY call_id DESC LIMIT 1", (_iso(start),)).fetchone()
    return int(row[0]) if row else None


def budget_allows(ledger: sqlite3.Connection, now: datetime, cost: int = CALL_COST) -> tuple[bool, str]:
    used = max(month_used(ledger, now), last_known_used(ledger, now) or 0)
    if used + cost > MONTHLY_CAP:
        return False, f"monthly cap: {used} credits used of {MONTHLY_CAP} allowed ({cost} needed)"
    remaining = last_known_remaining(ledger, now)
    if remaining is not None and remaining - cost < MIN_REMAINING:
        return False, f"the API reports {remaining} credits remaining; keeping {MIN_REMAINING} in reserve"
    return True, ""


# ------------------------------------------------------------------ schedule

def slot_due(now: datetime, league: str, last_call: datetime | None,
             window_hours: float = SLOT_WINDOW_HOURS) -> datetime | None:
    """The scheduled slot (UTC) that is due now, or None. Each slot fires at most once."""
    local = now.astimezone(EASTERN)
    for weekday, hour, minute in SLOTS[league]:
        days_back = (local.weekday() - weekday) % 7
        slot = (local - timedelta(days=days_back)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot > local:
            slot -= timedelta(days=7)
        age = local - slot
        if timedelta(0) <= age <= timedelta(hours=window_hours):
            slot_utc = slot.astimezone(timezone.utc)
            if last_call is None or last_call < slot_utc:
                return slot_utc
    return None


# ------------------------------------------------------------------ API

def _get(path: str, params: dict[str, Any], session=requests) -> requests.Response:
    response = session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def _credits(headers: Any) -> tuple[int | None, int | None, int | None]:
    def number(name: str) -> int | None:
        try:
            return int(float(headers.get(name)))
        except (TypeError, ValueError):
            return None
    return number("x-requests-remaining"), number("x-requests-used"), number("x-requests-last")


# ------------------------------------------------------------------ parsing

def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def parse_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per bookmaker: the home spread and both prices, the total and both prices."""
    home, away = event.get("home_team"), event.get("away_team")
    rows = []
    for book in event.get("bookmakers") or []:
        if book.get("key") not in TRACKED_BOOKS:
            continue
        row: dict[str, Any] = {
            "bookmaker": book.get("key"), "book_updated_at": book.get("last_update"),
            "home_spread": None, "home_spread_price": None, "away_spread_price": None,
            "total": None, "over_price": None, "under_price": None,
        }
        for market in book.get("markets") or []:
            outcomes = {o.get("name"): o for o in market.get("outcomes") or []}
            if market.get("key") == "spreads" and home in outcomes and away in outcomes:
                row["home_spread"] = _number(outcomes[home].get("point"))
                row["home_spread_price"] = _number(outcomes[home].get("price"))
                row["away_spread_price"] = _number(outcomes[away].get("price"))
            elif market.get("key") == "totals" and "Over" in outcomes and "Under" in outcomes:
                row["total"] = _number(outcomes["Over"].get("point"))
                row["over_price"] = _number(outcomes["Over"].get("price"))
                row["under_price"] = _number(outcomes["Under"].get("price"))
        if row["bookmaker"] and (row["home_spread"] is not None or row["total"] is not None):
            rows.append(row)
    return rows


# ------------------------------------------------------------------ matching to our games

#: Names the odds feed spells differently from the teams table after normalisation. The table holds
#: "San Jos� State" (a mangled accent), so the plain spelling needs a nudge.
NAME_FIXES = {"san jose state": "san jos state"}


def _normalize(text: str) -> str:
    from sports_aggregator.cfb.models import normalize_alias
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    key = normalize_alias(plain.replace("'", "").replace("’", ""))
    return NAME_FIXES.get(key, key)


def cfb_team_lookup(connection: sqlite3.Connection) -> dict[str, int | None]:
    """normalized name -> team_id, None where two teams share it (never guess between them)."""
    lookup: dict[str, int | None] = {}

    def add(name: str, team_id: int) -> None:
        key = _normalize(name)
        if not key:
            return
        lookup[key] = team_id if lookup.get(key, team_id) == team_id else None

    for row in connection.execute("SELECT team_id, school, mascot FROM teams"):
        add(row["school"], row["team_id"])
        if row["mascot"]:
            add(f"{row['school']} {row['mascot']}", row["team_id"])
    try:
        for row in connection.execute("SELECT team_id, normalized_alias FROM team_aliases"):
            add(row["normalized_alias"], row["team_id"])
    except sqlite3.OperationalError:
        pass
    return lookup


def resolve_cfb_team(name: str, lookup: dict[str, int | None]) -> int | None:
    """'Kennesaw State Owls' -> the school: try the whole name, then drop trailing words."""
    words = name.split()
    for length in range(len(words), 0, -1):
        team_id = lookup.get(_normalize(" ".join(words[:length])))
        if team_id is not None:
            return team_id
    return None


def cfb_game_id(connection: sqlite3.Connection, lookup: dict[str, int | None], event: dict[str, Any]) -> str | None:
    home = resolve_cfb_team(event.get("home_team") or "", lookup)
    away = resolve_cfb_team(event.get("away_team") or "", lookup)
    kickoff = _parse(event.get("commence_time"))
    if kickoff is None or (home is None and away is None):
        return None
    low, high = _iso(kickoff - timedelta(hours=36)), _iso(kickoff + timedelta(hours=36))
    if home is None or away is None:
        # An FCS opponent is not in the teams table. The FBS side plays once in a 72-hour window, so its game is
        # unambiguous; if that ever stops being true the match is refused rather than guessed.
        known = home if home is not None else away
        rows = connection.execute(
            "SELECT game_id FROM games WHERE (home_team_id=? OR away_team_id=?) AND start_date BETWEEN ? AND ?",
            (known, known, low, high)).fetchall()
        return str(rows[0][0]) if len(rows) == 1 else None
    rows = connection.execute(
        "SELECT game_id FROM games WHERE home_team_id=? AND away_team_id=? AND start_date BETWEEN ? AND ?",
        (home, away, low, high)).fetchall()
    if not rows:                                   # neutral-site games are sometimes listed the other way round
        rows = connection.execute(
            "SELECT game_id FROM games WHERE home_team_id=? AND away_team_id=? AND start_date BETWEEN ? AND ?",
            (away, home, low, high)).fetchall()
    return str(rows[0][0]) if len(rows) == 1 else None


def nfl_game_id(connection: sqlite3.Connection, names: dict[str, str], event: dict[str, Any]) -> str | None:
    home, away = names.get(event.get("home_team") or ""), names.get(event.get("away_team") or "")
    kickoff = _parse(event.get("commence_time"))
    if not home or not away or kickoff is None:
        return None
    low = (kickoff - timedelta(hours=36)).date().isoformat()
    high = (kickoff + timedelta(hours=36)).date().isoformat()
    rows = connection.execute(
        "SELECT game_id FROM games WHERE home_team=? AND away_team=? AND game_date BETWEEN ? AND ?",
        (home, away, low, high)).fetchall()
    return str(rows[0][0]) if len(rows) == 1 else None


# ------------------------------------------------------------------ storing

_VALUE_COLUMNS = ("home_spread", "home_spread_price", "away_spread_price", "total", "over_price", "under_price")


def store_snapshot(connection: sqlite3.Connection, league: str, captured_at: datetime, events: list[dict[str, Any]],
                   resolve: Callable[[dict[str, Any]], str | None]) -> tuple[int, int]:
    """Append a row per (event, book) whose numbers changed. Returns (rows stored, events matched to a game)."""
    connection.executescript(LINES_SCHEMA)
    stored = matched = 0
    for event in events:
        game_id = resolve(event)
        matched += game_id is not None
        if game_id is not None:           # rows stored before this game could be matched get its id
            connection.execute("UPDATE odds_api_lines SET game_id=? WHERE event_id=? AND game_id IS NULL",
                               (game_id, event["id"]))
        for row in parse_event(event):
            latest = connection.execute(
                "SELECT * FROM odds_api_lines WHERE event_id=? AND bookmaker=? ORDER BY line_id DESC LIMIT 1",
                (event["id"], row["bookmaker"])).fetchone()
            if latest is not None and all(latest[c] == row[c] for c in _VALUE_COLUMNS):
                continue
            connection.execute(
                "INSERT INTO odds_api_lines (captured_at,league,event_id,commence_time,game_id,home_team,away_team,"
                "bookmaker,home_spread,home_spread_price,away_spread_price,total,over_price,under_price,book_updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_iso(captured_at), league, event["id"], event["commence_time"], game_id, event["home_team"],
                 event["away_team"], row["bookmaker"], row["home_spread"], row["home_spread_price"],
                 row["away_spread_price"], row["total"], row["over_price"], row["under_price"], row["book_updated_at"]))
            stored += 1
    connection.commit()
    return stored, matched


# ------------------------------------------------------------------ orchestration

def _record(ledger: sqlite3.Connection, now: datetime, league: str, status: str, *, cost: int = 0,
            headers: Any = None, events: int | None = None, stored: int | None = None,
            matched: int | None = None, message: str = "") -> None:
    remaining, used, _last = _credits(headers) if headers is not None else (None, None, None)
    ledger.execute(
        "INSERT INTO odds_api_calls (called_at,league,cost,remaining,used,events,rows_stored,matched_games,status,message) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_iso(now), league, cost, remaining, used, events, stored, matched, status, message[:300]))
    ledger.commit()


def sweep(league: str, *, force: bool = False, now: datetime | None = None, session=requests,
          api_key: str | None = None) -> dict[str, Any]:
    """One guarded sweep. Never raises for an expected refusal; returns {'status': ..., 'message': ...}."""
    now = now or _now()
    key = api_key or (os.getenv("ODDS_API_KEY") or "").strip()
    if not key:
        return {"status": "skipped", "message": "ODDS_API_KEY is not set"}
    if league not in SPORTS:
        raise ValueError(f"unknown league {league!r}")
    with closing(_connect(ledger_path())) as ledger:
        ledger.executescript(LEDGER_SCHEMA)
        last = ledger.execute("SELECT MAX(called_at) FROM odds_api_calls WHERE league=? AND status='ok'", (league,)).fetchone()[0]
        if not force and slot_due(now, league, _parse(last)) is None:
            return {"status": "not-due", "message": "no scheduled slot is due"}
        ok, reason = budget_allows(ledger, now)
        if not ok:
            _record(ledger, now, league, "budget", message=reason)
            return {"status": "budget", "message": reason}
        sport = SPORTS[league]
        try:
            upcoming = _get(f"/sports/{sport}/events", {"apiKey": key}, session).json()     # free: costs no credits
        except Exception as exc:
            _record(ledger, now, league, "error", message=f"events: {exc}")
            return {"status": "error", "message": f"events check failed: {exc}"}
        horizon = now + timedelta(days=LOOKAHEAD_DAYS)
        soon = [e for e in upcoming if (_parse(e.get("commence_time")) or horizon) <= horizon]
        if not soon:
            _record(ledger, now, league, "no-games", events=0, message="no games inside the look-ahead window")
            return {"status": "no-games", "message": "no games inside the look-ahead window; nothing spent"}
        try:
            response = _get(f"/sports/{sport}/odds", {"apiKey": key, "regions": REGIONS, "markets": MARKETS,
                                                       "oddsFormat": "american"}, session)
            events = response.json()
        except Exception as exc:
            _record(ledger, now, league, "error", message=f"odds: {str(exc).replace(key, '***')}")
            return {"status": "error", "message": f"odds request failed: {str(exc).replace(key, '***')}"}
        with closing(_connect(league_database(league))) as connection:
            if league == "cfb":
                lookup = cfb_team_lookup(connection)
                resolve = lambda event: cfb_game_id(connection, lookup, event)       # noqa: E731
            else:
                names = {row["name"]: row["abbreviation"] for row in connection.execute("SELECT abbreviation, name FROM teams")}
                resolve = lambda event: nfl_game_id(connection, names, event)         # noqa: E731
            stored, matched = store_snapshot(connection, league, now, events, resolve)
        remaining, used, last_cost = _credits(response.headers)
        _record(ledger, now, league, "ok", cost=last_cost if last_cost is not None else CALL_COST,
                headers=response.headers, events=len(events), stored=stored, matched=matched)
        return {"status": "ok", "events": len(events), "matched_games": matched, "rows_stored": stored,
                "credits_remaining": remaining, "credits_used": used}


def run_due(*, now: datetime | None = None, session=requests) -> dict[str, dict[str, Any]]:
    return {league: sweep(league, now=now, session=session) for league in SPORTS}


def status() -> dict[str, Any]:
    now = _now()
    with closing(_connect(ledger_path())) as ledger:
        ledger.executescript(LEDGER_SCHEMA)
        out: dict[str, Any] = {"month_used": month_used(ledger, now), "monthly_cap": MONTHLY_CAP,
                               "api_remaining": last_known_remaining(ledger, now),
                               "recent_calls": [dict(r) for r in ledger.execute(
                                   "SELECT called_at,league,status,cost,remaining,events,rows_stored,matched_games,message "
                                   "FROM odds_api_calls ORDER BY call_id DESC LIMIT 8")]}
    for league in SPORTS:
        path = league_database(league)
        if path.exists():
            with closing(_connect(path)) as connection:
                try:
                    out[league] = dict(connection.execute(
                        "SELECT COUNT(*) rows_stored, COUNT(DISTINCT event_id) events, COUNT(DISTINCT bookmaker) books, "
                        "MAX(captured_at) latest FROM odds_api_lines").fetchone())
                except sqlite3.OperationalError:
                    out[league] = {"rows_stored": 0}
    return out


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:                                                       # pragma: no cover
        pass
    parser = argparse.ArgumentParser(description="Track sportsbook lines from The Odds API within the free quota")
    parser.add_argument("command", choices=("run-due", "sweep", "status"))
    parser.add_argument("--league", choices=tuple(SPORTS), default="cfb")
    parser.add_argument("--force", action="store_true", help="skip the schedule slot (the credit budget still applies)")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(), indent=2, default=str))
        return 0
    result = run_due() if args.command == "run-due" else sweep(args.league, force=args.force)
    print(json.dumps(result, indent=2, default=str))
    statuses = [result.get("status")] if "status" in result else [r.get("status") for r in result.values()]
    return 1 if "error" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
