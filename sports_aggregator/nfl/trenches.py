"""Offensive-line versus defensive-front matchup context."""
from __future__ import annotations
from typing import Any
from sports_aggregator.nfl.pff import NFLPFFService
from sports_aggregator.nfl.repository import NFLRepository

FRONT = {"EDGE", "DE", "DT", "DL", "NT", "LB", "OLB"}

def _weighted(rows: list[dict[str, Any]], metric: str, weight: str) -> float | None:
    sample = [(row.get(metric), row.get(weight)) for row in rows
              if row.get(metric) is not None and (row.get(weight) or 0) > 0]
    total = sum(item[1] for item in sample)
    return sum(item[0] * item[1] for item in sample) / total if total else None

def trench_matchups(repository: NFLRepository, pff: NFLPFFService, game: dict[str, Any],
                    roster_season: int, pff_season: int, stats_season: int,
                    profiles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    cards = []
    for offense, defense in ((game["away_team"], game["home_team"]),
                             (game["home_team"], game["away_team"])):
        current_offense = {row["player_id"] for row in repository.team_roster(roster_season, offense)}
        current_front = {row["player_id"] for row in repository.team_roster(roster_season, defense)
                         if (row.get("position") or "").upper() in FRONT}
        blockers = [row for row in pff.family_profiles(
            pff_season, "offense_blocking", offense,
            ("grades_pass_block", "grades_run_block", "pbe", "pressures_allowed",
             "snap_counts_pass_block", "snap_counts_run_block"),
        ) if row.get("gsis_id") in current_offense
             and ((row.get("snap_counts_pass_block") or 0) + (row.get("snap_counts_run_block") or 0)) >= 100]
        hit_rows = repository.player_leaders(stats_season, "def_qb_hits", team=defense, limit=100)
        sacks = {row["player_id"]: row["value"] for row in repository.player_leaders(
            stats_season, "def_sacks", team=defense, limit=100)}
        front = [{**row, "qb_hits": row["value"], "sacks": sacks.get(row["player_id"])}
                 for row in hit_rows if row["player_id"] in current_front]
        front.sort(key=lambda row: (-(row.get("qb_hits") or 0), -(row.get("sacks") or 0)))
        cards.append({
            "offense": offense, "defense": defense, "season": pff_season,
            "pass_block_grade": _weighted(blockers, "grades_pass_block", "snap_counts_pass_block"),
            "run_block_grade": _weighted(blockers, "grades_run_block", "snap_counts_run_block"),
            "pbe": _weighted(blockers, "pbe", "snap_counts_pass_block"),
            "pressures_allowed": sum(row.get("pressures_allowed") or 0 for row in blockers),
            "pass_block_snaps": sum(row.get("snap_counts_pass_block") or 0 for row in blockers),
            "blockers": sorted(blockers, key=lambda row: -(row.get("snap_counts_pass_block") or 0))[:5],
            "rushers": front[:5], "rush_season": stats_season,
                "defensive_rush_epa_allowed": (profiles.get(defense) or {}).get("defensive_rush_epa_allowed"),
            "note": ("PFF supplies the blocking side. Front pressure production comes from "
                     "nflverse box scores because no selected pass-rush PFF export is present."),
        })
    return cards
