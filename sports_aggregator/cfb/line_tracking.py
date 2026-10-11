"""Market line tracking for the CFB matchup page's engine section.

Spread and total are drawn from the opener through every stored consensus reading to
now, with the engine's own number as a dashed reference and a team logo wherever an
engine pick was confirmed. Spread is the home line (negative = home favoured), the way
the market table and the pick manifest already state it, so the engine's home margin is
negated onto the same scale.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sports_aggregator.nfl.pick_record import line_chart

_ENGINE_LABELS = {"agreement": "Both engines", "engine_a_only": "Engine A", "engine_b_only": "Engine B"}


def _parse(value: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _clock(moment: datetime | None) -> str:
    return "" if moment is None else moment.astimezone(timezone.utc).strftime("%m-%d %H:%M")


def _logo(identity: dict[str, Any] | None) -> str | None:
    identity = identity or {}
    return identity.get("logo_dark") or identity.get("logo")


def confirmed_picks(history: list[dict[str, Any]], current: dict[str, Any] | None,
                    game: dict[str, Any], identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each time an engine pick was confirmed or changed, oldest first; the live one is flagged."""
    entries = [{"at": _parse(row.get("recorded_at")), "team": row.get("selected_team"),
                "state": row.get("state")} for row in history]
    if current and current.get("selected_team"):
        entries.append({"at": datetime.now(timezone.utc), "team": current["selected_team"],
                        "state": current.get("state"), "current": True})
    picks: list[dict[str, Any]] = []
    previous = None
    for entry in sorted((e for e in entries if e["at"] is not None), key=lambda e: e["at"]):
        team = entry["team"]
        if team == previous:
            if entry.get("current") and picks:
                picks[-1]["current"] = True
            continue
        previous = team
        if not team:
            continue
        picks.append({"team": team, "at": entry["at"], "when": _clock(entry["at"]),
                      "engine": _ENGINE_LABELS.get(entry["state"], "Engine"),
                      "logo": _logo(identities.get(team)), "current": bool(entry.get("current"))})
    return picks


def _chart(timeline: list[dict[str, Any]], key: str, model: float | None, name: str,
           picks: list[dict[str, Any]]) -> dict[str, Any]:
    events = [e for e in timeline if e.get(key) is not None]
    if len(events) < 2:
        return {"has_data": False}
    xs = list(range(len(events)))
    series = [{"key": "off", "label": name, "values": {i: float(e[key]) for i, e in enumerate(events)}}]
    if model is not None:
        series.append({"key": "def", "label": "Football Lab", "values": {i: float(model) for i in xs},
                       "dashed": True, "markers": False})
    chart = line_chart(xs, series, fmt=".1f", label_of=lambda i: events[i]["label"],
                       label_every=max(1, len(xs) // 6))
    marks = []
    dated = [(i, e["at"]) for i, e in enumerate(events) if e["at"] is not None]
    points = chart["series"][0]["points"]
    for pick in picks:
        if not dated or not pick["logo"]:
            continue
        index = min(dated, key=lambda item: abs((item[1] - pick["at"]).total_seconds()))[0]
        point = points[index]
        marks.append({"x": point["x"], "y": point["y"], "logo": pick["logo"],
                      "title": f"{pick['engine']} confirmed {pick['team']} · {pick['when']}"})
    chart["marks"] = marks
    return chart


def build(game: dict[str, Any], market: dict[str, Any], readings: list[dict[str, Any]],
          history: list[dict[str, Any]], projection: dict[str, Any] | None,
          current_signal: dict[str, Any] | None, identities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Chart geometry plus the confirmed-pick timeline for one game."""
    picks = confirmed_picks(history, current_signal, game, identities)
    timeline: list[dict[str, Any]] = [{
        "at": None, "label": "Open",
        "spread": market.get("consensus_spread_open"), "total": market.get("consensus_total_open")}]
    for row in readings:
        moment = _parse(row["captured_at"])
        timeline.append({"at": moment, "label": _clock(moment),
                         "spread": row["spread"], "total": row["over_under"]})
    # Games tracked before line readings were stored still have the market spread the pick
    # manifest saw, so use it for any span the readings do not cover.
    first_reading = next((e["at"] for e in timeline[1:] if e["at"]), None)
    for row in history:
        moment = _parse(row.get("recorded_at"))
        if moment and row.get("market_spread") is not None and (first_reading is None or moment < first_reading):
            timeline.append({"at": moment, "label": _clock(moment),
                             "spread": row["market_spread"], "total": None})
    opened, rest = timeline[0], sorted(timeline[1:], key=lambda e: e["at"] or datetime.min.replace(tzinfo=timezone.utc))
    timeline = [opened, *rest]
    now_spread, now_total = market.get("consensus_spread"), market.get("consensus_total")
    if market.get("count") and (not rest or (rest[-1]["spread"], rest[-1]["total"]) != (now_spread, now_total)):
        fetched = max((_parse(p.get("fetched_at")) for p in market.get("providers", [])
                       if _parse(p.get("fetched_at"))), default=None)
        timeline.append({"at": fetched, "label": "Now", "spread": now_spread, "total": now_total})
    margin, total = (projection or {}).get("margin"), (projection or {}).get("total")
    model_spread = None if margin is None else -float(margin)
    model_total = None if total is None else float(total)
    return {
        "picks": picks,
        "spread_chart": _chart(timeline, "spread", model_spread, f"{game['home_team']} spread", picks),
        "total_chart": _chart(timeline, "total", model_total, "Total", picks),
    }
