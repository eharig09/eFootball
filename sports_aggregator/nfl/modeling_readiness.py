"""NFL Football Lab modeling-readiness audit.

Reports actual populated historical coverage by season for the tables needed by
projection research. Schema existence is not treated as data availability.
"""
from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.repository import NFLRepository


TABLES = {
    "games": ("games", "season"),
    "efficiency": ("game_team_efficiency", "season"),
    "situational": ("game_team_situational", "season"),
    "playcalling": ("game_team_playcalling", "season"),
    "team_weekly": ("team_weekly_stats", "season"),
    "player_weekly": ("player_weekly_stats", "season"),
    "snaps": ("snap_counts", "season"),
    "depth_charts": ("depth_chart_snapshots", "season"),
    "injuries": ("injury_reports", "season"),
    "elo_games": ("nfl_elo_games", "season"),
    "weather": ("nfl_game_weather", None),
    "qb_profiles": ("qb_pass_profiles", "season"),
    "receiver_profiles": ("receiver_pass_profiles", "season"),
    "rush_profiles": ("rush_direction_profiles", "season"),
    "pff_metrics": ("nfl_pff_player_metrics", "season"),
}


def _exists(connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def audit(repository: NFLRepository, *, from_season: int = 2010,
          to_season: int = 2026) -> dict[str, Any]:
    repository.initialize()
    seasons = list(range(int(from_season), int(to_season) + 1))
    with repository._connect() as connection:
        existing = {name: _exists(connection, table) for name, (table, _) in TABLES.items()}
        by_season = []
        for season in seasons:
            row: dict[str, Any] = {"season": season}
            for name, (table, season_col) in TABLES.items():
                if not existing[name]:
                    row[name] = None
                    continue
                if season_col is None:
                    row[name] = None
                    continue
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {season_col}=?",
                    (season,),
                ).fetchone()[0]
                row[name] = int(count)

            if existing["games"]:
                games = connection.execute(
                    """SELECT COUNT(*) AS n,
                              SUM(CASE WHEN completed=1 THEN 1 ELSE 0 END) AS completed,
                              SUM(CASE WHEN spread_line IS NOT NULL THEN 1 ELSE 0 END) AS spread,
                              SUM(CASE WHEN total_line IS NOT NULL THEN 1 ELSE 0 END) AS total,
                              SUM(CASE WHEN away_spread_odds IS NOT NULL
                                        AND home_spread_odds IS NOT NULL THEN 1 ELSE 0 END) AS spread_prices,
                              SUM(CASE WHEN away_rest IS NOT NULL
                                        AND home_rest IS NOT NULL THEN 1 ELSE 0 END) AS rest
                       FROM games WHERE season=?""",
                    (season,),
                ).fetchone()
                row["game_rows"] = int(games["n"] or 0)
                row["completed_games"] = int(games["completed"] or 0)
                row["games_with_spread"] = int(games["spread"] or 0)
                row["games_with_total"] = int(games["total"] or 0)
                row["games_with_spread_prices"] = int(games["spread_prices"] or 0)
                row["games_with_rest"] = int(games["rest"] or 0)

            # Core modeling intersection: both team situational + efficiency rows.
            if existing["games"] and existing["situational"] and existing["efficiency"]:
                core = connection.execute(
                    """SELECT COUNT(*) FROM games g
                       WHERE g.season=? AND g.completed=1
                         AND EXISTS(
                           SELECT 1 FROM game_team_situational s
                           WHERE s.game_id=g.game_id AND s.team=g.home_team)
                         AND EXISTS(
                           SELECT 1 FROM game_team_situational s
                           WHERE s.game_id=g.game_id AND s.team=g.away_team)
                         AND EXISTS(
                           SELECT 1 FROM game_team_efficiency e
                           WHERE e.game_id=g.game_id AND e.team=g.home_team)
                         AND EXISTS(
                           SELECT 1 FROM game_team_efficiency e
                           WHERE e.game_id=g.game_id AND e.team=g.away_team)""",
                    (season,),
                ).fetchone()[0]
                row["core_projection_games"] = int(core)

            completed = int(row.get("completed_games") or 0)
            core = int(row.get("core_projection_games") or 0)
            row["core_projection_coverage"] = (
                round(core / completed, 4) if completed else None
            )
            by_season.append(row)

        weather_count = (
            int(connection.execute("SELECT COUNT(*) FROM nfl_game_weather").fetchone()[0])
            if existing["weather"] else None
        )

    usable = [
        r["season"] for r in by_season
        if (r.get("core_projection_games") or 0) >= 200
        and (r.get("core_projection_coverage") or 0) >= 0.90
    ]
    return {
        "version": "nfl-modeling-readiness-v1",
        "from_season": int(from_season),
        "to_season": int(to_season),
        "tables_present": existing,
        "weather_rows": weather_count,
        "by_season": by_season,
        "recommended_core_window": {
            "from_season": min(usable) if usable else None,
            "to_season": max(usable) if usable else None,
            "criterion": ">=200 completed core-projection games and >=90% efficiency+situational coverage",
        },
    }
