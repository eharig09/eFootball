"""Evidence rules for tying a record that only carries a *name* (a portal entry, a draft pick, a PFF row) to a player.

Two different players share a name often enough to matter (1,399 of 18,888 stored portal entries name someone who
maps to more than one player id). A name match alone is therefore never enough: the record must also fit where the
player actually played and what he plays. Rosters are stored for 2019 onward, so a player's own stints are the
evidence.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

_GROUP_MEMBERS = {
    "QB": "QB", "RB": "RB FB HB APB", "REC": "WR TE", "OL": "OL OT OG C G T IOL",
    "DL": "DL DE DT NT EDGE", "LB": "LB ILB OLB MLB", "DB": "CB S DB FS SS NB", "ST": "K P LS PK",
}
POSITION_GROUP = {code: group for group, members in _GROUP_MEMBERS.items() for code in members.split()}
#: Position groups players move between often enough that a change is not evidence of a different person.
_NEIGHBORS = ({"RB", "REC"}, {"DL", "LB"}, {"LB", "DB"}, {"OL", "DL"})


def position_group(position: str | None) -> str | None:
    return POSITION_GROUP.get((position or "").upper().strip())


def positions_compatible(first: str | None, second: str | None) -> bool:
    """False only when both positions are known and belong to unrelated groups (a safety is not an offensive lineman)."""
    a, b = position_group(first), position_group(second)
    return not a or not b or a == b or {a, b} in _NEIGHBORS


def transfer_belongs(transfer: Mapping[str, Any], stints: Iterable[Mapping[str, Any]]) -> bool:
    """Whether a portal entry found by name is this player's, judged from his own roster stints.

    Needs team evidence: he was on the origin school in or before the portal season, or on the destination in or after
    it. A player with no stored stints at all cannot be contradicted, so a compatible position is enough for him.
    """
    stints = list(stints)
    year = transfer.get("season")
    origin, destination = transfer.get("origin"), transfer.get("destination")
    if not stints:
        return True
    if year is not None:
        if origin and any(item["team"] == origin and item["season"] <= year for item in stints):
            return True
        if destination and any(item["team"] == destination and item["season"] >= year for item in stints):
            return True
    return False


def draft_pick_belongs(pick: Mapping[str, Any], player_id: str, stints: Iterable[Mapping[str, Any]]) -> bool:
    """A draft pick carries the college athlete id when the source knew it; without one, the college must match."""
    athlete = pick.get("college_athlete_id")
    if athlete:
        return str(athlete) == str(player_id)
    teams = {item["team"] for item in stints}
    return pick.get("college_team") in teams and any(
        item["team"] == pick.get("college_team") and item["season"] <= (pick.get("draft_year") or 0) for item in stints)
