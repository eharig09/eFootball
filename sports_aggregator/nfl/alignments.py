"""Evidence-backed, non-assigning PFF/PBP matchup interactions."""

from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.pff import NFLPFFService
from sports_aggregator.nfl.repository import NFLRepository


def alignment_matchups(repository: NFLRepository, pff: NFLPFFService,
                       game: dict[str, Any], roster_season: int,
                       pff_season: int, defense_profiles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    cards = []
    for offense, defense in ((game["away_team"], game["home_team"]),
                             (game["home_team"], game["away_team"])):
        offense_ids = {row["player_id"] for row in repository.team_roster(roster_season, offense)}
        defense_ids = {row["player_id"] for row in repository.team_roster(roster_season, defense)}
        usage = {row["player_id"]: row for row in repository.team_player_usage(pff_season, offense)}
        receivers = [row for row in pff.family_profiles(
            pff_season, "receiving_summary", offense,
            ("routes", "grades_pass_route", "yprr", "avg_depth_of_target",
             "slot_rate", "wide_rate"),
        ) if row.get("gsis_id") in offense_ids and (row.get("routes") or 0) >= 100]
        receivers.sort(key=lambda row: (
            -(usage.get(row.get("gsis_id"), {}).get("targets") or 0),
            -(row.get("routes") or 0),
        ))
        slot_defenders = [row for row in pff.family_profiles(
            pff_season, "slot_coverage", defense,
            ("coverage_snaps", "qb_rating_against", "yards_per_coverage_snap"),
        ) if row.get("gsis_id") in defense_ids and (row.get("coverage_snaps") or 0) >= 40]
        slot_defenders.sort(key=lambda row: -(row.get("coverage_snaps") or 0))
        allowed_zones = [zone for zone in defense_profiles.get(defense, {}).get("zones", [])
                         if (zone.get("attempts") or 0) >= 20]
        vulnerable = max(allowed_zones, key=lambda row: row.get("epa_per_attempt") or -99,
                         default=None)
        for receiver in receivers[:2]:
            role = usage.get(receiver.get("gsis_id"), {})
            slot_rate = receiver.get("slot_rate") or 0
            defender = slot_defenders[0] if slot_rate >= 50 and slot_defenders else None
            alignment = "slot" if slot_rate >= 50 else "wide"
            receiver_profile = repository.receiver_pass_profile(
                pff_season, receiver.get("gsis_id") or "",
            )
            receiver_zones = [zone for zone in receiver_profile.get("zones", [])
                              if (zone.get("targets") or 0) >= 3]
            receiver_zones.sort(key=lambda row: (
                -(row.get("targets") or 0), -(row.get("epa_per_target") or -99),
            ))
            preferred = max(receiver_zones, key=lambda row: (
                row.get("targets") or 0, row.get("epa_per_target") or -99,
            ), default=None)
            exact_allowed = None
            if preferred:
                exact_allowed = next((
                    zone for zone in defense_profiles.get(defense, {}).get("zones", [])
                    if zone.get("depth_bucket") == preferred.get("depth_bucket")
                    and zone.get("pass_location") == preferred.get("pass_location")
                ), None)
            total_targets = receiver_profile.get("total", {}).get("targets") or 0
            zone_share = ((preferred.get("targets") or 0) / total_targets
                          if preferred and total_targets else None)
            zone_matchups = []
            for zone in receiver_zones[:3]:
                allowed = next((item for item in defense_profiles.get(defense, {}).get("zones", [])
                                if item.get("depth_bucket") == zone.get("depth_bucket")
                                and item.get("pass_location") == zone.get("pass_location")), None)
                zone_matchups.append({
                    "depth_bucket": zone["depth_bucket"], "pass_location": zone["pass_location"],
                    "targets": zone.get("targets"), "receptions": zone.get("receptions"),
                    "receiving_yards": zone.get("receiving_yards"),
                    "offense_epa": zone.get("epa_per_target"),
                    "target_share": ((zone.get("targets") or 0) / total_targets if total_targets else None),
                    "defense_attempts": allowed.get("attempts") if allowed else None,
                    "defense_epa": allowed.get("epa_per_attempt") if allowed else None,
                    "defense_completion_rate": allowed.get("completion_rate") if allowed else None,
                })
            if preferred and exact_allowed:
                interaction = (
                    f"{preferred['depth_bucket'].title()} {preferred['pass_location']} is "
                    f"{receiver['player_name']}’s highest-volume stored target area and the "
                    f"comparison below is the defense’s result allowed in that exact zone."
                )
            elif preferred:
                interaction = "The receiver’s preferred target zone is measured, but the defense lacks a qualifying result in that exact cell."
            else:
                interaction = "Alignment and route volume are available; receiver-level target-location data is not yet available."
            cards.append({
                "offense": offense, "defense": defense, "player_name": receiver["player_name"],
                "player_id": receiver.get("gsis_id"), "alignment": alignment,
                "alignment_rate": slot_rate if alignment == "slot" else receiver.get("wide_rate"),
                "routes": receiver.get("routes"), "route_grade": receiver.get("grades_pass_route"),
                "yprr": receiver.get("yprr"), "target_share": role.get("target_share"),
                "defender": defender, "vulnerable_zone": vulnerable,
                "preferred_zone": preferred, "exact_allowed_zone": exact_allowed,
                "preferred_zone_share": zone_share, "interaction": interaction,
                "zone_matchups": zone_matchups,
                "confidence": "High-volume interaction" if (receiver.get("routes") or 0) >= 350 else "Rotation interaction",
                "season": pff_season,
            })
    return cards


def rushing_matchups(repository: NFLRepository, pff: NFLPFFService,
                     game: dict[str, Any], roster_season: int, pff_season: int,
                     profiles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Connect likely ball carriers to the opposing run-defense result."""
    output = []
    for offense, defense in ((game["away_team"], game["home_team"]),
                             (game["home_team"], game["away_team"])):
        current_ids = {row["player_id"] for row in repository.team_roster(roster_season, offense)}
        usage_rows = repository.team_player_usage(pff_season, offense)
        usage = {row["player_id"]: row for row in usage_rows if row["player_id"] in current_ids}
        runners = [row for row in pff.family_profiles(
            pff_season, "rushing_summary", offense,
            ("attempts", "grades_run", "elusive_rating", "yco_attempt", "avoided_tackles"),
        ) if row.get("gsis_id") in current_ids]
        runners.sort(key=lambda row: (-(usage.get(row.get("gsis_id"), {}).get("carries") or 0),
                                      -(row.get("attempts") or 0)))
        defense_profile = profiles.get(defense) or {}
        for runner in runners[:3]:
            role = usage.get(runner.get("gsis_id"), {})
            output.append({
                "offense": offense, "defense": defense, "player_id": runner.get("gsis_id"),
                "player_name": runner.get("player_name"), "carries": role.get("carries"),
                "carry_share": role.get("carry_share"), "rushing_yards": role.get("rushing_yards"),
                "run_grade": runner.get("grades_run"), "elusive_rating": runner.get("elusive_rating"),
                "yards_after_contact": runner.get("yco_attempt"),
                "avoided_tackles": runner.get("avoided_tackles"),
                "defense_rush_epa_allowed": defense_profile.get("defensive_rush_epa_allowed"),
                "defense_success_allowed": defense_profile.get("defensive_success_allowed"),
                "season": pff_season,
            })
    return output
