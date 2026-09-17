"""Per-dataset nflverse synchronization into the canonical NFL database."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Callable

from sports_aggregator.nfl.models import Game, Player, SyncDatasetResult, SyncReport, Team, optional_float
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.nflverse import NflverseClient
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.elo import build_elo


LOGGER = logging.getLogger(__name__)

# nflverse's seasonal roster release contains the most recent row for each
# player/team spell. These values describe a spell that has ended and should
# not be presented as part of that team's current organization.
TERMINAL_ROSTER_STATUSES = frozenset({"CUT", "RET", "TRD", "TRC"})

#: Subprocess-sized groups for `sync()`'s `only` parameter -- together a
#: partition of every job name in `sync()`'s own `jobs` list, so each of
#: nfl-core's four steps (see bootstrap.py) covers a disjoint slice and the
#: union across all four is exactly what the old single "core" step did.
#: depth_charts gets its own group because it is, by a wide margin, the
#: single largest dataset (500k+ rows for a season); pbp_efficiency gets
#: its own because it is the only optional (include_pbp) job.
CORE_FOUNDATION = frozenset({"teams", "games", "elo", "players"})
CORE_STATS = frozenset({"weekly_stats", "next_gen_stats", "snap_counts",
                        "player_master", "player_ids", "team_weekly"})
CORE_DEPTH = frozenset({"depth_charts"})
CORE_PBP = frozenset({"pbp_efficiency"})

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
    "ngs_passing": {"season", "week", "season_type", "player_gsis_id", "team_abbr"},
    "ngs_rushing": {"season", "week", "season_type", "player_gsis_id", "team_abbr"},
    "ngs_receiving": {"season", "week", "season_type", "player_gsis_id", "team_abbr"},
}

# Next Gen Stats: (nflverse column, stored metric key, weight column within
# the same NGS row). Most NGS columns are already per-week averages, so a
# raw SUM() across a season is meaningless -- summing "avg time to throw"
# over 17 weeks does not produce a number anyone can read. Instead each one
# gets a weighted-sum companion (see _ngs_rows below) that a season query
# can SUM() alongside the weight's own already-summed total and divide back
# down, the same volume-weighted-ratio approach explorer.py already uses for
# PACR/RACR -- just generalized to a weight that is not literally "yards".
NGS_PASSING_RATE_METRICS = (
    ("avg_time_to_throw", "ngs_pass_time_to_throw", "attempts"),
    ("aggressiveness", "ngs_pass_aggressiveness", "attempts"),
    ("avg_intended_air_yards", "ngs_pass_intended_air_yards", "attempts"),
    ("avg_air_yards_to_sticks", "ngs_pass_air_yards_to_sticks", "attempts"),
    ("completion_percentage_above_expectation", "ngs_pass_cpoe", "attempts"),
    ("avg_completed_air_yards", "ngs_pass_completed_air_yards", "completions"),
)
NGS_RUSHING_RATE_METRICS = (
    ("efficiency", "ngs_rush_efficiency", "rush_attempts"),
    ("avg_time_to_los", "ngs_rush_time_to_los", "rush_attempts"),
    ("percent_attempts_gte_eight_defenders", "ngs_rush_stacked_box_pct", "rush_attempts"),
)
NGS_RECEIVING_RATE_METRICS = (
    ("avg_separation", "ngs_rec_separation", "targets"),
    ("avg_cushion", "ngs_rec_cushion", "targets"),
    ("percent_share_of_intended_air_yards", "ngs_rec_air_yards_share", "targets"),
)
#: Already a real per-week yards total (rush_yards minus expected_rush_yards
#: for that week's carries), not a rate -- sums across weeks like any other
#: yardage stat, no weighting needed.
NGS_RUSHING_SUM_METRICS = (("rush_yards_over_expected", "ngs_rush_yards_over_expected"),)


def _records(dataset: str, frame) -> list[dict]:
    missing = REQUIRED_COLUMNS[dataset] - set(frame.columns)
    if missing:
        raise ValueError(f"nflverse {dataset} missing required columns {sorted(missing)}")
    return frame.to_dict("records")


def _weekly_game_lookup(weekly_rows: list[dict]) -> dict[tuple[int, str, str], dict]:
    """(week, season_type, player_id) -> the game context Next Gen Stats
    itself does not carry, so its rows can still land in player_weekly_stats,
    whose primary key requires a game_id."""
    lookup: dict[tuple[int, str, str], dict] = {}
    for row in weekly_rows:
        player_id = str(row.get("player_id") or "").strip()
        if not player_id:
            continue
        key = (int(row["week"]), str(row.get("season_type") or "REG"), player_id)
        lookup[key] = {
            "game_id": str(row.get("game_id") or ""),
            "opponent_team": canon_team(row.get("opponent_team")),
            "player_name": row.get("player_display_name") or row.get("player_name"),
            "position": row.get("position"),
        }
    return lookup


def _ngs_rows(records: list[dict], game_lookup: dict[tuple[int, str, str], dict], *,
             rate_metrics: tuple[tuple[str, str, str], ...] = (),
             sum_metrics: tuple[tuple[str, str], ...] = (),
             extra: Callable[[dict], dict[str, float]] | None = None) -> list[dict]:
    rows: list[dict] = []
    for record in records:
        # week 0 is a season-to-date running total nflverse republishes
        # alongside each real week, not a game of its own.
        if int(record.get("week") or 0) == 0:
            continue
        player_id = str(record.get("player_gsis_id") or "").strip()
        if not player_id:
            continue
        season_type = str(record.get("season_type") or "REG")
        context = game_lookup.get((int(record["week"]), season_type, player_id))
        if context is None:
            continue
        row: dict[str, Any] = {
            "season": int(record["season"]), "week": int(record["week"]),
            "season_type": season_type, "game_id": context["game_id"],
            "player_id": player_id,
            "player_display_name": record.get("player_display_name") or context.get("player_name"),
            "team": canon_team(record.get("team_abbr")), "opponent_team": context["opponent_team"],
            "position": record.get("player_position") or context.get("position"),
        }
        for source_column, metric_key, weight_column in rate_metrics:
            value = optional_float(record.get(source_column))
            weight = optional_float(record.get(weight_column))
            if value is None or weight is None:
                continue
            row[metric_key] = value
            row[f"{metric_key}_wtd"] = value * weight
        for source_column, metric_key in sum_metrics:
            value = optional_float(record.get(source_column))
            if value is not None:
                row[metric_key] = value
        if extra:
            row.update(extra(record))
        rows.append(row)
    return rows


def _receiving_ngs_extra(record: dict) -> dict[str, float]:
    """YAC above expectation is a per-catch average in the source data;
    multiplying by receptions turns it into a real weekly yards total, the
    same shape as ngs_rush_yards_over_expected, so it can sum across weeks
    without needing a weighted-ratio companion."""
    above = optional_float(record.get("avg_yac_above_expectation"))
    receptions = optional_float(record.get("receptions"))
    if above is None or receptions is None:
        return {}
    return {"ngs_rec_yac_above_expectation": above * receptions}


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
             include_pbp: bool = True, only: frozenset[str] | None = None) -> SyncReport:
        """`only` scopes this call to a subset of dataset jobs by name (see
        the `jobs` list below), each its own subprocess-bounded refresh_cli
        segment in production -- a live memory-footprint investigation
        found the combined "run all 12 jobs in one process" step (the
        original, unscoped `sync()`) genuinely needs more virtual address
        space than one child process can safely be given on a 512MB
        instance, since pandas/pyarrow alone need a real baseline before
        any dataset-sized allocation. Splitting bounds each subprocess's
        peak to whatever its own group's largest dataset needs, not the
        cumulative total of all twelve run back to back."""
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

        def store_next_gen_stats() -> int:
            weekly_rows = _records(
                "weekly_stats", self.client.load_weekly([season], season_type="", force=force))
            for row in weekly_rows:
                row["team"] = canon_team(row.get("team"))
                row["opponent_team"] = canon_team(row.get("opponent_team"))
            lookup = _weekly_game_lookup(weekly_rows)
            total = self.repository.upsert_weekly_stats(_ngs_rows(
                _records("ngs_passing", self.client.load_ngs_passing([season], force=force)),
                lookup, rate_metrics=NGS_PASSING_RATE_METRICS,
            ))
            total += self.repository.upsert_weekly_stats(_ngs_rows(
                _records("ngs_rushing", self.client.load_ngs_rushing([season], force=force)),
                lookup, rate_metrics=NGS_RUSHING_RATE_METRICS, sum_metrics=NGS_RUSHING_SUM_METRICS,
            ))
            total += self.repository.upsert_weekly_stats(_ngs_rows(
                _records("ngs_receiving", self.client.load_ngs_receiving([season], force=force)),
                lookup, rate_metrics=NGS_RECEIVING_RATE_METRICS, extra=_receiving_ngs_extra,
            ))
            return total

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
            ("next_gen_stats", store_next_gen_stats),
            ("snap_counts", store_snap_counts), ("depth_charts", store_depth_charts),
            ("player_master", store_player_master), ("player_ids", store_player_ids),
            ("team_weekly", store_team_weekly),
        ]
        if include_pbp:
            jobs.append(("pbp_efficiency", store_pbp))
        if only is not None:
            jobs = [(name, job) for name, job in jobs if name in only]
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
        totals = {"seasons": 0, "games": 0, "weekly_metrics": 0, "next_gen_stats": 0,
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
            lookup = _weekly_game_lookup(weekly)
            totals["next_gen_stats"] += self.repository.upsert_weekly_stats(_ngs_rows(
                _records("ngs_passing", self.client.load_ngs_passing([season], force=force)),
                lookup, rate_metrics=NGS_PASSING_RATE_METRICS,
            ))
            totals["next_gen_stats"] += self.repository.upsert_weekly_stats(_ngs_rows(
                _records("ngs_rushing", self.client.load_ngs_rushing([season], force=force)),
                lookup, rate_metrics=NGS_RUSHING_RATE_METRICS, sum_metrics=NGS_RUSHING_SUM_METRICS,
            ))
            totals["next_gen_stats"] += self.repository.upsert_weekly_stats(_ngs_rows(
                _records("ngs_receiving", self.client.load_ngs_receiving([season], force=force)),
                lookup, rate_metrics=NGS_RECEIVING_RATE_METRICS, extra=_receiving_ngs_extra,
            ))
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
