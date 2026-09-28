"""Small, read-only integrity audit for the current NFL season.

The refresh jobs intentionally stay split into memory-bounded processes.  This
audit is the contract between them: a schedule row can be final before the
weekly-stat or play-by-play releases arrive, but the lag is made explicit
instead of silently presenting an empty postgame page.
"""

from __future__ import annotations

from contextlib import closing
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository


def season_coverage(repository: NFLRepository, season: int) -> dict[str, Any]:
    repository.initialize()
    with closing(repository._connect()) as connection:
        completed = {
            str(row["game_id"]): int(row["week"])
            for row in connection.execute(
                "SELECT game_id,week FROM games WHERE season=? AND completed=1",
                (int(season),),
            )
        }
        stat_games = {
            str(row[0]) for row in connection.execute(
                "SELECT DISTINCT game_id FROM player_weekly_stats WHERE season=?",
                (int(season),),
            )
        }
        pbp_games = {
            str(row[0]) for row in connection.execute(
                "SELECT DISTINCT game_id FROM game_team_efficiency WHERE season=?",
                (int(season),),
            )
        }
        orphan_stats = int(connection.execute(
            """SELECT COUNT(DISTINCT p.game_id)
               FROM player_weekly_stats p
               LEFT JOIN games g ON g.game_id=p.game_id
               WHERE p.season=? AND g.game_id IS NULL""",
            (int(season),),
        ).fetchone()[0])

    completed_ids = set(completed)
    missing_stats = sorted(completed_ids - stat_games)
    missing_pbp = sorted(completed_ids - pbp_games)
    postgame_ready = completed_ids & stat_games & pbp_games
    latest_completed_week = max(completed.values(), default=None)

    return {
        "season": int(season),
        "completed_games": len(completed_ids),
        "stat_game_logs": len(completed_ids & stat_games),
        "pbp_game_logs": len(completed_ids & pbp_games),
        "postgame_ready": len(postgame_ready),
        "latest_completed_week": latest_completed_week,
        "latest_stat_week": max((completed[game_id] for game_id in completed_ids & stat_games), default=None),
        "latest_pbp_week": max((completed[game_id] for game_id in completed_ids & pbp_games), default=None),
        "missing_stats": missing_stats,
        "missing_pbp": missing_pbp,
        "orphan_stat_games": orphan_stats,
        "healthy": not missing_stats and not missing_pbp and not orphan_stats,
    }
