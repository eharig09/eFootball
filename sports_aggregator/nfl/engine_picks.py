"""Presentation policy for Football Lab's weekly NFL picks board."""

from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.live_projection import report as live_projection_report
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


def _confidence(edge: float | None, scale: float | None) -> tuple[str, float | None]:
    if edge is None or not scale:
        return "unrated", None
    strength = abs(float(edge)) / float(scale)
    if strength >= 0.75:
        return "high", strength
    if strength >= 0.40:
        return "medium", strength
    return "low", strength


def build_dashboard(repository: NFLRepository, season: int, week: int) -> dict[str, Any]:
    packet = live_projection_report(repository, season=int(season), week=int(week))
    games = []
    for raw in packet.get("games", []):
        row = dict(raw)
        model = row.get("football_lab")
        market = row.get("market_anchor")
        disagreement = row.get("disagreement") or {}
        scale = row.get("historical_uncertainty_scale") or {}
        if not model:
            row.update({"straight_up_pick": None, "ats_pick": None, "total_pick": None,
                        "ats_confidence": "unrated", "total_confidence": "unrated"})
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
        ats_confidence, ats_strength = _confidence(
            disagreement.get("margin"), scale.get("margin_residual_scale"))
        total_confidence, total_strength = _confidence(
            disagreement.get("total"), scale.get("total_residual_scale"))
        row.update({
            "straight_up_pick": winner,
            "ats_pick": ats_pick,
            "total_pick": total_pick,
            "ats_confidence": ats_confidence,
            "total_confidence": total_confidence,
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
            "high_confidence": sum(
                game.get("ats_confidence") == "high" or game.get("total_confidence") == "high"
                for game in games
            ),
        },
    }
