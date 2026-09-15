"""Roster significance and position-room packets built from auditable evidence."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from typing import Any

from sports_aggregator.nfl.pff import NFLPFFService
from sports_aggregator.nfl.naming import normalize_name
from sports_aggregator.nfl.repository import NFLRepository

GRADE_KEYS = (
    "deep_grades_pass", "medium_grades_pass", "short_grades_pass",
    "behind_los_grades_pass", "grades_pass_route", "grades_run",
    "grades_pass_block", "grades_run_block", "man_grades_coverage_defense",
    "zone_grades_coverage_defense",
)
GRADE_FAMILIES = {
    "deep_grades_pass": "passing_depth", "medium_grades_pass": "passing_depth",
    "short_grades_pass": "passing_depth", "behind_los_grades_pass": "passing_depth",
    "grades_pass_route": "receiving_summary",
    "grades_run": "rushing_summary", "grades_pass_block": "offense_blocking",
    "grades_run_block": "offense_blocking",
    "man_grades_coverage_defense": "defense_coverage_scheme",
    "zone_grades_coverage_defense": "defense_coverage_scheme",
}
POSITION_SECTIONS = (
    ("Offense", ("QB", "RB", "FB", "WR", "TE", "LT", "LG", "C", "RG", "RT", "OT", "OG", "OL")),
    ("Defense", ("EDGE", "DE", "DT", "NT", "DL", "LB", "CB", "NB", "S", "DB")),
    ("Special teams", ("K", "P", "LS", "KR", "PR")),
)


def _snap_index(repository: NFLRepository, season: int, teams: set[str]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in repository.snap_leaders_for_teams(season, teams):
        player_id = row.get("player_id")
        if player_id and max(row.get("offense_pct") or 0, row.get("defense_pct") or 0,
                             row.get("st_pct") or 0) > max(
                index.get(player_id, {}).get("offense_pct") or 0,
                index.get(player_id, {}).get("defense_pct") or 0,
                index.get(player_id, {}).get("st_pct") or 0):
            index[row["player_id"]] = row
    return index


def _grade_index(repository: NFLRepository, season: int,
                 player_ids: set[str]) -> dict[str, dict[str, dict[str, float]]]:
    if not player_ids:
        return {}
    placeholders = ",".join("?" for _ in player_ids)
    metric_placeholders = ",".join("?" for _ in GRADE_KEYS)
    with closing(repository._connect()) as connection:
        rows = connection.execute(
            f"""SELECT gsis_id,team,family,metric,value FROM nfl_pff_player_metrics
                WHERE season=? AND week=0 AND gsis_id IN ({placeholders})
                  AND metric IN ({metric_placeholders})""",
            (season, *player_ids, *GRADE_KEYS),
        )
        output: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
        for row in rows:
            if row["family"] == GRADE_FAMILIES.get(row["metric"]):
                output[row["gsis_id"]][row["team"]][row["metric"]] = row["value"]
    return {player_id: dict(teams) for player_id, teams in output.items()}


def _player_grades(index: dict[str, dict[str, dict[str, float]]], player_id: str,
                   teams: list[str] | tuple[str, ...] | None = None) -> dict[str, float]:
    """Return metrics from the relevant club, averaging only genuine multi-club stints."""
    by_team = index.get(player_id, {})
    selected = [metrics for team, metrics in by_team.items() if not teams or team in teams]
    if not selected:
        selected = list(by_team.values())
    values: dict[str, list[float]] = defaultdict(list)
    for metrics in selected:
        for metric, value in metrics.items():
            values[metric].append(value)
    return {metric: sum(items) / len(items) for metric, items in values.items()}


def _relevant_grades(position: str | None, metrics: dict[str, float]) -> list[dict[str, Any]]:
    """Select role-specific evidence instead of rewarding a player's highest grade."""
    position = (position or "").upper()
    if position == "QB":
        keys = ("deep_grades_pass", "medium_grades_pass", "short_grades_pass",
                "behind_los_grades_pass")
    elif position in {"RB", "FB"}: keys = ("grades_run", "grades_pass_route", "grades_pass_block")
    elif position in {"WR", "TE"}: keys = ("grades_pass_route",)
    elif position in {"OT", "T", "OG", "G", "C", "OL", "LT", "LG", "RG", "RT"}:
        keys = ("grades_pass_block", "grades_run_block")
    elif position in {"CB", "DB", "S", "FS", "SS", "LB", "ILB", "OLB"}:
        keys = ("man_grades_coverage_defense", "zone_grades_coverage_defense")
    else: keys = ()
    labels = {
        "deep_grades_pass": "deep passing", "medium_grades_pass": "intermediate passing",
        "short_grades_pass": "short passing", "behind_los_grades_pass": "behind-LOS passing",
        "grades_pass_route": "route",
        "grades_run": "rushing", "grades_pass_block": "pass block",
        "grades_run_block": "run block", "man_grades_coverage_defense": "man coverage",
        "zone_grades_coverage_defense": "zone coverage",
    }
    return [{"metric": key, "label": labels[key], "value": metrics[key]}
            for key in keys if key in metrics]


