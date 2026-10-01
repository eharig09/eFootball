"""Team discipline: penalties committed and drawn, with league ranks (1 = best for the team).

Built from `nfl_penalties` (enforced flags only, regular season). A flag a team commits is `committed`; the same
flag seen from the other bench is `drawn` -- "opponent gifts". EPA is the flagged team's own EPA on no-play flags,
where the penalty is the whole play, so a gift is the negative of the offender's number.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository

#: Flags that happen before the snap (the offense or defense lined up wrong or ran the clock).
PRESNAP_TYPES = frozenset({
    "False Start", "Delay of Game", "Defensive Offside", "Offside", "Neutral Zone Infraction", "Encroachment",
    "Illegal Formation", "Illegal Shift", "Illegal Motion", "Defensive Delay of Game", "Too Many Men on Field",
    "Illegal Substitution", "Offside on Free Kick", "Ineligible Downfield Pass",
})
TOP_TYPES = 6
TOP_OFFENDERS = 5

#: metric -> (label, value key, format, higher_is_better) for the two panels. Ranks put the best team first.
COMMITTED_METRICS = (
    ("Flags", "flags", "int", False), ("Yards", "yards", "int", False), ("Flags / game", "flags_pg", "f1", False),
    ("Auto first downs", "auto_first_downs", "int", False), ("Pre-snap flags", "presnap", "int", False),
    ("EPA cost (no-play)", "epa", "signed2", True),
)
DRAWN_METRICS = (
    ("Flags", "flags", "int", True), ("Yards", "yards", "int", True), ("Flags / game", "flags_pg", "f1", True),
    ("Auto first downs", "auto_first_downs", "int", True), ("Pre-snap flags", "presnap", "int", True),
    ("EPA gift (no-play)", "epa", "signed2", True),
)


def _side(rows: list[dict[str, Any]], games: int, *, drawn: bool) -> dict[str, Any]:
    flags = len(rows)
    yards = sum(row["yards"] for row in rows)
    epas = [row["epa_team"] for row in rows if row.get("epa_team") is not None]
    epa = sum(epas) * (-1 if drawn else 1)
    types = Counter(row["penalty_type"] for row in rows)
    type_yards: dict[str, float] = defaultdict(float)
    for row in rows:
        type_yards[row["penalty_type"]] += row["yards"]
    offenders: dict[str, dict[str, Any]] = {}
    if not drawn:
        for row in rows:
            key = row.get("player_id") or row.get("player_name")
            if not key:
                continue
            entry = offenders.setdefault(key, {"player_id": row.get("player_id"), "player_name": row.get("player_name"),
                                               "flags": 0, "yards": 0.0, "first_downs": 0})
            entry["flags"] += 1
            entry["yards"] += row["yards"]
            entry["first_downs"] += row["auto_first_down"] or 0
    return {
        "flags": flags, "yards": yards, "flags_pg": flags / games if games else None,
        "yards_pg": yards / games if games else None,
        "auto_first_downs": sum(row["auto_first_down"] or 0 for row in rows),
        "presnap": sum(1 for row in rows if row["penalty_type"] in PRESNAP_TYPES),
        "epa": epa, "epa_flags": len(epas), "no_play_flags": sum(1 for row in rows if row["no_play"]),
        "types": [{"type": name, "count": count, "yards": type_yards[name], "share": count / flags}
                  for name, count in types.most_common(TOP_TYPES)] if flags else [],
        "offenders": sorted(offenders.values(), key=lambda item: (-item["flags"], -item["yards"], item["player_name"] or ""))[:TOP_OFFENDERS],
    }


def _rank(values: dict[str, float | None], *, higher_is_better: bool) -> dict[str, int]:
    scored = sorted(((team, value) for team, value in values.items() if value is not None),
                    key=lambda item: item[1], reverse=higher_is_better)
    return {team: position for position, (team, _) in enumerate(scored, 1)}


def league_penalties(repository: NFLRepository, season: int, *, before_week: int | None = None) -> dict[str, dict[str, Any]]:
    """Profile of every team that has played, keyed by team code. Empty when the season has no stored penalties."""
    return repository.memo(("league_penalties", int(season), before_week), lambda: _build(repository, season, before_week))


def _build(repository: NFLRepository, season: int, before_week: int | None) -> dict[str, dict[str, Any]]:
    penalties = [row for row in repository.penalties(season)
                 if before_week is None or (row["week"] or 0) < before_week]
    if not penalties:
        return {}
    games_by_team: dict[str, set[str]] = defaultdict(set)
    for game in repository.schedule(season):
        if not game.get("completed") or game.get("season_type", "REG") != "REG":
            continue
        if before_week is not None and (game.get("week") or 0) >= before_week:
            continue
        games_by_team[game["away_team"]].add(game["game_id"])
        games_by_team[game["home_team"]].add(game["game_id"])
    committed: dict[str, list] = defaultdict(list)
    drawn: dict[str, list] = defaultdict(list)
    for row in penalties:
        committed[row["team"]].append(row)
        drawn[row["opponent"]].append(row)
    teams = sorted(set(games_by_team) | set(committed) | set(drawn))
    profiles: dict[str, dict[str, Any]] = {}
    for team in teams:
        games = len(games_by_team.get(team, ()))
        own, theirs = _side(committed.get(team, []), games, drawn=False), _side(drawn.get(team, []), games, drawn=True)
        profiles[team] = {
            "team": team, "games": games, "season": int(season), "committed": own, "drawn": theirs,
            "net_yards": theirs["yards"] - own["yards"], "net_flags": theirs["flags"] - own["flags"],
            "net_yards_pg": ((theirs["yards"] - own["yards"]) / games) if games else None,
        }
    ranks: dict[str, dict[str, dict[str, int]]] = {"committed": {}, "drawn": {}}
    for side, metrics in (("committed", COMMITTED_METRICS), ("drawn", DRAWN_METRICS)):
        for _, key, _, higher in metrics:
            ranks[side][key] = _rank({team: profile[side][key] for team, profile in profiles.items()}, higher_is_better=higher)
    ranks["net_yards_pg"] = _rank({team: profile["net_yards_pg"] for team, profile in profiles.items()}, higher_is_better=True)
    maxes = {side: {key: max((abs(profile[side][key] or 0) for profile in profiles.values()), default=0) or 1
                    for _, key, _, _ in metrics}
             for side, metrics in (("committed", COMMITTED_METRICS), ("drawn", DRAWN_METRICS))}
    for team, profile in profiles.items():
        profile["maxes"] = maxes
        profile["ranks"] = {"committed": {key: table.get(team) for key, table in ranks["committed"].items()},
                            "drawn": {key: table.get(team) for key, table in ranks["drawn"].items()},
                            "net_yards_pg": ranks["net_yards_pg"].get(team)}
        profile["of"] = len(profiles)
    return profiles


def team_penalties(repository: NFLRepository, season: int, team: str, *,
                   before_week: int | None = None) -> dict[str, Any] | None:
    """One team's discipline profile with league ranks, or None when nothing is stored for it."""
    profile = league_penalties(repository, season, before_week=before_week).get(team)
    if not profile or not profile["games"]:
        return None
    return profile


def panel_rows(profile: dict[str, Any], side: str) -> list[dict[str, Any]]:
    """Display rows for one side's panel: label, value, format, rank and a 0-100 bar length (rank-based)."""
    metrics = COMMITTED_METRICS if side == "committed" else DRAWN_METRICS
    total = profile["of"] or 32
    rows = []
    for label, key, fmt, _ in metrics:
        rank = profile["ranks"][side].get(key)
        rows.append({"label": label, "value": profile[side][key], "format": fmt, "rank": rank, "of": total,
                     "bar": round(abs(profile[side][key] or 0) / profile["maxes"][side][key] * 100),
                     "good": bool(rank and rank <= total / 3), "poor": bool(rank and rank > total * 2 / 3)})
    return rows
