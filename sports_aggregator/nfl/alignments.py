"""Evidence-backed, non-assigning PFF/PBP matchup interactions."""

from __future__ import annotations

from statistics import median
from typing import Any

from sports_aggregator.nfl.explorer import with_rates
from sports_aggregator.nfl.pff import NFLPFFService, season_scaled_minimum
from sports_aggregator.nfl.ranking import rank_lookup
from sports_aggregator.nfl.repository import NFLRepository


def ngs_season_lookup(repository: NFLRepository, season: int, team: str,
                       metrics: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Season-average Next Gen Stats for one team's roster, keyed by player.

    `with_rates` divides each metric's weighted-sum companion back down by
    its own volume metric (see explorer.py) -- safe to call here even though
    this query only asks for a handful of the columns it knows how to derive,
    since every division it does not have inputs for just comes back None.
    """
    rows = repository.player_season_stats(season, metrics, team=team)
    return {row["player_id"]: with_rates(row) for row in rows}


def _ngs_league_ranks(repository: NFLRepository, season: int, positions: tuple[str, ...],
                      query_metrics: tuple[str, ...],
                      rank_metrics: tuple[str, ...]) -> dict[str, dict[str, dict[str, int]]]:
    """Where each candidate's NGS numbers stand against every qualifying
    player at the same positions leaguewide, not just this game's two
    rosters -- pooled across `positions` (e.g. WR+TE) into one leaderboard
    rather than ranked position-by-position, since alignment/rushing cards
    don't distinguish those sub-positions from each other."""
    rows: list[dict[str, Any]] = []
    for position in positions:
        rows.extend(with_rates(row) for row in
                    repository.player_season_stats(season, query_metrics, position=position))
    return rank_lookup(rows, id_key="player_id", metrics=rank_metrics)


def _weak_zones(zones: list[dict[str, Any]], minimum_attempts: float) -> set[tuple[str, str]]:
    """Zones a defense allows more value in than its own other zones.

    Self-relative on purpose: early in a season there are only a handful of
    qualifying zones, not enough for an honest league-wide baseline, but
    "worse than this defense's own median zone" only needs the defense's own
    sample. Fewer than three qualifying zones isn't enough to call any of
    them relatively weak, so none are flagged rather than guessing.
    """
    qualifying = [zone for zone in zones if (zone.get("attempts") or 0) >= minimum_attempts
                  and zone.get("epa_per_attempt") is not None]
    if len(qualifying) < 3:
        return set()
    baseline = median(zone["epa_per_attempt"] for zone in qualifying)
    return {(zone["depth_bucket"], zone["pass_location"]) for zone in qualifying
            if zone["epa_per_attempt"] > baseline}


def passer_ngs_leaders(repository: NFLRepository, game: dict[str, Any],
                       pff_season: int) -> dict[str, dict[str, Any] | None]:
    """Each team's most-used passer's season Next Gen Stats, ranked against
    every qualifying QB leaguewide -- the passing counterpart to the
    receiver/runner NGS already carried on the alignment/rushing cards."""
    query_metrics = ("attempts", "ngs_pass_cpoe_wtd", "ngs_pass_time_to_throw_wtd",
                     "ngs_pass_aggressiveness_wtd")
    rank_metrics = ("ngs_pass_cpoe", "ngs_pass_time_to_throw", "ngs_pass_aggressiveness")
    league_ranks = rank_lookup(
        (with_rates(row) for row in repository.player_season_stats(
            pff_season, query_metrics, position="QB")),
        id_key="player_id", metrics=rank_metrics,
    )
    output: dict[str, dict[str, Any] | None] = {}
    for team in (game["away_team"], game["home_team"]):
        rows = [with_rates(row) for row in repository.player_season_stats(
            pff_season, query_metrics, team=team, position="QB")]
        leader = max(rows, key=lambda row: row.get("attempts") or 0, default=None)
        if leader is None:
            output[team] = None
            continue
        player_id = leader["player_id"]
        output[team] = {
            "player_id": player_id, "player_name": leader.get("player_name"),
            "attempts": leader.get("attempts"),
            "ngs_cpoe": leader.get("ngs_pass_cpoe"),
            "ngs_cpoe_rank": league_ranks["ngs_pass_cpoe"].get(player_id),
            "ngs_time_to_throw": leader.get("ngs_pass_time_to_throw"),
            "ngs_time_to_throw_rank": league_ranks["ngs_pass_time_to_throw"].get(player_id),
            "ngs_aggressiveness": leader.get("ngs_pass_aggressiveness"),
            "ngs_aggressiveness_rank": league_ranks["ngs_pass_aggressiveness"].get(player_id),
        }
    return output


def alignment_matchups(repository: NFLRepository, pff: NFLPFFService,
                       game: dict[str, Any], roster_season: int,
                       pff_season: int, defense_profiles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    # Full-season sample floors (100 routes, 40 slot-coverage snaps, 20 zone
    # attempts) exclude every player/zone early in a season; scale them to
    # what's actually synced. Receiver zones and PFF roles come from
    # `pff_season` (which often lags a year behind, so it can already be a
    # full season deep); defense zones come from whatever season the caller
    # actually built `defense_profiles` against -- usually the live current
    # season, which is why that floor is scaled separately per defense below
    # rather than off `pff_season`'s own week count.
    weeks_played = repository.latest_stat_week(pff_season)
    minimum_routes = season_scaled_minimum(100, weeks_played)
    minimum_slot_snaps = season_scaled_minimum(40, weeks_played)
    minimum_zone_targets = max(2, round(season_scaled_minimum(3, weeks_played)))
    # Leaguewide, not per-team -- computed once since it doesn't depend on
    # which offense/defense pairing is being built below.
    receiving_ngs_ranks = _ngs_league_ranks(
        repository, pff_season, ("WR", "TE"),
        ("targets", "ngs_rec_separation_wtd", "ngs_rec_cushion_wtd"),
        ("ngs_rec_separation", "ngs_rec_cushion"),
    )
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
        ) if row.get("gsis_id") in offense_ids and (row.get("routes") or 0) >= minimum_routes]
        receivers.sort(key=lambda row: (
            -(usage.get(row.get("gsis_id"), {}).get("targets") or 0),
            -(row.get("routes") or 0),
        ))
        receiving_ngs = ngs_season_lookup(
            repository, pff_season, offense,
            ("targets", "ngs_rec_separation_wtd", "ngs_rec_cushion_wtd"),
        )
        slot_defenders = [row for row in pff.family_profiles(
            pff_season, "slot_coverage", defense,
            ("coverage_snaps", "qb_rating_against", "yards_per_coverage_snap"),
        ) if row.get("gsis_id") in defense_ids and (row.get("coverage_snaps") or 0) >= minimum_slot_snaps]
        slot_defenders.sort(key=lambda row: -(row.get("coverage_snaps") or 0))
        defense_zones = defense_profiles.get(defense, {}).get("zones", [])
        defense_zone_season = defense_profiles.get(defense, {}).get("season") or pff_season
        defense_weeks_played = repository.latest_stat_week(defense_zone_season)
        minimum_zone_attempts = season_scaled_minimum(20, defense_weeks_played)
        allowed_zones = [zone for zone in defense_zones if (zone.get("attempts") or 0) >= minimum_zone_attempts]
        vulnerable = max(allowed_zones, key=lambda row: row.get("epa_per_attempt") or -99,
                         default=None)
        weak_zones = _weak_zones(defense_zones, minimum_zone_attempts)
        # Widen the candidate pool beyond the two most-used receivers so a
        # receiver whose routes concentrate in this defense's weak zones can
        # outrank a higher-volume teammate who doesn't, then cut back down --
        # selection stays grounded in real usage, ranking reflects matchup fit.
        candidates = []
        for receiver in receivers[:4]:
            role = usage.get(receiver.get("gsis_id"), {})
            slot_rate = receiver.get("slot_rate") or 0
            defender = slot_defenders[0] if slot_rate >= 50 and slot_defenders else None
            alignment = "slot" if slot_rate >= 50 else "wide"
            receiver_profile = repository.receiver_pass_profile(
                pff_season, receiver.get("gsis_id") or "",
            )
            receiver_zones = [zone for zone in receiver_profile.get("zones", [])
                              if (zone.get("targets") or 0) >= minimum_zone_targets]
            receiver_zones.sort(key=lambda row: (
                -(row.get("targets") or 0), -(row.get("epa_per_target") or -99),
            ))
            preferred = max(receiver_zones, key=lambda row: (
                row.get("targets") or 0, row.get("epa_per_target") or -99,
            ), default=None)
            exact_allowed = None
            if preferred:
                exact_allowed = next((
                    zone for zone in defense_zones
                    if zone.get("depth_bucket") == preferred.get("depth_bucket")
                    and zone.get("pass_location") == preferred.get("pass_location")
                ), None)
            total_targets = receiver_profile.get("total", {}).get("targets") or 0
            zone_share = ((preferred.get("targets") or 0) / total_targets
                          if preferred and total_targets else None)
            # Every zone the receiver has real volume in, not just the top
            # one -- a receiver who touches several of this defense's weak
            # zones is a bigger matchup edge than one whose volume is
            # concentrated in a single (even if very weak) cell.
            zone_matchups = []
            exploit_score = 0.0
            weak_hits = []
            for zone in receiver_zones[:5]:
                allowed = next((item for item in defense_zones
                                if item.get("depth_bucket") == zone.get("depth_bucket")
                                and item.get("pass_location") == zone.get("pass_location")), None)
                share = (zone.get("targets") or 0) / total_targets if total_targets else None
                is_weak = (zone["depth_bucket"], zone["pass_location"]) in weak_zones
                if is_weak:
                    weak_hits.append(zone)
                if share and allowed and allowed.get("epa_per_attempt") is not None:
                    exploit_score += share * allowed["epa_per_attempt"]
                zone_matchups.append({
                    "depth_bucket": zone["depth_bucket"], "pass_location": zone["pass_location"],
                    "targets": zone.get("targets"), "receptions": zone.get("receptions"),
                    "receiving_yards": zone.get("receiving_yards"),
                    "offense_epa": zone.get("epa_per_target"), "target_share": share,
                    "defense_attempts": allowed.get("attempts") if allowed else None,
                    "defense_epa": allowed.get("epa_per_attempt") if allowed else None,
                    "defense_completion_rate": allowed.get("completion_rate") if allowed else None,
                    "is_weak_zone": is_weak,
                })
            if len(weak_hits) >= 2:
                labels = ", ".join(f"{zone['depth_bucket'].title()} {zone['pass_location']}"
                                   for zone in weak_hits[:3])
                interaction = (
                    f"{receiver['player_name']} has measured volume in {len(weak_hits)} zones "
                    f"where {defense} allows more value than its own zone average, led by {labels}."
                )
            elif weak_hits:
                zone = weak_hits[0]
                interaction = (
                    f"{zone['depth_bucket'].title()} {zone['pass_location']} is a zone "
                    f"{receiver['player_name']} has real volume in and {defense} allows more "
                    f"value than its own zone average there."
                )
            elif preferred and exact_allowed:
                interaction = (
                    f"{preferred['depth_bucket'].title()} {preferred['pass_location']} is "
                    f"{receiver['player_name']}’s highest-volume stored target area and the "
                    f"comparison below is the defense’s result allowed in that exact zone."
                )
            elif preferred:
                interaction = "The receiver’s preferred target zone is measured, but the defense lacks a qualifying result in that exact cell."
            else:
                interaction = "Alignment and route volume are available; receiver-level target-location data is not yet available."
            receiver_ngs = receiving_ngs.get(receiver.get("gsis_id"), {})
            candidates.append({
                "offense": offense, "defense": defense, "player_name": receiver["player_name"],
                "player_id": receiver.get("gsis_id"), "alignment": alignment,
                "alignment_rate": slot_rate if alignment == "slot" else receiver.get("wide_rate"),
                "routes": receiver.get("routes"), "route_grade": receiver.get("grades_pass_route"),
                "yprr": receiver.get("yprr"), "target_share": role.get("target_share"),
                "defender": defender, "vulnerable_zone": vulnerable,
                "preferred_zone": preferred, "exact_allowed_zone": exact_allowed,
                "preferred_zone_share": zone_share, "interaction": interaction,
                "zone_matchups": zone_matchups, "weak_zone_hits": len(weak_hits),
                "exploit_score": exploit_score,
                "confidence": "High-volume interaction" if (receiver.get("routes") or 0) >= 350 else "Rotation interaction",
                "season": pff_season,
                "ngs_separation": receiver_ngs.get("ngs_rec_separation"),
                "ngs_cushion": receiver_ngs.get("ngs_rec_cushion"),
                "ngs_separation_rank": receiving_ngs_ranks["ngs_rec_separation"].get(receiver.get("gsis_id")),
                "ngs_cushion_rank": receiving_ngs_ranks["ngs_rec_cushion"].get(receiver.get("gsis_id")),
            })
        candidates.sort(key=lambda card: (-card["weak_zone_hits"], -card["exploit_score"]))
        cards.extend(candidates[:3])
    return cards


