"""Current and returning player workload context for NFL pages."""

from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.repository import NFLRepository


def team_usage_context(repository: NFLRepository, season: int, team: str, *,
                       before_week: int | None = None,
                       preferred_season: int | None = None) -> dict[str, Any]:
    """Return workload rows, falling back to current-roster players last year."""
    stats_season = preferred_season or season
    rows = repository.team_player_usage(
        stats_season, team,
        before_week=before_week if stats_season == season else None,
    )
    current_ids = {row["player_id"] for row in repository.team_roster(season, team)}
    rows = [row for row in rows if row["player_id"] in current_ids]
    if not any(row["opportunities"] for row in rows) and stats_season == season:
        stats_season = season - 1
        rows = [row for row in repository.team_player_usage(stats_season, team)
                if row["player_id"] in current_ids]
    for row in rows:
        row["stat_season"] = stats_season
        row["player_url"] = f"/nfl/players/{row['player_id']}/?season={stats_season}"
    active = [row for row in rows if row["opportunities"] > 0]
    return {
        "season": stats_season,
        "rows": active,
        "returning_target_share": sum(row["target_share"] or 0 for row in active),
        "returning_carry_share": sum(row["carry_share"] or 0 for row in active),
        "returning_opportunity_share": sum(
            row["opportunity_share"] or 0 for row in active
        ),
        "target_leaders": sorted(
            (row for row in active if row["targets"] > 0),
            key=lambda row: (-row["target_share"], -row["targets"]),
        )[:4],
        "carry_leaders": sorted(
            (row for row in active if row["carries"] > 0),
            key=lambda row: (-row["carry_share"], -row["carries"]),
        )[:4],
    }


def returning_player_usage(repository: NFLRepository, season: int,
                           player_id: str) -> dict[str, Any] | None:
    """Find a player's workload in the requested or immediately prior season."""
    player = repository.get_player(season, player_id)
    if player is None:
        return None
    for stats_season in (season, season - 1):
        identity = repository.get_player(stats_season, player_id)
        if identity is None:
            continue
        for team in identity.get("teams", []):
            row = next((item for item in repository.team_player_usage(stats_season, team)
                        if item["player_id"] == player_id), None)
            if row and row["opportunities"] > 0:
                return {**row, "team": team, "season": stats_season,
                        "is_baseline": stats_season != season}
    return None
