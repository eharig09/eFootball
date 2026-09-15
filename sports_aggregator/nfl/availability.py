"""Player availability packets shared by team depth and matchup pages."""

from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.naming import normalize_name
from sports_aggregator.nfl.repository import NFLRepository


SEVERITY = {"O": 5, "OUT": 5, "IR": 5, "PUP": 5, "SUSP": 5,
            "D": 4, "DOUBTFUL": 4, "Q": 3, "QUESTIONABLE": 3,
            "L": 2, "LIMITED": 2, "PROBABLE": 1, "P": 1}


def _clean_detail(row: dict[str, Any]) -> str:
    anatomy = " ".join(value for value in (row.get("side"), row.get("injury_type")) if value)
    parts = [anatomy or row.get("location"), row.get("detail"), row.get("practice_status")]
    return " · ".join(str(value) for value in parts if value)


def availability_packet(repository: NFLRepository, season: int, team: str) -> dict[str, Any]:
    injuries = repository.team_injuries(season, team)
    depth_rows = repository.current_depth_chart(season, team)
    depth_by_gsis = {row["gsis_id"]: row for row in depth_rows if row.get("gsis_id")}
    depth_by_espn = {str(row["espn_id"]): row for row in depth_rows if row.get("espn_id")}
    depth_by_name = {normalize_name(row["player_name"]): row for row in depth_rows}
    snap_season = season if repository.team_snap_leaders(season, team) else season - 1
    snaps = {row.get("player_id"): row for row in repository.team_snap_leaders(snap_season, team)
             if row.get("player_id")}
    rows = []
    for injury in injuries:
        depth = (depth_by_gsis.get(injury.get("gsis_id")) or
                 depth_by_espn.get(str(injury.get("espn_id"))) or
                 depth_by_name.get(normalize_name(injury.get("player_name")))) or {}
        snap = snaps.get(injury.get("gsis_id"), {})
        participation = max(snap.get("offense_pct") or 0, snap.get("defense_pct") or 0,
                            snap.get("st_pct") or 0) or None
        designation = str(injury.get("designation") or injury.get("status") or "Unknown").upper()
        severity = SEVERITY.get(designation, SEVERITY.get(str(injury.get("status") or "").upper(), 2))
        rows.append({**injury, "designation_label": injury.get("status") or designation,
                     "designation_class": "out" if severity >= 5 else
                     ("doubtful" if severity == 4 else "questionable"),
                     "severity": severity, "depth_rank": depth.get("position_rank"),
                     "depth_slot": depth.get("position_abbreviation"),
                     "snap_participation": participation,
                     "impact": "Starter" if depth.get("position_rank") == 1 else
                     ("High-use" if (participation or 0) >= .5 else "Depth"),
                     "detail_line": _clean_detail(injury),
                     "player_url": (f"/nfl/players/{injury['gsis_id']}/?season={season}"
                                    if injury.get("gsis_id") else None)})
    rows.sort(key=lambda row: (-row["severity"], 0 if row["impact"] == "Starter" else 1,
                               -(row.get("snap_participation") or 0), row["player_name"]))
    return {"team": team, "season": season, "rows": rows,
            "unavailable": sum(row["severity"] >= 5 for row in rows),
            "questionable": sum(row["severity"] == 3 for row in rows),
            "as_of": max((row.get("fetched_at") or "" for row in rows), default=None),
            "source_url": rows[0].get("source_url") if rows else None}
