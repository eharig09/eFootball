"""ESPN (DraftKings) opening and current NFL lines.

nflverse carries a single line per game, which is effectively the close, so it
cannot say where a number opened. ESPN's event odds resource keeps both the
opener and the latest line -- including for finished games, where "latest" is
the closing line -- which is what the movement charts and closing-line value
need. One scoreboard request per week maps ESPN event ids to games; the odds
resource is then read per event.

Sign convention everywhere in this module: spreads are stored the way nflverse
stores them (positive = home favored), so they compare directly with
games.spread_line and the forecast ledger.
"""
from __future__ import annotations

import os
from typing import Any

import requests

from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.repository import NFLRepository

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ODDS_URL = ("https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
            "events/{event_id}/competitions/{event_id}/odds")
TIMEOUT = 30


def _number(value: Any) -> float | None:
    """Parse '-8.5', '+3', 'o45.5', 'PK' -- anything else is unknown, never zero."""
    if value is None:
        return None
    text = str(value).strip().lower().lstrip("ou")
    if text in ("pk", "pick", "even", "ev"):
        return 0.0
    try:
        return float(text.replace("+", ""))
    except ValueError:
        return None


def _american(node: Any) -> float | None:
    return _number((node or {}).get("american")) if isinstance(node, dict) else None


def parse_odds(item: dict[str, Any]) -> dict[str, Any]:
    """Normalise one ESPN odds item to opener/current values in nflverse sign."""
    home, away = item.get("homeTeamOdds") or {}, item.get("awayTeamOdds") or {}

    def home_spread(state: str) -> float | None:
        line = _american((home.get(state) or {}).get("pointSpread"))
        return None if line is None else (0.0 if line == 0 else -line)

    def total(state: str) -> float | None:
        return _american((item.get(state) or {}).get("total"))

    def money(side: dict[str, Any], state: str) -> float | None:
        return _american((side.get(state) or {}).get("moneyLine"))

    return {
        "provider": (item.get("provider") or {}).get("name") or "ESPN",
        "open_spread": home_spread("open"), "open_total": total("open"),
        "open_home_moneyline": money(home, "open"), "open_away_moneyline": money(away, "open"),
        "current_spread": home_spread("current"), "current_total": total("current"),
        "current_home_moneyline": money(home, "current"),
        "current_away_moneyline": money(away, "current"),
        "home_spread_odds": home.get("spreadOdds"), "away_spread_odds": away.get("spreadOdds"),
        "over_odds": item.get("overOdds"), "under_odds": item.get("underOdds"),
    }


def week_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    for event in payload.get("events") or []:
        competitors = (event.get("competitions") or [{}])[0].get("competitors") or []
        sides = {c.get("homeAway"): canon_team((c.get("team") or {}).get("abbreviation"))
                 for c in competitors}
        if "home" in sides and "away" in sides:
            events.append({"event_id": str(event["id"]), "home": sides["home"],
                           "away": sides["away"],
                           "completed": bool(((event.get("status") or {}).get("type") or {})
                                             .get("completed"))})
    return events


def sync_market_lines(repository: NFLRepository, season: int, weeks: list[int], *,
                      session: requests.Session | None = None,
                      skip_settled: bool = True) -> dict[str, int]:
    """Fetch and store ESPN lines for the given weeks.

    `skip_settled` leaves finished games that already have a stored opener
    alone: their line can no longer move, so refetching them every run would
    only burn requests. Network failures for one game never abort the rest.
    """
    session = session or requests.Session()
    schedule = repository.schedule(int(season))
    by_teams = {(int(g["week"]), g["away_team"], g["home_team"]): g for g in schedule}
    known = repository.espn_market_game_ids(int(season)) if skip_settled else set()
    counts = {"games": 0, "changed": 0, "missing": 0, "errors": 0}
    for week in weeks:
        try:
            response = session.get(SCOREBOARD_URL, timeout=TIMEOUT, params={
                "dates": int(season), "seasontype": 2, "week": int(week)})
            response.raise_for_status()
            events = week_events(response.json())
        except (requests.RequestException, ValueError):
            counts["errors"] += 1
            continue
        for event in events:
            game = by_teams.get((int(week), event["away"], event["home"]))
            if game is None:
                counts["missing"] += 1
                continue
            if skip_settled and game["completed"] and game["game_id"] in known:
                continue
            try:
                response = session.get(ODDS_URL.format(event_id=event["event_id"]), timeout=TIMEOUT)
                response.raise_for_status()
                items = response.json().get("items") or []
            except (requests.RequestException, ValueError):
                counts["errors"] += 1
                continue
            if not items:
                continue
            row = parse_odds(items[0])
            counts["games"] += 1
            if repository.record_espn_market(
                    game["game_id"], int(season), int(week),
                    {**row, "event_id": event["event_id"], "completed": game["completed"]}):
                counts["changed"] += 1
    return counts


