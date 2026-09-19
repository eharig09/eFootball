"""Presentation packets for nflfastR quarterback and defense passing zones."""

from __future__ import annotations

from math import sqrt
from typing import Any


DEPTHS = (("deep", "20+ yards"), ("intermediate", "10–19 yards"),
          ("short", "1–9 yards"), ("behind", "Behind LOS"))
LOCATIONS = ("left", "middle", "right")


def receiver_position_breakdown(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate opponent receiving production without listing every player."""
    grouped: dict[str, dict[str, Any]] = {}
    for player in players:
        position = str(player.get("position") or "UNK").upper()
        row = grouped.setdefault(position, {
            "position": position, "players": 0, "targets": 0, "receptions": 0,
            "yards": 0, "touchdowns": 0,
        })
        row["players"] += 1
        row["targets"] += int(player.get("targets") or 0)
        row["receptions"] += int(player.get("receptions") or 0)
        row["yards"] += round(float(player.get("receiving_yards") or 0))
        row["touchdowns"] += int(player.get("touchdowns") or 0)
    rows = sorted(grouped.values(), key=lambda row: (-row["targets"], row["position"]))
    for row in rows:
        row["detail"] = (f"{row['receptions']}/{row['targets']} · {row['yards']} yd · "
                         f"{row['touchdowns']} TD")
    return rows


def pass_zone_packet(profile: dict[str, Any], *, receiver: bool = False,
                     contributors: list[dict[str, Any]] | None = None,
                     defenders: list[dict[str, Any]] | None = None,
                     lower_is_better: bool = False) -> dict[str, Any]:
    indexed = {(row["depth_bucket"], row["pass_location"]): row
               for row in profile.get("zones", [])}
    contributor_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for contributor in contributors or ():
        contributor_index.setdefault(
            (contributor["depth_bucket"], contributor["pass_location"]), []
        ).append(contributor)
    # Coverage assignment isn't in the data, but who broke up or picked off a
    # target in this zone is -- an honest defensive credit, shown alongside
    # the offensive contributors rather than instead of them.
    defender_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for defender in defenders or ():
        defender_index.setdefault(
            (defender["depth_bucket"], defender["pass_location"]), []
        ).append(defender)
    rows = []
    for depth, label in DEPTHS:
        cells = []
        for location in LOCATIONS:
            item = indexed.get((depth, location))
            zone_contributors = contributor_index.get((depth, location), [])
            cells.append({"location": location.title(), **(item or {}),
                          "contributors": zone_contributors[:8],
                          "contributor_position_summary": receiver_position_breakdown(
                              zone_contributors),
                          "defenders": defender_index.get((depth, location), [])[:8]})
        rows.append({"depth": depth, "label": label, "cells": cells})
    sample_key = "targets" if receiver else "attempts"
    metric_key = "epa_per_target" if receiver else "epa_per_attempt"
    for row in rows:
        for cell in row["cells"]:
            value = cell.get(metric_key)
            if not cell.get(sample_key) or value is None:
                cell.update(tone="neutral", strength=0)
                continue
            performance = -value if lower_is_better else value
            magnitude = abs(performance)
            strength = 1 if magnitude < .10 else (2 if magnitude < .30 else
                       (3 if magnitude < .60 else 4))
            cell.update(tone="good" if performance > .025 else
                        ("bad" if performance < -.025 else "neutral"), strength=strength)
    return {**profile, "rows": rows, "has_data": bool(profile.get("zones")),
            "is_receiver": receiver, "sample_key": sample_key,
            "lower_is_better": lower_is_better}


def pass_matchup_packet(offense: dict[str, Any] | None,
                        defense: dict[str, Any] | None) -> dict[str, Any]:
    """Join quarterback production to opponent results allowed by field zone."""
    offense = offense or {"rows": [], "total": {}}
    defense = defense or {"rows": [], "total": {}}
    offense_cells = {(row["depth"], cell["location"].lower()): cell
                     for row in offense.get("rows", []) for cell in row["cells"]}
    defense_cells = {(row["depth"], cell["location"].lower()): cell
                     for row in defense.get("rows", []) for cell in row["cells"]}
    offense_total = offense.get("total", {}).get("attempts") or 0
    defense_total = defense.get("total", {}).get("attempts") or 0
    rows = []
    for depth, label in DEPTHS:
        cells = []
        for location in LOCATIONS:
            attack = offense_cells.get((depth, location), {})
            resist = defense_cells.get((depth, location), {})
            attack_attempts = attack.get("attempts") or 0
            defense_attempts = resist.get("attempts") or 0
            attack_epa = attack.get("epa_per_attempt")
            defense_epa = resist.get("epa_per_attempt")
            comparable = bool(attack_attempts and defense_attempts and
                              attack_epa is not None and defense_epa is not None)
            # Both values share the attacking-offense EPA sign convention (positive
            # favors whoever has the ball), so a favorable matchup for the offense
            # needs BOTH terms to point that way — they combine additively, not by
            # subtraction (which would cancel a good offense against a leaky defense).
            edge = attack_epa + defense_epa if comparable else None
            interaction = (sqrt((attack_attempts / offense_total) *
                                (defense_attempts / defense_total))
                           if offense_total and defense_total else None)
            magnitude = abs(edge) if edge is not None else 0
            strength = (1 if magnitude < .10 else (2 if magnitude < .30 else
                        (3 if magnitude < .60 else 4))) if comparable else 0
            lean = (("offense" if edge > .025 else
                    ("defense" if edge < -.025 else "even")) if comparable else "neutral")
            cells.append({
                "location": location.title(), "offense_attempts": attack_attempts,
                "defense_attempts": defense_attempts, "offense_epa": attack_epa,
                "defense_epa": defense_epa, "edge": edge, "lean": lean,
                "strength": strength, "interaction_share": interaction,
                "contributors": attack.get("contributors", []),
                "defenders": resist.get("defenders", []),
                # Season-wide, every-opponent receivers this defense has
                # allowed in this exact zone -- "what positions beat this
                # defense here", not this one game's specific recipients.
                "defense_allowed": resist.get("contributors", []),
                "defense_position_summary": resist.get(
                    "contributor_position_summary", []),
            })
        rows.append({"depth": depth, "label": label, "cells": cells})
    return {"rows": rows,
            "has_data": bool(offense.get("has_data") and defense.get("has_data")),
            "offense_total": offense_total, "defense_total": defense_total}
