"""Team-page intelligence assembled from canonical NFL repository data."""

from __future__ import annotations

from collections import Counter
from typing import Any

from sports_aggregator.nfl.ranking import rank_within
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.usage import team_usage_context


OFFENSE = {"QB", "RB", "FB", "WR", "TE", "OT", "T", "OG", "G", "C", "OL"}
DEFENSE = {"DE", "DT", "DL", "NT", "EDGE", "LB", "ILB", "OLB", "CB", "DB", "S", "FS", "SS"}
UNIT_GROUPS = (
    ("Quarterbacks", {"QB"}),
    ("Backfield", {"RB", "FB"}),
    ("Receivers", {"WR", "TE"}),
    ("Offensive line", {"OT", "T", "OG", "G", "C", "OL"}),
    ("Defensive front", {"DE", "DT", "DL", "NT", "EDGE"}),
    ("Linebackers", {"LB", "ILB", "OLB"}),
    ("Secondary", {"CB", "DB", "S", "FS", "SS"}),
    ("Specialists", {"K", "PK", "P", "LS"}),
)


def unit_continuity(repository: NFLRepository, season: int, team: str) -> list[dict[str, Any]]:
    """Measure retained prior-season participation within football position units."""
    current = repository.team_roster(season, team)
    prior = repository.team_roster(season - 1, team)
    prior_snaps = {row["player_id"]: row for row in repository.team_snap_leaders(season - 1, team)
                   if row.get("player_id")}
    prior_depth_starters = {row["gsis_id"] for row in repository.current_depth_chart(season - 1, team)
                            if row.get("gsis_id") and row.get("position_rank") == 1}

    def participation(player_id: str) -> float:
        snap = prior_snaps.get(player_id, {})
        share = max(snap.get("offense_pct") or 0, snap.get("defense_pct") or 0)
        games = snap.get("games") or 0
        # Equivalent starts: a full-game share contributes one start. The
        # depth-chart fallback is used only when no snap stream is available.
        return games * share if games else (1.0 if player_id in prior_depth_starters else 0.0)

    output = []
    for label, positions in UNIT_GROUPS:
        current_ids = {row["player_id"] for row in current if row.get("position") in positions}
        prior_ids = {row["player_id"] for row in prior if row.get("position") in positions}
        returning = current_ids & prior_ids
        prior_participation = sum(participation(player_id) for player_id in prior_ids)
        returning_participation = sum(participation(player_id) for player_id in returning)
        output.append({
            "unit": label, "current": len(current_ids), "prior": len(prior_ids),
            "returning": len(returning), "additions": len(current_ids - prior_ids),
            "departures": len(prior_ids - current_ids),
            "prior_participation": round(prior_participation, 1),
            "returning_participation": round(returning_participation, 1),
            "retention": (returning_participation / prior_participation
                          if prior_participation else (0.0 if prior_ids and not returning else None)),
        })
    return output


def _game_card(game: dict[str, Any], team: str, identities: dict[str, dict]) -> dict[str, Any]:
    away = game["away_team"] == team
    opponent = game["home_team"] if away else game["away_team"]
    own = game["away_score"] if away else game["home_score"]
    other = game["home_score"] if away else game["away_score"]
    result = None
    if game["completed"]:
        result = "W" if own > other else ("L" if own < other else "T")
    return {
        **game, "opponent": opponent, "opponent_identity": identities.get(opponent, {}),
        "site": "at" if away else "vs", "own_score": own, "opponent_score": other,
        "result": result, "url": f"/nfl/games/{game['game_id']}/",
    }