# ---------------------------------------------------------------- game panel

def _clock(stamp: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(
            ZoneInfo("America/New_York"))
    except ValueError:
        return stamp[:16]
    return moment.strftime("%a %-I%p" if os.name != "nt" else "%a %I%p").replace(" 0", " ")


def movement_packet(repository: NFLRepository, game: dict[str, Any], *,
                    model_margin: float | None = None,
                    model_total: float | None = None) -> dict[str, Any]:
    """Opener-to-latest movement for one game, in the game page's away-team-line convention.

    nflverse's positive-home-favored spread_line is numerically the away team's
    line, so no sign flip is needed. Football Lab's home margin lives on the same
    scale and is drawn as a dashed reference.
    """
    from sports_aggregator.nfl.pick_record import line_chart
    from sports_aggregator.tables import Column, Table

    market = repository.espn_market(game["game_id"])
    if market is None:
        return {"available": False}
    snapshots = [s for s in repository.market_line_history(game["game_id"])
                 if s["source"] == "espn"]
    labels = ["Open"] + [_clock(s["captured_at"]) for s in snapshots]

    def chart(open_key: str, field: str, model: float | None, name: str) -> dict[str, Any]:
        base = market.get(open_key)
        values = {0: base} if base is not None else {}
        for index, snap in enumerate(snapshots, start=1):
            if snap.get(field) is not None:
                values[index] = float(snap[field])
        if len(values) < 2:
            return {"has_data": False}
        xs = sorted(values)
        series = [{"key": "off", "label": name, "values": values}]
        if model is not None:
            series.append({"key": "def", "label": "Football Lab",
                           "values": {x: float(model) for x in xs},
                           "dashed": True, "markers": False})
        return line_chart(xs, series, fmt=".1f", label_of=lambda i: labels[i],
                          label_every=max(1, len(xs) // 6))

    def move(open_key: str, now_key: str) -> float | None:
        a, b = market.get(open_key), market.get(now_key)
        return None if a is None or b is None else round(float(b) - float(a), 2)

    def show(value: float | None, kind: str) -> str | None:
        if value is None:
            return None
        if kind == "total":
            return f"{value:g}"
        if kind == "ml":
            return f"{value:+.0f}"
        return "PK" if value == 0 else f"{value:+g}"

    rows = []
    for label, open_key, now_key, model, kind in (
            (f"{game['away_team']} spread", "open_spread", "current_spread", model_margin, "spread"),
            ("Total", "open_total", "current_total", model_total, "total"),
            (f"{game['away_team']} moneyline", "open_away_moneyline", "current_away_moneyline", None, "ml"),
            (f"{game['home_team']} moneyline", "open_home_moneyline", "current_home_moneyline", None, "ml")):
        change = move(open_key, now_key)
        rows.append({"market": label, "open": show(market.get(open_key), kind),
                     "now": show(market.get(now_key), kind),
                     "move": None if change is None else ("0" if change == 0 else f"{change:+g}"),
                     "model": show(model, "total" if kind == "total" else "spread")
                     if kind != "ml" else None})
    table = Table([
        Column("market", "Market", "text", emphasis=True),
        Column("open", "Open", "text", align="right"),
        Column("now", "Close" if game.get("completed") else "Now", "text", align="right"),
        Column("move", "Move", "text", align="right"),
        Column("model", "Football Lab", "text", align="right",
               title="Football Lab on the same scale (margin or total)"),
    ], rows, dense=True, sortable=False)
    return {
        "available": True, "provider": market["provider"], "final": bool(market["final"]),
        "table": table, "snapshots": len(snapshots),
        "spread_chart": chart("open_spread", "spread_line", model_margin,
                              f"{game['away_team']} line"),
        "total_chart": chart("open_total", "total_line", model_total, "Total"),
        "spread_move": move("open_spread", "current_spread"),
        "total_move": move("open_total", "current_total"),
    }
