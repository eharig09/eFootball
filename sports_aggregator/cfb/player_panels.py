"""Percentile panels for a college football player: each stat ranked among FBS players at the same position group.

Season stat lines come from `player_season_stats` (CFBD). A player is ranked only against players who qualify in that
category -- a minimum volume that grows with the games played, so a four-game season does not rank a one-catch back
against a lead receiver -- and only when he qualifies himself. Percentiles are best-first whatever the stat's direction.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from sports_aggregator.cfb import derived_cache
from sports_aggregator.cfb.identity_links import position_group
from sports_aggregator.cfb.team_panels import SETTLED_GAMES
from sports_aggregator.nfl.ranking import rank_within
from sports_aggregator.ranked_panels import panel_row

#: category -> (label, qualifying stat, minimum per team game, ((label, stat_type, format, lower_is_better), ...))
CATEGORIES = (
    ("passing", "Passing", "ATT", 5.0, (
        ("Pass yards", "YDS", "int", False), ("Pass TD", "TD", "int", False), ("Interceptions", "INT", "int", True),
        ("Completion %", "PCT", "f1", False), ("Yards / attempt", "YPA", "f1", False), ("Attempts", "ATT", "int", False))),
    ("rushing", "Rushing", "CAR", 3.0, (
        ("Carries", "CAR", "int", False), ("Rush yards", "YDS", "int", False), ("Yards / carry", "YPC", "f1", False),
        ("Rush TD", "TD", "int", False), ("Longest", "LONG", "int", False))),
    ("receiving", "Receiving", "REC", 1.0, (
        ("Receptions", "REC", "int", False), ("Rec yards", "YDS", "int", False), ("Yards / catch", "YPR", "f1", False),
        ("Rec TD", "TD", "int", False), ("Longest", "LONG", "int", False))),
    ("defensive", "Defense", "TOT", 2.0, (
        ("Total tackles", "TOT", "int", False), ("Solo tackles", "SOLO", "int", False), ("Tackles for loss", "TFL", "f1", False),
        ("Sacks", "SACKS", "f1", False), ("QB hurries", "QB HUR", "int", False), ("Passes defended", "PD", "int", False))),
    ("interceptions", "Ball hawk", "INT", 0.0, (
        ("Interceptions", "INT", "int", False), ("Return yards", "YDS", "int", False))),
)
GROUP_LABELS = {"QB": "quarterbacks", "RB": "running backs", "REC": "receivers (WR/TE)", "OL": "offensive linemen",
                "DL": "defensive linemen", "LB": "linebackers", "DB": "defensive backs", "ST": "specialists"}
_QUERY = """
SELECT s.player_id, s.position, s.category, s.stat_type, s.numeric_value
  FROM player_season_stats s JOIN teams t ON t.school = s.team
 WHERE s.season=? AND s.numeric_value IS NOT NULL
"""


def _games(repository, season: int) -> int:
    """Typical (median) games played by an FBS team, so minimums scale through the season."""
    with repository._reader() as connection:
        counts = sorted(row[0] for row in connection.execute(
            """SELECT COUNT(*) FROM games g JOIN teams t ON t.school IN (g.home_team, g.away_team)
               WHERE g.season=? AND g.completed=1 AND g.home_points IS NOT NULL GROUP BY t.school""", (season,)))
    return counts[len(counts) // 2] if counts else 0


def _pool(repository, season: int) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        try:
            with repository._reader() as connection:
                rows = [dict(row) for row in connection.execute(_QUERY, (season,))]
            games = _games(repository, season)
        except sqlite3.OperationalError:
            return {"games": 0, "players": {}}
        players: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = players.setdefault(row["player_id"], {"group": position_group(row["position"]), "stats": {}})
            entry["group"] = entry["group"] or position_group(row["position"])
            entry["stats"].setdefault(row["category"], {})[row["stat_type"]] = row["numeric_value"]
        return {"games": games, "players": players}
    return derived_cache.derived(repository, "player_panel_pool", build, int(season))


def panel_season(repository, season: int) -> tuple[int, bool]:
    if _pool(repository, season)["games"] >= SETTLED_GAMES:
        return season, False
    return (season - 1, True) if _pool(repository, season - 1)["players"] else (season, False)


def player_panels(repository, season: int, player_id: str) -> dict[str, Any] | None:
    """Percentile groups for one player, or None when he has no qualifying stats that season."""
    used, baseline = panel_season(repository, season)
    pool = _pool(repository, used)
    me = pool["players"].get(str(player_id))
    if not me or not me["group"]:
        return None
    groups = []
    for key, label, qualifier, per_game, definitions in CATEGORIES:
        minimum = per_game * max(pool["games"], 1)

        def qualifies(stats: dict[str, Any]) -> bool:
            volume = stats.get(qualifier) or 0
            return volume > 0 and volume >= minimum
        own = me["stats"].get(key)
        if not own or not qualifies(own):
            continue
        peers = [{"id": pid, **entry["stats"][key]} for pid, entry in pool["players"].items()
                 if entry["group"] == me["group"] and key in entry["stats"] and qualifies(entry["stats"][key])]
        rows = []
        for row_label, stat, fmt, lower in definitions:
            value = own.get(stat)
            if value is None:
                continue
            ranked = rank_within(peers, id_key="id", value_key=stat, lower_is_better=lower).get(str(player_id), {})
            if ranked:
                rows.append(panel_row(row_label, stat, fmt, value, ranked["rank"], ranked["of"], False))
        if rows:
            groups.append({"label": label, "rows": rows})
    if not groups:
        return None
    return {"season": used, "baseline": baseline, "group": me["group"], "group_label": GROUP_LABELS[me["group"]],
            "games": pool["games"],
            "panel": {"season": used, "groups": groups, "has_data": True}}