def _split(games: list[dict], team: str, label: str, predicate) -> dict[str, Any]:
    selected = [game for game in games if game["completed"] and predicate(game)]
    wins = losses = ties = points_for = points_against = 0
    for game in selected:
        away = game["away_team"] == team
        own = game["away_score"] if away else game["home_score"]
        other = game["home_score"] if away else game["away_score"]
        points_for += own; points_against += other
        if own > other: wins += 1
        elif own < other: losses += 1
        else: ties += 1
    return {"label": label, "wins": wins, "losses": losses, "ties": ties,
            "record": f"{wins}-{losses}" + (f"-{ties}" if ties else ""),
            "point_diff": points_for - points_against}


def team_context(repository: NFLRepository, season: int, team: str) -> dict[str, Any]:
    identities = {row["abbreviation"]: row for row in repository.list_teams()}
    games = repository.schedule(season, team=team)
    cards = [_game_card(game, team, identities) for game in games]
    completed = [game for game in cards if game["completed"]]
    upcoming = [game for game in cards if not game["completed"]]
    roster = repository.team_roster(season, team)
    positions = Counter((row.get("position") or "Other") for row in roster)
    status = Counter((row.get("status") or "Unknown") for row in roster)
    experienced = [row["years_experience"] for row in roster if row.get("years_experience") is not None]

    performance_season = season
    league = repository.league_efficiency(performance_season)
    profile = next((row for row in league if row["team"] == team), None)
    if profile is None:
        performance_season = season - 1
        league = repository.league_efficiency(performance_season)
        profile = next((row for row in league if row["team"] == team), None)
    rank_keys = ("net_epa", "epa_per_play", "pass_epa_per_play", "rush_epa_per_play",
                "defensive_epa_allowed")
    lower_is_better = frozenset({"defensive_epa_allowed"})
    ranks = {
        key: rank_within(league, id_key="team", value_key=key,
                         lower_is_better=key in lower_is_better).get(team, {}).get("rank")
        for key in rank_keys
    }

    production = repository.team_season_summary(season, team)
    production_season = season
    if not production["games"]:
        production_season = season - 1
        production = repository.team_season_summary(production_season, team)

    leaders = []
    current_ids = {row["player_id"] for row in roster}
    for category, metric, value_label in (
        ("Passing", "passing_yards", "yards"), ("Rushing", "rushing_yards", "yards"),
        ("Receiving", "receiving_yards", "yards"), ("Pressure", "def_sacks", "sacks"),
    ):
        leader_season = season
        rows = repository.player_leaders(leader_season, metric, team=team, limit=1)
        if not rows:
            leader_season = season - 1
            rows = [row for row in repository.player_leaders(
                leader_season, metric, team=team, limit=100,
            ) if row["player_id"] in current_ids][:1]
        if rows:
            leaders.append({**rows[0], "category": category, "value_label": value_label,
                            "stat_season": leader_season,
                            "url": f"/nfl/players/{rows[0]['player_id']}/?season={leader_season}"})

    return {
        "recent_games": list(reversed(completed[-3:])), "upcoming_games": upcoming[:3],
        "next_game": upcoming[0] if upcoming else None,
        "last_game": completed[-1] if completed else None,
        "record_splits": (
            _split(games, team, "Home", lambda game: game["home_team"] == team),
            _split(games, team, "Away", lambda game: game["away_team"] == team),
            _split(games, team, "Division", lambda game: bool(game["division_game"])),
        ),
        "production": production, "production_season": production_season,
        "efficiency": profile, "performance_season": performance_season,
        "efficiency_ranks": ranks, "leaders": leaders,
        "unit_continuity": unit_continuity(repository, season, team),
        "usage": team_usage_context(
            repository, season, team, preferred_season=production_season,
        ),
        "roster_summary": {
            "players": len(roster),
            "offense": sum(count for position, count in positions.items() if position in OFFENSE),
            "defense": sum(count for position, count in positions.items() if position in DEFENSE),
            "specialists": sum(count for position, count in positions.items()
                               if position not in OFFENSE | DEFENSE),
            "average_experience": sum(experienced) / len(experienced) if experienced else None,
            "positions": dict(positions), "statuses": dict(status),
        },
    }
