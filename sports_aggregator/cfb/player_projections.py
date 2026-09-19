"""Leak-safe allocation of team matchup projections to current players.

This first player layer allocates only quantities supported by stored box-score
usage.  The source has receptions but not targets/routes, so it intentionally
does not manufacture target projections.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.cfb.external import initialize as initialize_external
from sports_aggregator.cfb.game_projection import _decay_weights
from sports_aggregator.cfb.models import normalize_alias
from sports_aggregator.cfb.xdrives import RECENCY_LAMBDA, TRAILING_WINDOW_GAMES


def _passing(value: Any) -> tuple[float | None, float | None]:
    try:
        completed, attempts = str(value or "").split("/", 1)
        return float(completed), float(attempts)
    except (TypeError, ValueError):
        return None, None


def _availability_factor(status: str | None) -> float:
    value = str(status or "").strip().casefold()
    if value in {"out", "inactive", "suspended"}: return 0.0
    if value in {"doubtful"}: return 0.25
    if value in {"questionable", "game time decision", "game-time decision"}: return 0.75
    return 1.0


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def project_team_players(repository, team: str, season: int, *, before_date: str,
                         team_projection: dict[str, Any],
                         window: int = TRAILING_WINDOW_GAMES) -> dict[str, Any]:
    """Allocate one team's projected volume/yardage using strictly prior games."""
    repository.initialize(); initialize_external(repository)
    with repository._reader() as connection:
        games = [dict(row) for row in connection.execute("""
          SELECT DISTINCT g.game_id,g.start_date FROM games g
          JOIN game_player_box_stats b USING(game_id)
          WHERE g.season=? AND g.completed=1 AND b.team=? AND g.start_date<?
          ORDER BY g.start_date DESC,g.game_id DESC LIMIT ?
        """, (int(season), team, before_date, int(window)))]
        games.reverse()
        if not games:
            return {"team": team, "season": season, "games": 0, "players": [],
                    "unallocated": {}, "target_projection_available": False}
        game_ids = [int(row["game_id"]) for row in games]
        marks = ",".join("?" for _ in game_ids)
        stats = [dict(row) for row in connection.execute(f"""
          SELECT game_id,player_id,player,category,stat_type,stat_value,numeric_value
          FROM game_player_box_stats WHERE team=? AND game_id IN ({marks})
            AND category IN ('passing','rushing','receiving')
          ORDER BY game_id,player_id
        """, (team, *game_ids))]
        roster = [dict(row) for row in connection.execute(
            "SELECT player_id,first_name,last_name,normalized_name,position FROM players WHERE season=? AND team=?",
            (int(season), team))]
        availability = {}
        for row in connection.execute("""SELECT a.* FROM player_availability a
          JOIN (SELECT player_id,MAX(reported_at) reported_at FROM player_availability
                WHERE season=? AND team=? AND (reported_at IS NULL OR reported_at<?)
                GROUP BY player_id) latest
          ON latest.player_id=a.player_id AND (latest.reported_at=a.reported_at
             OR (latest.reported_at IS NULL AND a.reported_at IS NULL))
          WHERE a.season=? AND a.team=?""",
          (int(season), team, before_date, int(season), team)):
            if row["player_id"]:
                availability[str(row["player_id"])] = dict(row)

    roster_ids = {str(row["player_id"]): row for row in roster}
    roster_names = {str(row["normalized_name"]): row for row in roster}
    weights = dict(zip(game_ids, _decay_weights(len(game_ids), RECENCY_LAMBDA)))
    players: dict[str, dict[str, Any]] = {}
    for row in stats:
        player_id = str(row["player_id"]); name = str(row["player"])
        roster_row = roster_ids.get(player_id) or roster_names.get(normalize_alias(name))
        if roster and roster_row is None:
            continue
        item = players.setdefault(player_id, {
            "player_id": player_id, "player": name,
            "position": (roster_row or {}).get("position"),
            "weighted": defaultdict(float), "games": set(),
        })
        weight = weights[int(row["game_id"])]
        item["games"].add(int(row["game_id"]))
        category, stat_type = str(row["category"]), str(row["stat_type"]).upper()
        if category == "passing" and stat_type == "C/ATT":
            completions, attempts = _passing(row["stat_value"])
            if completions is not None: item["weighted"]["pass_completions"] += weight * completions
            if attempts is not None: item["weighted"]["pass_attempts"] += weight * attempts
        elif row["numeric_value"] is not None:
            key = {
                ("passing", "YDS"): "pass_yards", ("passing", "TD"): "pass_tds",
                ("rushing", "CAR"): "rush_attempts", ("rushing", "YDS"): "rush_yards",
                ("rushing", "TD"): "rush_tds", ("receiving", "REC"): "receptions",
                ("receiving", "YDS"): "receiving_yards", ("receiving", "TD"): "receiving_tds",
            }.get((category, stat_type))
            if key: item["weighted"][key] += weight * float(row["numeric_value"])

    totals = defaultdict(float)
    for item in players.values():
        for key, value in item["weighted"].items(): totals[key] += value
    attempts, completions = totals["pass_attempts"], totals["pass_completions"]
    completion_rate = completions / attempts if attempts else None
    expected_completions = (float(team_projection["dropbacks"]) * completion_rate
                            if team_projection.get("dropbacks") is not None and completion_rate is not None else None)
    scoring_tds = totals["receiving_tds"] + totals["rush_tds"]
    pass_td_share = totals["receiving_tds"] / scoring_tds if scoring_tds else None
    expected_tds = team_projection.get("expected_touchdowns")
    expected_pass_tds = (float(expected_tds) * pass_td_share
                         if expected_tds is not None and pass_td_share is not None else None)
    expected_rush_tds = (float(expected_tds) - expected_pass_tds
                         if expected_tds is not None and expected_pass_tds is not None else None)

    def allocate(total_key: str, player_key: str, projected: float | None, factor: float) -> float | None:
        denominator = totals[total_key]
        return (float(projected) * players[player_id]["weighted"][player_key] / denominator * factor
                if projected is not None and denominator else None)

    output = []
    for player_id, item in players.items():
        report = availability.get(player_id) or {}
        factor = _availability_factor(report.get("status"))
        row = {
            "player_id": player_id, "player": item["player"], "position": item["position"],
            "games": len(item["games"]), "availability": report.get("status"),
            "availability_factor": factor,
            "expected_dropbacks": allocate("pass_attempts", "pass_attempts", team_projection.get("dropbacks"), factor),
            "expected_pass_yards": allocate("pass_yards", "pass_yards", team_projection.get("pass_yards"), factor),
            "expected_pass_tds": allocate("pass_tds", "pass_tds", expected_pass_tds, factor),
            "expected_rush_attempts": allocate("rush_attempts", "rush_attempts", team_projection.get("rush_attempts"), factor),
            "expected_rush_yards": allocate("rush_yards", "rush_yards", team_projection.get("rush_yards"), factor),
            "expected_rush_tds": allocate("rush_tds", "rush_tds", expected_rush_tds, factor),
            "expected_receptions": allocate("receptions", "receptions", expected_completions, factor),
            "expected_receiving_yards": allocate("receiving_yards", "receiving_yards", team_projection.get("pass_yards"), factor),
            "expected_receiving_tds": allocate("receiving_tds", "receiving_tds", expected_pass_tds, factor),
        }
        row["expected_opportunities"] = sum(float(row[key] or 0.0) for key in (
            "expected_dropbacks", "expected_rush_attempts", "expected_receptions"))
        output.append({key: _round(value, 2) if key.startswith("expected_") and key != "expected_opportunities" else value
                       for key, value in row.items()})
    output.sort(key=lambda row: (-float(row["expected_opportunities"]), row["player"]))

    projected_totals = {
        "dropbacks": team_projection.get("dropbacks"), "pass_yards": team_projection.get("pass_yards"),
        "rush_attempts": team_projection.get("rush_attempts"), "rush_yards": team_projection.get("rush_yards"),
        "receptions": expected_completions, "pass_tds": expected_pass_tds, "rush_tds": expected_rush_tds,
    }
    field_map = {
        "dropbacks": "expected_dropbacks", "pass_yards": "expected_pass_yards",
        "rush_attempts": "expected_rush_attempts", "rush_yards": "expected_rush_yards",
        "receptions": "expected_receptions", "pass_tds": "expected_pass_tds",
        "rush_tds": "expected_rush_tds",
    }
    unallocated = {}
    for key, projected in projected_totals.items():
        allocated = sum(float(row.get(field_map[key]) or 0.0) for row in output)
        unallocated[key] = _round(max(0.0, float(projected) - allocated), 2) if projected is not None else None
    return {"team": team, "season": season, "games": len(games), "players": output,
            "unallocated": unallocated, "completion_rate": _round(completion_rate, 3),
            "target_projection_available": False,
            "note": "Receptions are projected from completion share; targets/routes are not available in the source."}


def project_matchup_players(repository, projection: dict[str, Any], *, season: int,
                            before_date: str) -> dict[str, Any]:
    return {
        "away": project_team_players(repository, projection["away_team"], season,
                                     before_date=before_date, team_projection=projection["away"]),
        "home": project_team_players(repository, projection["home_team"], season,
                                     before_date=before_date, team_projection=projection["home"]),
    }