def _draft_index(repository: NFLRepository, player_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not player_ids:
        return {}
    placeholders = ",".join("?" for _ in player_ids)
    with closing(repository._connect()) as connection:
        return {row["gsis_id"]: {key: row[key] for key in
                ("draft_year", "draft_round", "draft_pick", "draft_team")}
                for row in connection.execute(
                    f"""SELECT gsis_id,draft_year,draft_round,draft_pick,draft_team
                        FROM player_master WHERE gsis_id IN ({placeholders})""",
                    tuple(player_ids),
                )}


def _score(row: dict[str, Any], snaps: dict[str, Any] | None,
           grades: list[dict[str, Any]], draft: dict[str, Any], season: int) -> dict[str, Any]:
    snap_pct = max((snaps or {}).get("offense_pct") or 0,
                   (snaps or {}).get("defense_pct") or 0,
                   (snaps or {}).get("st_pct") or 0)
    snap_points = min(38.0, snap_pct * 38)
    grade = (sum(item["value"] for item in grades) / len(grades)) if grades else None
    grade_points = 0 if grade is None else max(0.0, min(37.0, (grade - 50) * 1.15))
    draft_round = draft.get("draft_round")
    draft_pick = draft.get("draft_pick")
    career_year = max(1, season - (draft.get("draft_year") or season) + 1)
    draft_weight = 1.0 if career_year <= 2 else (.4 if career_year <= 4 else 0.0)
    if draft_round == 1:
        draft_points = 25
    elif draft_round == 2:
        draft_points = 20
    elif draft_round == 3:
        draft_points = 15
    elif draft_round in {4, 5}:
        draft_points = 9
    elif draft_round in {6, 7}:
        draft_points = 5
    else:
        draft_points = 0
    draft_points *= draft_weight
    score = round(snap_points + grade_points + draft_points, 1)
    evidence = []
    if snap_pct:
        evidence.append(f"{snap_pct:.0%} snap participation")
    if grades:
        evidence.extend(f"{item['value']:.1f} PFF {item['label']}" for item in grades)
    if draft_round and draft_weight:
        evidence.append(f"Round {draft_round}" + (f", pick {draft_pick}" if draft_pick else "") +
                        (" draft capital" if draft_weight == 1 else " draft capital (reduced)"))
    return {**row, "significance_score": score, "snap_participation": snap_pct or None,
            "pff_grade": grade, "pff_grades": grades,
            "pff_grade_metric": ", ".join(item["metric"] for item in grades) or None,
            "draft": draft, "career_year": career_year, "draft_weight": draft_weight,
            "significance_evidence": evidence,
            "significance_tier": ("Core" if score >= 65 else
                                  ("Material" if score >= 38 or
                                   (draft_round in {1, 2} and draft_weight == 1) else "Depth"))}


def significant_movements(repository: NFLRepository, pff: NFLPFFService,
                          season: int, team: str, *, limit: int = 8) -> dict[str, Any]:
    movement = repository.roster_movements(season, team)
    prior = movement["prior_season"]
    teams = {team}
    for row in movement["arrivals"]:
        teams.update(row.get("from_teams") or ())
    snaps = _snap_index(repository, prior, teams)
    player_ids = {row["player_id"] for row in
                  movement["arrivals"] + movement["departures"]}
    grades = _grade_index(repository, prior, player_ids)
    drafts = _draft_index(repository, player_ids)

    def enrich(row: dict[str, Any]) -> dict[str, Any]:
        prior_teams = row.get("from_teams") or [row.get("team")]
        relevant = _relevant_grades(
            row.get("position"), _player_grades(grades, row["player_id"], prior_teams))
        return _score(row, snaps.get(row["player_id"]), relevant,
                      drafts.get(row["player_id"], {}), season)

    arrivals = sorted(map(enrich, movement["arrivals"]),
                      key=lambda row: (-row["significance_score"], row["full_name"]))
    departures = sorted(map(enrich, movement["departures"]),
                        key=lambda row: (-row["significance_score"], row["full_name"]))
    # A player must have meaningful prior participation/quality or premium draft
    # capital. This keeps churn at the bottom of the roster out of headline cards.
    key = lambda rows: [row for row in rows if (
        row["significance_score"] >= 30 or
        ((row.get("draft", {}).get("draft_round") or 99) <= 2 and row.get("draft_weight"))
    )][:limit]
    return {**movement, "all_arrivals": arrivals, "all_departures": departures,
            "arrivals": key(arrivals), "departures": key(departures),
            "arrival_count": len(movement["arrivals"]), "departure_count": len(movement["departures"]),
            "threshold": 30}


def position_rooms(repository: NFLRepository, pff: NFLPFFService,
                   season: int, team: str, pff_season: int,
                   injuries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    roster = repository.team_roster(season, team)
    roster_by_id = {row["player_id"]: row for row in roster}
    depth: dict[str, dict[str, Any]] = {}
    for row in repository.current_depth_chart(season, team):
        player_id = row.get("gsis_id")
        if not player_id:
            continue
        current = depth.get(player_id)
        candidate_special = row.get("position_group") == "Special Teams"
        current_special = current and current.get("position_group") == "Special Teams"
        # A player can appear in a scrimmage formation and on special teams.
        # Preserve the offense/defense role first, then the strongest rank.
        if (current is None or (current_special and not candidate_special) or
                (current_special == candidate_special and
                 (row.get("position_rank") or 99) < (current.get("position_rank") or 99))):
            depth[player_id] = row
    snap_season = season if repository.team_snap_leaders(season, team) else season - 1
    snaps = _snap_index(repository, snap_season, {team})
    # Depth is authoritative for named slots. Roster-only players are appended
    # afterward so no active member disappears from the full room view.
    player_ids = set(roster_by_id) | set(depth)
    grades = _grade_index(repository, pff_season, player_ids)
    drafts = _draft_index(repository, player_ids)
    injury_by_gsis = {row["gsis_id"]: row for row in injuries or () if row.get("gsis_id")}
    injury_by_espn = {str(row["espn_id"]): row for row in injuries or () if row.get("espn_id")}
    injury_by_name = {normalize_name(row["player_name"]): row for row in injuries or ()}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    slots = {
        "LT": "LT", "RT": "RT", "LG": "LG", "RG": "RG", "C": "C",
        "LDE": "EDGE", "RDE": "EDGE", "LE": "EDGE", "RE": "EDGE",
        "LDT": "DT", "RDT": "DT", "NT": "NT",
        "MLB": "LB", "LILB": "LB", "RILB": "LB", "WLB": "LB", "SLB": "LB",
        "LCB": "CB", "RCB": "CB", "NB": "NB", "FS": "S", "SS": "S",
        "PK": "K",
    }
    for player_id in player_ids:
        player = roster_by_id.get(player_id, {})
        slot = depth.get(player_id, {})
        snap = snaps.get(player_id, {})
        draft = drafts.get(player_id, {})
        raw_slot = slot.get("position_abbreviation")
        group = slots.get(raw_slot, raw_slot) or player.get("position") or player.get("depth_position") or "Other"
        relevant = _relevant_grades(group, _player_grades(grades, player_id, [team]))
        grade = sum(item["value"] for item in relevant) / len(relevant) if relevant else None
        injury = (injury_by_gsis.get(player_id) or
                  injury_by_espn.get(str(player.get("espn_id"))) or
                  injury_by_name.get(normalize_name(player.get("full_name") or slot.get("player_name"))))
        groups[group].append({"player_id": player_id,
                              "full_name": player.get("full_name") or slot.get("player_name") or player_id,
                              **player, "depth_rank": slot.get("position_rank"),
                              "depth_slot": slot.get("position_abbreviation"),
                              "depth_formation": slot.get("position_group"),
                              "snap_season": snap_season,
                              "snap_participation": max(snap.get("offense_pct") or 0,
                                                        snap.get("defense_pct") or 0,
                                                        snap.get("st_pct") or 0) or None,
                              "pff_grade": grade,
                              "pff_grade_metric": ", ".join(item["metric"] for item in relevant) or None,
                              "draft": draft, "injury": injury})
    ordered = []
    flat_order = tuple(position for _, positions in POSITION_SECTIONS for position in positions)
    position_index = {name: index for index, name in enumerate(flat_order)}
    for group, players in groups.items():
        players.sort(key=lambda row: (row["depth_rank"] or 99,
                                     -(row["snap_participation"] or 0), row["full_name"]))
        ordered.append({"position": group, "players": players,
                        "starter_count": sum((row["depth_rank"] or 99) == 1 for row in players)})
    ordered.sort(key=lambda room: (position_index.get(room["position"], 99), room["position"]))
    sections = []
    used = set()
    for label, positions in POSITION_SECTIONS:
        rooms = [room for position in positions for room in ordered
                 if room["position"] == position]
        used.update(room["position"] for room in rooms)
        if rooms:
            sections.append({"label": label, "rooms": rooms})
    other = [room for room in ordered if room["position"] not in used]
    if other:
        sections.append({"label": "Other", "rooms": other})
    return sections
