"""Coach and coordinator performance/tendency packets for NFL team pages."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.nfl.pff import NFLPFFService
from sports_aggregator.nfl.repository import NFLRepository


FRONT = {"DE", "DT", "DL", "NT", "EDGE"}
LINEBACKERS = {"LB", "ILB", "OLB"}
SECONDARY = {"CB", "DB", "S", "FS", "SS"}


def _pressure_sources(repository: NFLRepository, season: int, team: str) -> dict[str, Any]:
    rows = repository.player_leaders_for_metrics(
        season, ("def_sacks", "def_qb_hits"), team=team,
    )
    groups: dict[str, float] = defaultdict(float)
    for row in rows:
        position = (row.get("position") or "").upper()
        group = ("Defensive front" if position in FRONT else
                 "Linebackers" if position in LINEBACKERS else
                 "Secondary" if position in SECONDARY else "Other")
        groups[group] += float(row.get("value") or 0)
    total = sum(groups.values())
    order = ("Defensive front", "Linebackers", "Secondary", "Other")
    return {
        "events": total,
        "groups": [{"label": label, "events": groups[label],
                    "share": groups[label] / total if total else None}
                   for label in order if groups[label]],
    }


def staff_tendencies(repository: NFLRepository, pff: NFLPFFService, season: int,
                     team: str, pff_season: int | None,
                     efficiency: dict[str, Any] | None,
                     efficiency_ranks: dict[str, int | None] | None = None) -> dict[str, Any]:
    staff = repository.team_staff(season, team)
    by_role = {row["role"]: row for row in staff}
    head_coach = by_role.get("Head coach")
    playcaller = next((row for row in staff if row.get("playcaller")), None)
    coordinator = by_role.get("Offensive coordinator")
    defensive = by_role.get("Defensive coordinator")
    playcalling = repository.team_playcalling_profile(season, team)
    situation = repository.team_situational_profile(season, team)
    coverage = (pff.team_coverage_tendency(pff_season, team)
                if pff_season is not None else {"season": None, "coverage_snaps": 0})
    efficiency_ranks = efficiency_ranks or {}
    return {
        "staff": staff,
        "head_coach": head_coach,
        "head_coach_performance": repository.coach_performance(
            head_coach["coach_name"] if head_coach else None
        ),
        "offense": {
            "coach": playcaller or coordinator,
            "coordinator": coordinator,
            "playcalling": playcalling,
            "situation": situation,
            "epa_per_play": efficiency.get("epa_per_play") if efficiency else None,
            "epa_per_play_rank": efficiency_ranks.get("epa_per_play"),
            "pass_epa_per_play": efficiency.get("pass_epa_per_play") if efficiency else None,
            "pass_epa_per_play_rank": efficiency_ranks.get("pass_epa_per_play"),
            "rush_epa_per_play": efficiency.get("rush_epa_per_play") if efficiency else None,
            "rush_epa_per_play_rank": efficiency_ranks.get("rush_epa_per_play"),
        },
        "defense": {
            "coach": defensive,
            "coverage": coverage,
            "pressure": _pressure_sources(repository, season, team),
            "epa_allowed": efficiency.get("defensive_epa_allowed") if efficiency else None,
            "epa_allowed_rank": efficiency_ranks.get("defensive_epa_allowed"),
            "pressure_rate": (repository.team_pressure_rate(season, team)
                              or repository.team_pressure_rate(season - 1, team)),
        },
    }
