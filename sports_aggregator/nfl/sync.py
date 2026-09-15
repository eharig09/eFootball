"""Per-dataset nflverse synchronization into the canonical NFL database."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Callable

from sports_aggregator.nfl.models import Game, Player, SyncDatasetResult, SyncReport, Team
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.nflverse import NflverseClient
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.elo import build_elo


LOGGER = logging.getLogger(__name__)

# nflverse's seasonal roster release contains the most recent row for each
# player/team spell. These values describe a spell that has ended and should
# not be presented as part of that team's current organization.
TERMINAL_ROSTER_STATUSES = frozenset({"CUT", "RET", "TRD", "TRC"})

REQUIRED_COLUMNS = {
    "teams": {"team_abbr", "team_name", "team_nick", "team_conf", "team_division"},
    "games": {"game_id", "season", "game_type", "week", "gameday", "away_team", "home_team"},
    "players": {"season", "team", "full_name", "gsis_id"},
    "weekly_stats": {"season", "week", "season_type", "game_id", "player_id", "team", "opponent_team"},
    "snap_counts": {"season", "week", "game_id", "player", "team", "opponent",
                    "offense_snaps", "defense_snaps", "st_snaps"},
    "depth_charts": {"dt", "team", "player_name", "pos_slot", "pos_rank"},
    "player_master": {"gsis_id", "display_name", "pfr_id", "espn_id"},
    "player_ids": {"gsis_id", "name", "pfr_id", "sleeper_id"},
    "team_weekly": {"season", "week", "season_type", "team", "opponent_team"},
    "pbp": {"game_id", "season", "week", "posteam", "defteam", "epa", "success",
            "pass", "rush", "down", "yards_gained"},
}


def _records(dataset: str, frame) -> list[dict]:
    missing = REQUIRED_COLUMNS[dataset] - set(frame.columns)
    if missing:
        raise ValueError(f"nflverse {dataset} missing required columns {sorted(missing)}")
    return frame.to_dict("records")


class NFLDataSync:
    def __init__(self, client: NflverseClient, repository: NFLRepository) -> None:
        self.client = client
        self.repository = repository

    def sync_players(self, season: int, *, force: bool = False) -> int:
        """Replace a season roster with each player's current team spell.

        A player's ``week`` is the latest update to that row, not a complete
        team snapshot. Keep the newest player/team row, then remove terminal
        transaction states so cuts, retirements, and former trade rows do not
        inflate current rosters or movement counts.
        """
        self.repository.initialize()
        rows = _records("players", self.client.load_rosters([season], force=force))
        latest: dict[tuple[int, str, str], tuple[int, Player]] = {}
        for row in rows:
            try:
                player = Player.from_nflverse(row)
            except ValueError:
                continue  # unsigned rows can legitimately lack a GSIS id
            key = (player.season, player.player_id, player.team)
            week = int(row.get("week") or 0)
            if key not in latest or week >= latest[key][0]:
                latest[key] = (week, player)
        current = (
            item[1] for item in latest.values()
            if (item[1].status or "").upper() not in TERMINAL_ROSTER_STATUSES
        )
        return self.repository.replace_players(season, current)

    def sync(self, season: int, *, force: bool = False,
             include_pbp: bool = True) -> SyncReport:
        started_at = datetime.now(timezone.utc)
        self.repository.initialize()

        def store_teams() -> int:
            rows = _records("teams", self.client.load_teams(force=force))
            return self.repository.replace_teams(Team.from_nflverse(row) for row in rows)

        def store_games() -> int:
            rows = _records("games", self.client.load_schedules([season], force=force))
            return self.repository.replace_games(season, (Game.from_nflverse(row) for row in rows))

        def store_elo() -> int:
            rows = _records("games", self.client.load_schedules(range(2010, season + 1), force=force))
            return build_elo(self.repository, rows)

        def store_players() -> int:
            return self.sync_players(season, force=force)

        def store_weekly() -> int:
            rows = _records("weekly_stats", self.client.load_weekly([season], season_type="", force=force))
            for row in rows:
                row["team"] = canon_team(row.get("team"))
                row["opponent_team"] = canon_team(row.get("opponent_team"))
            return self.repository.replace_weekly_stats(season, rows)

        def store_snap_counts() -> int:
            rows = _records("snap_counts", self.client.load_snap_counts([season], force=force))
            return self.repository.replace_snap_counts(season, rows)

        def store_depth_charts() -> int:
            rows = _records("depth_charts", self.client.load_depth_charts([season], force=force))
            return self.repository.replace_depth_charts(season, rows)

        def store_player_master() -> int:
            return self.repository.replace_player_master(
                _records("player_master", self.client.load_player_master(force=force))
            )

        def store_player_ids() -> int:
            return self.repository.replace_player_id_crosswalk(
                _records("player_ids", self.client.load_player_ids(force=force))
            )

        def store_team_weekly() -> int:
            return self.repository.replace_team_weekly_stats(
                season, _records("team_weekly", self.client.load_team_weekly([season], force=force))
            )

        def store_pbp() -> int:
            rows = _records("pbp", self.client.load_pbp([season], force=force))
            efficiency = self.repository.replace_game_efficiency(season, rows)
            situational = self.repository.replace_game_situational(season, rows)
            playcalling = self.repository.replace_game_playcalling(season, rows)
            profiles = self.repository.replace_qb_pass_profiles(season, rows)
            receivers = self.repository.replace_receiver_pass_profiles(season, rows)
            rush_direction = self.repository.replace_rush_direction_profiles(season, rows)
            situational_pass = self.repository.replace_situational_pass_profiles(season, rows)
            rush_situational = self.repository.replace_rush_situational_profiles(season, rows)
            return (efficiency + situational + playcalling + profiles + receivers
                   + rush_direction + situational_pass + rush_situational)

        jobs: list[tuple[str, Callable[[], int]]] = [
            ("teams", store_teams), ("games", store_games), ("elo", store_elo),
            ("players", store_players), ("weekly_stats", store_weekly),
            ("snap_counts", store_snap_counts), ("depth_charts", store_depth_charts),
            ("player_master", store_player_master), ("player_ids", store_player_ids),
            ("team_weekly", store_team_weekly),
        ]
        if include_pbp:
            jobs.append(("pbp_efficiency", store_pbp))
        results: list[SyncDatasetResult] = []
        for name, job in jobs:
            try:
                count = job()
                results.append(SyncDatasetResult(name, count, "success"))
            except Exception as exc:
                results.append(SyncDatasetResult(name, 0, "failed", str(exc)))
                LOGGER.exception("NFL sync failed dataset=%s season=%s", name, season)

        report = SyncReport(season, started_at, datetime.now(timezone.utc), tuple(results))
        self.repository.record_sync(report)
        return report

    def sync_history(self, start_season: int = 2010, end_season: int = 2025, *,
                     force: bool = False, include_pbp: bool = True) -> dict[str, int]:
        """Backfill public history one season at a time to bound peak memory."""
        self.repository.initialize()
        seasons = range(int(start_season), int(end_season) + 1)
        schedules = _records("games", self.client.load_schedules(seasons, force=force))
        games_by_season: dict[int, list[dict]] = {}
        for row in schedules:
            games_by_season.setdefault(int(row["season"]), []).append(row)
        totals = {"seasons": 0, "games": 0, "weekly_metrics": 0,
                  "team_metrics": 0, "efficiency": 0, "situational": 0,
                  "playcalling": 0, "pass_zones": 0, "receiver_zones": 0,
                  "rush_direction": 0, "situational_pass": 0, "rush_situational": 0}
        for season in seasons:
            totals["seasons"] += 1
            game_rows = games_by_season.get(season, [])
            totals["games"] += self.repository.replace_games(
                season, (Game.from_nflverse(row) for row in game_rows)
            )
            weekly = _records(
                "weekly_stats", self.client.load_weekly([season], season_type="", force=force)
            )
            for row in weekly:
                row["team"] = canon_team(row.get("team"))
                row["opponent_team"] = canon_team(row.get("opponent_team"))
            totals["weekly_metrics"] += self.repository.replace_weekly_stats(season, weekly)
            team_rows = _records(
                "team_weekly", self.client.load_team_weekly([season], force=force)
            )
            totals["team_metrics"] += self.repository.replace_team_weekly_stats(season, team_rows)
            if include_pbp:
                pbp = _records("pbp", self.client.load_pbp([season], force=force))
                totals["efficiency"] += self.repository.replace_game_efficiency(season, pbp)
                totals["situational"] += self.repository.replace_game_situational(season, pbp)
                totals["playcalling"] += self.repository.replace_game_playcalling(season, pbp)
                totals["pass_zones"] += self.repository.replace_qb_pass_profiles(season, pbp)
                totals["receiver_zones"] += self.repository.replace_receiver_pass_profiles(season, pbp)
                totals["rush_direction"] += self.repository.replace_rush_direction_profiles(season, pbp)
                totals["situational_pass"] += self.repository.replace_situational_pass_profiles(season, pbp)
                totals["rush_situational"] += self.repository.replace_rush_situational_profiles(season, pbp)
        # Rebuild Elo once, after all canonical games are available.
        build_elo(self.repository, schedules)
        return totals