def rushing_matchups(repository: NFLRepository, pff: NFLPFFService,
                     game: dict[str, Any], roster_season: int, pff_season: int,
                     profiles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Connect likely ball carriers to the opposing run-defense result."""
    output = []
    rushing_ngs_ranks = _ngs_league_ranks(
        repository, pff_season, ("RB", "FB"),
        ("carries", "ngs_rush_efficiency_wtd", "ngs_rush_stacked_box_pct_wtd",
         "ngs_rush_yards_over_expected"),
        ("ngs_rush_efficiency", "ngs_rush_stacked_box_pct", "ngs_rush_yards_over_expected"),
    )
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
        rushing_ngs = ngs_season_lookup(
            repository, pff_season, offense,
            ("carries", "ngs_rush_efficiency_wtd", "ngs_rush_stacked_box_pct_wtd",
             "ngs_rush_yards_over_expected"),
        )
        defense_profile = profiles.get(defense) or {}
        for runner in runners[:3]:
            role = usage.get(runner.get("gsis_id"), {})
            runner_ngs = rushing_ngs.get(runner.get("gsis_id"), {})
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
                "ngs_efficiency": runner_ngs.get("ngs_rush_efficiency"),
                "ngs_stacked_box_pct": runner_ngs.get("ngs_rush_stacked_box_pct"),
                "ngs_yards_over_expected": runner_ngs.get("ngs_rush_yards_over_expected"),
                "ngs_efficiency_rank": rushing_ngs_ranks["ngs_rush_efficiency"].get(runner.get("gsis_id")),
                "ngs_yards_over_expected_rank": rushing_ngs_ranks["ngs_rush_yards_over_expected"].get(
                    runner.get("gsis_id")),
            })
    return output


def player_matchup_watches(alignment_cards: list[dict[str, Any]],
                           trench_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Named player-vs-player pairings worth calling out on the matchups tab.

    Most of what this page knows is receiver-vs-zone, not receiver-vs-
    defender -- the stored data has no coverage assignment, and the page
    says so deliberately (see alignment_matchups' docstring context). Two
    pairings ARE genuinely named on both sides: a slot receiver against the
    opponent's primary slot corner, and a team's leading pass rusher against
    the opposing line's best pass-block grade -- both roles are positional,
    not a specific-play assignment, so naming them is not overreach.
    """
    watches = []
    for card in alignment_cards:
        defender = card.get("defender")
        if not defender or not defender.get("player_name") or not card.get("player_id"):
            continue
        watches.append({
            "kind": "Slot coverage", "offense": card["offense"], "defense": card["defense"],
            "offense_player": card["player_name"], "offense_player_id": card["player_id"],
            "offense_detail": f"{card.get('route_grade') or 0:.1f} route grade · "
                              f"{card.get('yprr') or 0:.2f} yd/route",
            "defense_player": defender["player_name"], "defense_player_id": defender.get("gsis_id"),
            "defense_detail": f"{defender.get('yards_per_coverage_snap') or 0:.2f} "
                              f"yd/snap allowed in the slot",
        })
    for card in trench_cards:
        rusher = card["rushers"][0] if card.get("rushers") else None
        blockers = [row for row in card.get("blockers", []) if row.get("grades_pass_block") is not None]
        blocker = max(blockers, key=lambda row: row["grades_pass_block"], default=None)
        if not rusher or not blocker or not rusher.get("player_id"):
            continue
        watches.append({
            "kind": "Pass rush", "offense": card["offense"], "defense": card["defense"],
            "offense_player": blocker["player_name"], "offense_player_id": blocker.get("gsis_id"),
            "offense_detail": f"{blocker.get('grades_pass_block') or 0:.1f} pass-block grade",
            "defense_player": rusher["player_name"], "defense_player_id": rusher.get("player_id"),
            "defense_detail": f"{rusher.get('qb_hits') or 0:.0f} hits · {rusher.get('sacks') or 0:.1f} sacks",
        })
    return watches
