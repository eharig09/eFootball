"""Presentation packets for nflfastR run-direction charting.

Mirrors `passing.py`'s zone/matchup packet shape (tone/strength for a single
side, an additive offense+defense edge for a joined matchup grid), just over
the standard 7-cell broadcast run-direction chart (left end/tackle/guard,
middle, right guard/tackle/end) instead of the depth x location pass grid --
there's no depth dimension to a run direction the way there is a throw depth.
"""

from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.repository import NFLRepository

DIRECTIONS = tuple(NFLRepository.RUN_DIRECTIONS)
DIRECTION_LABELS = {
    "left end": "Left End", "left tackle": "Left Tackle", "left guard": "Left Guard",
    "middle": "Middle", "right guard": "Right Guard", "right tackle": "Right Tackle",
    "right end": "Right End",
}


def run_direction_packet(profile: dict[str, Any], *, contributors: list[dict[str, Any]] | None = None,
                         defenders: list[dict[str, Any]] | None = None,
                         lower_is_better: bool = False) -> dict[str, Any]:
    indexed = {row["direction"]: row for row in profile.get("directions", [])}
    contributor_index: dict[str, list[dict[str, Any]]] = {}
    for contributor in contributors or ():
        contributor_index.setdefault(contributor["direction"], []).append(contributor)
    # Same honest-credit approach as the pass zone grid: who tackled the run
    # is already attributed in the play-by-play, not a fabricated assignment.
    defender_index: dict[str, list[dict[str, Any]]] = {}
    for defender in defenders or ():
        defender_index.setdefault(defender["direction"], []).append(defender)
    cells = []
    for direction in DIRECTIONS:
        item = indexed.get(direction)
        cell = {"direction": direction, "label": DIRECTION_LABELS[direction], **(item or {}),
               "contributors": contributor_index.get(direction, [])[:8],
               "defenders": defender_index.get(direction, [])[:8]}
        value = cell.get("epa_per_attempt")
        if not cell.get("attempts") or value is None:
            cell.update(tone="neutral", strength=0)
        else:
            performance = -value if lower_is_better else value
            magnitude = abs(performance)
            strength = 1 if magnitude < .10 else (2 if magnitude < .30 else
                       (3 if magnitude < .60 else 4))
            cell.update(tone="good" if performance > .025 else
                        ("bad" if performance < -.025 else "neutral"), strength=strength)
        cells.append(cell)
    return {**profile, "cells": cells, "has_data": bool(profile.get("directions")),
            "lower_is_better": lower_is_better}


def run_matchup_packet(offense: dict[str, Any] | None,
                       defense: dict[str, Any] | None) -> dict[str, Any]:
    """Join rushing production to opponent results allowed by run direction."""
    offense = offense or {"cells": [], "total": {}}
    defense = defense or {"cells": [], "total": {}}
    offense_cells = {cell["direction"]: cell for cell in offense.get("cells", [])}
    defense_cells = {cell["direction"]: cell for cell in defense.get("cells", [])}
    offense_total = offense.get("total", {}).get("attempts") or 0
    defense_total = defense.get("total", {}).get("attempts") or 0
    cells = []
    for direction in DIRECTIONS:
        attack = offense_cells.get(direction, {})
        resist = defense_cells.get(direction, {})
        attack_attempts = attack.get("attempts") or 0
        defense_attempts = resist.get("attempts") or 0
        attack_epa = attack.get("epa_per_attempt")
        defense_epa = resist.get("epa_per_attempt")
        comparable = bool(attack_attempts and defense_attempts and
                          attack_epa is not None and defense_epa is not None)
        # Same offense-relative EPA sign convention as the pass matchup grid --
        # both terms favor the offense when positive, so they add, not subtract.
        edge = attack_epa + defense_epa if comparable else None
        magnitude = abs(edge) if edge is not None else 0
        strength = (1 if magnitude < .10 else (2 if magnitude < .30 else
                    (3 if magnitude < .60 else 4))) if comparable else 0
        lean = (("offense" if edge > .025 else
                ("defense" if edge < -.025 else "even")) if comparable else "neutral")
        cells.append({
            "direction": direction, "label": DIRECTION_LABELS[direction],
            "offense_attempts": attack_attempts, "defense_attempts": defense_attempts,
            "offense_epa": attack_epa, "defense_epa": defense_epa,
            "edge": edge, "lean": lean, "strength": strength,
            "contributors": attack.get("contributors", []),
            "defenders": resist.get("defenders", []),
            # Season-wide, every-opponent rushers this defense has allowed
            # in this exact direction -- "what positions beat this defense
            # here", not this one game's specific ball carriers.
            "defense_allowed": resist.get("contributors", []),
        })
    return {"cells": cells,
            "has_data": bool(offense.get("has_data") and defense.get("has_data")),
            "offense_total": offense_total, "defense_total": defense_total}
