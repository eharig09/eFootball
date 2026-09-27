"""Read models' weekly pregame manifest into an auditable dashboard packet."""
from __future__ import annotations

from typing import Any

from sports_aggregator.cfb.two_engine_live import (
    engine_b_rules_plain_language,
    manifest_for_games,
    record_summary,
    route_plain_language,
)


PICK_STATES = {"agreement", "engine_a_only", "engine_b_only"}


def available_weeks(repository, season: int) -> list[int]:
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT DISTINCT week FROM games
               WHERE season=? AND week IS NOT NULL ORDER BY week""",
            (int(season),),
        ).fetchall()
    return [int(row["week"]) for row in rows]


def games_for_week(repository, season: int, week: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        return [
            dict(row) for row in connection.execute(
                """SELECT * FROM games WHERE season=? AND week=?
                   ORDER BY start_date,game_id""",
                (int(season), int(week)),
            )
        ]


def default_week(repository, season: int) -> int | None:
    upcoming = repository.upcoming_games(int(season), limit=1)
    if upcoming and upcoming[0].get("week") is not None:
        return int(upcoming[0]["week"])
    weeks = available_weeks(repository, season)
    return weeks[-1] if weeks else None


def _side_spread(packet: dict[str, Any], side: str | None) -> float | None:
    spread = packet.get("market_spread")
    if spread is None or side not in {"home", "away"}:
        return None
    return float(spread) if side == "home" else -float(spread)


def _result(game: dict[str, Any], side: str | None, spread: float | None) -> str:
    if not game.get("completed") or spread is None or side not in {"home", "away"}:
        return "Pending"
    margin = float(game["home_points"]) - float(game["away_points"])
    edge = (margin if side == "home" else -margin) + spread
    return "Push" if abs(edge) < 1e-9 else "Win" if edge > 0 else "Loss"


def _total_result(game: dict[str, Any], direction: str | None, line: Any) -> str:
    if not game.get("completed") or line is None or direction not in {"over", "under"}:
        return "Pending"
    edge = float(game["home_points"]) + float(game["away_points"]) - float(line)
    if direction == "under":
        edge = -edge
    return "Push" if abs(edge) < 1e-9 else "Win" if edge > 0 else "Loss"


def _engine_row(game: dict[str, Any], packet: dict[str, Any], key: str) -> dict[str, Any]:
    leg = packet.get(key) or {}
    side = leg.get("selected_side")
    spread = _side_spread(packet, side)
    detail = (
        route_plain_language(leg.get("route"))
        if key == "engine_a"
        else engine_b_rules_plain_language(leg.get("rules"))
    )
    return {
        **game,
        "engine": "Engine A" if key == "engine_a" else "Engine B",
        "pick": leg.get("selected_team"),
        "line": spread,
        "detail": detail or leg.get("reason") or "Qualified model rule",
        "state": packet.get("state"),
        "state_label": packet.get("state_label"),
        "result": _result(game, side, spread),
        "updated_at": packet.get("frozen_at"),
    }


def _totals_row(game: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any] | None:
    totals = packet.get("totals") or {}
    regime = totals.get("regime_benchmark") or {}
    direction = totals.get("model_direction")
    line = totals.get("closing_total")
    if not regime.get("tracked") or direction not in {"over", "under"} or line is None:
        return None
    return {
        **game,
        "engine": "Totals",
        "pick": str(direction).title(),
        "line": float(line),
        "projected_total": totals.get("calibrated_projected_total"),
        "edge": totals.get("closing_edge"),
        "detail": regime.get("label") or "Tracked historical totals regime",
        "state": "qualified",
        "state_label": "TRACKED TOTAL",
        "result": _total_result(game, direction, line),
        "updated_at": packet.get("frozen_at"),
    }


def build_dashboard(repository, season: int, week: int) -> dict[str, Any]:
    games = games_for_week(repository, season, week)
    packets = manifest_for_games(repository, [int(game["game_id"]) for game in games])
    engine_a: list[dict[str, Any]] = []
    engine_b: list[dict[str, Any]] = []
    totals: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []

    for game in games:
        packet = packets.get(int(game["game_id"])) or {}
        a = packet.get("engine_a") or {}
        b = packet.get("engine_b") or {}
        total = _totals_row(game, packet)
        if a.get("qualified"):
            engine_a.append(_engine_row(game, packet, "engine_a"))
        if b.get("qualified"):
            engine_b.append(_engine_row(game, packet, "engine_b"))
        if total:
            totals.append(total)
        audit.append({
            **game,
            "state": packet.get("state") or "untracked",
            "state_label": packet.get("state_label") or "ANALYSIS PENDING",
            "engine_a_pick": a.get("selected_team") if a.get("qualified") else None,
            "engine_b_pick": b.get("selected_team") if b.get("qualified") else None,
            "totals_pick": f"{total['pick']} {total['line']:g}" if total else None,
            "engine_a_reason": a.get("reason") or (
                "Inputs ready; no Engine A route matched."
                if packet else "Waiting for scheduled analysis"
            ),
            "engine_b_reason": b.get("reason") or (
                "Inputs ready; no Engine B rule matched."
                if packet else "Waiting for scheduled analysis"
            ),
            "updated_at": packet.get("frozen_at"),
        })

    return {
        "games": games,
        "engine_a": engine_a,
        "engine_b": engine_b,
        "totals": totals,
        "audit": audit,
        "counts": {
            "games": len(games),
            "analyzed": len(packets),
            "pending": sum(row["state"] in {"pending", "untracked"} for row in audit),
            "no_signal": sum(row["state"] == "none" for row in audit),
            "engine_a": len(engine_a),
            "engine_b": len(engine_b),
            "totals": len(totals),
            "conflicts": sum(row["state"] == "conflict" for row in audit),
        },
        "week_record": record_summary(repository, season, week=week),
        "season_record": record_summary(repository, season),
    }


def filter_dashboard(
    dashboard: dict[str, Any], *, query: str = "", status: str = "all"
) -> dict[str, Any]:
    needle = query.strip().casefold()

    def matches(row: dict[str, Any]) -> bool:
        if needle and needle not in (
            f"{row.get('away_team', '')} {row.get('home_team', '')} "
            f"{row.get('pick', '')}"
        ).casefold():
            return False
        state = str(row.get("state") or "")
        if status == "picks":
            return state in PICK_STATES or state == "qualified"
        if status == "pending":
            return state in {"pending", "untracked"}
        if status == "no_pick":
            return state in {"none", "conflict"}
        return True

    result = dict(dashboard)
    for key in ("engine_a", "engine_b", "totals", "audit"):
        result[key] = [row for row in dashboard[key] if matches(row)]
    return result
