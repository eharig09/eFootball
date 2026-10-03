"""Presentation policy for Football Lab's weekly NFL picks board."""

from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.live_projection import report as live_projection_report
from sports_aggregator.nfl.perception_challenger import build_rows as perception_rows
from sports_aggregator.nfl.repository import NFLRepository


def available_weeks(repository: NFLRepository, season: int) -> list[int]:
    return sorted({int(game["week"]) for game in repository.schedule(season)})


def default_week(repository: NFLRepository, season: int) -> int | None:
    games = repository.schedule(season)
    lined = [game for game in games if not game["completed"] and game.get("spread_line") is not None]
    if lined:
        return min(int(game["week"]) for game in lined)
    upcoming = [game for game in games if not game["completed"]]
    if upcoming:
        return min(int(game["week"]) for game in upcoming)
    return max((int(game["week"]) for game in games), default=None)


def _spread_label(team: str, line: float) -> str:
    return f"{team} {'+' if line > 0 else ''}{line:g}" if line else f"{team} PK"


def _separation(edge: float | None, scale: float | None) -> tuple[str, float | None]:
    if edge is None or not scale:
        return "unrated", None
    strength = abs(float(edge)) / float(scale)
    if strength >= 0.75:
        return "high", strength
    if strength >= 0.40:
        return "medium", strength
    return "low", strength


def _line_move(game: dict[str, Any], stored: dict[str, Any] | None) -> dict[str, Any] | None:
    """ESPN opener-to-now movement for the pick card, on the away-team line."""
    if not stored or stored.get("open_spread") is None or stored.get("current_spread") is None:
        return None

    def spread(value: float) -> str:
        return "PK" if value == 0 else f"{value:+g}"

    parts = [f"{game['away_team']} {spread(stored['open_spread'])} → {spread(stored['current_spread'])}"]
    if stored.get("open_total") is not None and stored.get("current_total") is not None:
        parts.append(f"total {stored['open_total']:g} → {stored['current_total']:g}")
    return {"text": " · ".join(parts),
            "spread": round(stored["current_spread"] - stored["open_spread"], 2),
            "total": (round(stored["current_total"] - stored["open_total"], 2)
                      if stored.get("open_total") is not None and stored.get("current_total") is not None
                      else None)}


def build_dashboard(repository: NFLRepository, season: int, week: int) -> dict[str, Any]:
    packet = live_projection_report(repository, season=int(season), week=int(week))
    narratives = {}
    if isinstance(repository, NFLRepository):
        narratives = {
            str(item["game_id"]): item.get("tags") or []
            for item in perception_rows(repository, int(season), int(season))
            if int(item["week"]) == int(week)
        }
    stored_lines = repository.espn_market_week(int(season), int(week)) if isinstance(repository, NFLRepository) else {}
    games = []
    for raw in packet.get("games", []):
        row = dict(raw)
        row["line_move"] = _line_move(row, stored_lines.get(str(row.get("game_id"))))
        row["narrative_tags"] = narratives.get(str(row.get("game_id")), [])
        model = row.get("football_lab")
        market = row.get("market_anchor")
        disagreement = row.get("disagreement") or {}
        scale = row.get("historical_uncertainty_scale") or {}
        if not model:
            row.update({"straight_up_pick": None, "ats_pick": None, "total_pick": None,
                        "ats_separation": "unrated", "total_separation": "unrated"})
            games.append(row)
            continue

        model_margin = float(model["margin"])
        winner = row["home_team"] if model_margin > 0 else row["away_team"]
        ats_pick = total_pick = None
        if market:
            margin_edge = float(disagreement.get("margin") or 0.0)
            ats_team = row["home_team"] if margin_edge > 0 else row["away_team"]
            ats_line = -float(market["margin"]) if ats_team == row["home_team"] else float(market["margin"])
            ats_pick = _spread_label(ats_team, ats_line)
            total_edge = float(disagreement.get("total") or 0.0)
            total_pick = f"{'Over' if total_edge > 0 else 'Under'} {float(market['total']):g}"
        ats_separation, ats_strength = _separation(
            disagreement.get("margin"), scale.get("margin_residual_scale"))
        total_separation, total_strength = _separation(
            disagreement.get("total"), scale.get("total_residual_scale"))
        row.update({
            "straight_up_pick": winner,
            "ats_pick": ats_pick,
            "total_pick": total_pick,
            "ats_separation": ats_separation,
            "total_separation": total_separation,
            "ats_strength": ats_strength,
            "total_strength": total_strength,
        })
        games.append(row)

    games.sort(key=lambda row: (row.get("game_date") or "", row.get("game_id") or ""))
    return {
        **packet,
        "games": games,
        "counts": {
            "games": len(games),
            "projected": sum(bool(game.get("football_lab")) for game in games),
            "lined": sum(bool(game.get("market_anchor")) for game in games),
            "high_separation": sum(
                game.get("ats_separation") == "high" or game.get("total_separation") == "high"
                for game in games
            ),
        },
        "label_policy": {
            "status": "experimental_forecast",
            "separation_is_confidence": False,
            "note": "Separation measures model-market distance, not a calibrated win probability or validated betting edge.",
        },
    }
