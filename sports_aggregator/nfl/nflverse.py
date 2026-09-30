"""nflverse release-asset client with season-aware, atomic disk caching.

This is a deliberate port of the proven adapter in the sibling
``scouting_report`` project. It owns only transport and caching; normalization
and persistence belong to the NFL sync and repository modules.
"""

from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
import time
from typing import Iterable

import requests


RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"
CURRENT_SEASON_TTL = 6 * 3600
CURRENT_ASSET_TTLS = {
    # Scores and lines are tiny and time-sensitive. The larger analytical
    # releases update less often, but should still be rechecked during the
    # same football afternoon instead of waiting six hours.
    "schedules": 15 * 60,
    "weekly": 2 * 3600,
    "team_weekly": 2 * 3600,
    "snap_counts": 2 * 3600,
    "pbp": 2 * 3600,
    "participation": 2 * 3600,
    "ftn": 2 * 3600,
    "ngs_passing": 2 * 3600,
    "ngs_rushing": 2 * 3600,
    "ngs_receiving": 2 * 3600,
}

# asset -> (release tag, filename template). A template without {season} is a
# live, league-wide file and therefore always uses the current-data TTL.
ASSETS = {
    "teams": ("teams", "teams_colors_logos.parquet"),
    "weekly": ("stats_player", "stats_player_week_{season}.parquet"),
    "schedules": ("schedules", "games.parquet"),
    "snap_counts": ("snap_counts", "snap_counts_{season}.parquet"),
    "rosters": ("rosters", "roster_{season}.parquet"),
    "injuries": ("injuries", "injuries_{season}.parquet"),
    "depth_charts": ("depth_charts", "depth_charts_{season}.parquet"),
    "player_master": ("players", "players.parquet"),
    "team_weekly": ("stats_team", "stats_team_week_{season}.parquet"),
    "pbp": ("pbp", "play_by_play_{season}.parquet"),
    # Enhanced play-level releases. Both join to pbp on (game id, play id).
    # Participation lags the season (404 until nflverse publishes it) and FTN
    # charting starts in 2022; a missing file is an empty frame, not an error.
    "participation": ("pbp_participation", "pbp_participation_{season}.parquet"),
    "ftn": ("ftn_charting", "ftn_charting_{season}.parquet"),
    # These three are one continuously-updated file each, covering every
    # season since 2016 -- not one release per season like the assets above.
    "ngs_passing": ("nextgen_stats", "ngs_passing.parquet"),
    "ngs_rushing": ("nextgen_stats", "ngs_rushing.parquet"),
    "ngs_receiving": ("nextgen_stats", "ngs_receiving.parquet"),
}
PLAYER_IDS_URL = "https://github.com/dynastyprocess/data/raw/master/files/db_playerids.csv"
PBP_ANALYTICS_COLUMNS = (
    "game_id", "season", "week", "posteam", "defteam", "epa", "success", "pass", "rush",
    "qb_epa", "down", "yards_gained", "pass_attempt", "air_yards", "passer_player_id",
    "passer_player_name", "pass_location", "complete_pass", "passing_yards",
    "pass_touchdown", "interception", "cpoe", "receiver_player_id", "receiver_player_name",
    "receiving_yards", "yards_after_catch", "third_down_converted", "yardline_100",
    "score_differential", "qtr", "drive", "game_seconds_remaining",
    "shotgun", "no_huddle", "run_location", "run_gap", "rusher_player_id",
    "rusher_player_name", "rush_touchdown",
    "solo_tackle_1_player_id", "solo_tackle_1_player_name",
    "tackle_with_assist_1_player_id", "tackle_with_assist_1_player_name",
    "pass_defense_1_player_id", "pass_defense_1_player_name",
    "interception_player_id", "interception_player_name",
    "sack_player_id", "sack_player_name",
)


#: Columns kept from the full 372-column play-by-play file for the stored play table.
PLAY_DETAIL_COLUMNS = (
    "game_id", "play_id", "season", "week", "season_type", "qtr", "time", "game_seconds_remaining",
    "drive", "fixed_drive", "fixed_drive_result", "posteam", "defteam", "home_team", "away_team",
    "posteam_type", "down", "ydstogo", "yardline_100", "yrdln", "desc", "play_type", "pass", "rush",
    "sack", "interception", "fumble_lost", "touchdown", "penalty", "yards_gained", "epa", "success",
    "wp", "home_wp", "wpa", "score_differential", "total_home_score", "total_away_score",
    "air_yards", "pass_location", "run_location", "run_gap", "shotgun", "no_huddle", "qb_dropback",
    "play_deleted", "passer_player_id", "receiver_player_id", "rusher_player_id", "complete_pass",
    "yards_after_catch", "cpoe", "qb_scramble", "goal_to_go", "pass_touchdown", "rush_touchdown",
    "first_down",
)
PARTICIPATION_COLUMNS = (
    "nflverse_game_id", "play_id", "offense_formation", "offense_personnel", "defense_personnel",
    "defenders_in_box", "number_of_pass_rushers", "time_to_throw", "was_pressure",
    "defense_man_zone_type", "defense_coverage_type", "route",
    "offense_players", "offense_positions", "defense_players", "defense_positions",
)
FTN_COLUMNS = (
    "nflverse_game_id", "nflverse_play_id", "starting_hash", "qb_location", "is_no_huddle",
    "is_motion", "is_play_action", "is_screen_pass", "is_rpo", "is_trick_play",
    "is_qb_out_of_pocket", "is_interception_worthy", "is_throw_away", "read_thrown",
    "is_catchable_ball", "is_contested_ball", "is_created_reception", "is_drop",
    "n_blitzers", "n_pass_rushers",
)


class NflverseError(RuntimeError):
    """Raised when nflverse data cannot be fetched or decoded."""


def current_season(today: date | datetime | None = None) -> int:
    today = today or datetime.now()
    return today.year if today.month >= 3 else today.year - 1


def _pandas():
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - deployment/configuration guard
        raise NflverseError(
            "NFL data sync requires pandas and pyarrow; install requirements.txt"
        ) from exc
    return pd


class NflverseClient:
    """Read nflverse parquet releases through a project-owned raw cache."""

    def __init__(
        self,
        cache_path: str | os.PathLike[str],
        *,
        session: requests.Session | None = None,
        ttl_seconds: int = CURRENT_SEASON_TTL,
        asset_ttls: dict[str, int] | None = None,
        clock=time.time,
        today=None,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.session = session or requests.Session()
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.asset_ttls = dict(CURRENT_ASSET_TTLS if asset_ttls is None else asset_ttls)
        self.clock = clock
        self.today = today

    def _path(self, asset: str, season: int | None) -> Path:
        name = f"{asset}.parquet" if season is None else f"{asset}_{season}.parquet"
        return self.cache_path / name

    def _is_fresh(self, path: Path, asset: str, season: int | None) -> bool:
        if not path.exists():
            return False
        if season is not None and season < current_season(self.today) and asset != "schedules":
            return True
        ttl = max(0, int(self.asset_ttls.get(asset, self.ttl_seconds)))
        return self.clock() - path.stat().st_mtime < ttl

    def frame(self, asset: str, season: int | None = None, *, force: bool = False,
              columns: Iterable[str] | None = None):
        if asset not in ASSETS:
            raise NflverseError(f"unknown asset {asset!r}; have {sorted(ASSETS)}")
        tag, template = ASSETS[asset]
        if "{season}" in template and season is None:
            raise NflverseError(f"asset {asset!r} is published per season; pass one")

        path = self._path(asset, season)
        if not force and self._is_fresh(path, asset, season):
            return _pandas().read_parquet(path, columns=list(columns) if columns else None)

        url = f"{RELEASE_BASE}/{tag}/{template.format(season=season)}"
        try:
            response = self.session.get(url, timeout=120)
        except requests.RequestException as exc:
            if path.exists():
                return _pandas().read_parquet(path, columns=list(columns) if columns else None)
            raise NflverseError(f"could not reach nflverse for {asset} {season or ''}: {exc}") from exc

        if response.status_code == 404:
            return _pandas().DataFrame()
        if not response.ok:
            if path.exists():
                return _pandas().read_parquet(path, columns=list(columns) if columns else None)
            raise NflverseError(f"nflverse returned {response.status_code} for {url}")

        self.cache_path.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(response.content)
        os.replace(temporary, path)
        try:
            return _pandas().read_parquet(path, columns=list(columns) if columns else None)
        except Exception as exc:
            # A successful HTTP response can still be an upstream error page or
            # corrupt asset. Do not preserve it as a trusted cache entry.
            path.unlink(missing_ok=True)
            raise NflverseError(f"could not decode nflverse parquet for {asset}: {exc}") from exc

    def _stack(self, asset: str, seasons: Iterable[int], *, force: bool = False,
               columns: Iterable[str] | None = None):
        pd = _pandas()
        frames = [self.frame(asset, int(season), force=force, columns=columns) for season in seasons]
        frames = [frame for frame in frames if not frame.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def load_teams(self, *, force: bool = False):
        return self.frame("teams", force=force)

    def load_schedules(self, seasons: Iterable[int] | None = None, *, force: bool = False):
        frame = self.frame("schedules", force=force)
        if frame.empty or seasons is None:
            return frame
        wanted = {int(season) for season in seasons}
        return frame[frame["season"].isin(wanted)].reset_index(drop=True)

    def load_rosters(self, seasons: Iterable[int], *, force: bool = False):
        return self._stack("rosters", seasons, force=force)

    def load_weekly(self, seasons: Iterable[int], *, season_type: str = "REG", force: bool = False):
        frame = self._stack("weekly", seasons, force=force)
        if not frame.empty and season_type and "season_type" in frame.columns:
            frame = frame[frame["season_type"] == season_type]
        return frame.reset_index(drop=True)

    def load_snap_counts(self, seasons: Iterable[int], *, force: bool = False):
        return self._stack("snap_counts", seasons, force=force)

    def load_injuries(self, seasons: Iterable[int], *, force: bool = False):
        return self._stack("injuries", seasons, force=force)

    def load_depth_charts(self, seasons: Iterable[int], *, force: bool = False):
        return self._stack("depth_charts", seasons, force=force)

    def load_player_master(self, *, force: bool = False):
        return self.frame("player_master", force=force)

    def load_player_ids(self, *, force: bool = False):
        """Load DynastyProcess's weekly cross-platform ID map through the raw cache."""
        path = self.cache_path / "player_ids.csv"
        if not force and self._is_fresh(path, "player_ids", None):
            return _pandas().read_csv(path, low_memory=False)
        try:
            response = self.session.get(PLAYER_IDS_URL, timeout=120)
        except requests.RequestException as exc:
            if path.exists():
                return _pandas().read_csv(path, low_memory=False)
            raise NflverseError(f"could not reach DynastyProcess player IDs: {exc}") from exc
        if not response.ok:
            if path.exists():
                return _pandas().read_csv(path, low_memory=False)
            raise NflverseError(f"DynastyProcess returned {response.status_code} for player IDs")
        self.cache_path.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".csv.part")
        temporary.write_bytes(response.content)
        os.replace(temporary, path)
        try:
            return _pandas().read_csv(path, low_memory=False)
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise NflverseError(f"could not decode DynastyProcess player IDs: {exc}") from exc

    def load_team_weekly(self, seasons: Iterable[int], *, force: bool = False):
        return self._stack("team_weekly", seasons, force=force)

    def load_pbp(self, seasons: Iterable[int], *, force: bool = False):
        return self._stack("pbp", seasons, force=force, columns=PBP_ANALYTICS_COLUMNS)

    def _projected(self, asset: str, seasons: Iterable[int], wanted: Iterable[str], *, force: bool = False):
        """Stack a per-season asset keeping only `wanted` columns that the file actually has.

        The full play-by-play parquet is ~370 columns; reading all of it on a small instance is
        what once caused a MemoryError, so this reads the schema first and projects. A column
        nflverse drops or renames simply comes through absent instead of failing the read.
        """
        pd = _pandas()
        frames = []
        for season in seasons:
            # Ensures the file is cached (downloading if stale) without decoding it.
            probe = self.frame(asset, int(season), force=force, columns=[next(iter(wanted))])
            if probe.empty:
                continue
            import pyarrow.parquet as pq
            present = set(pq.read_schema(self._path(asset, int(season))).names)
            keep = [column for column in wanted if column in present]
            frames.append(pd.read_parquet(self._path(asset, int(season)), columns=keep))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def load_play_details(self, seasons: Iterable[int], *, force: bool = False):
        return self._projected("pbp", seasons, PLAY_DETAIL_COLUMNS, force=force)

    def load_participation(self, seasons: Iterable[int], *, force: bool = False):
        return self._projected("participation", seasons, PARTICIPATION_COLUMNS, force=force)

    def load_ftn(self, seasons: Iterable[int], *, force: bool = False):
        return self._projected("ftn", seasons, FTN_COLUMNS, force=force)

    def _load_next_gen_stats(self, asset: str, seasons: Iterable[int], *, force: bool = False):
        """Next Gen Stats ships as one all-seasons file per stat type, so
        filter to the wanted seasons client-side rather than fetching once
        per season like the per-season release assets above."""
        frame = self.frame(asset, force=force)
        if frame.empty:
            return frame
        wanted = {int(season) for season in seasons}
        return frame[frame["season"].isin(wanted)].reset_index(drop=True)

    def load_ngs_passing(self, seasons: Iterable[int], *, force: bool = False):
        return self._load_next_gen_stats("ngs_passing", seasons, force=force)

    def load_ngs_rushing(self, seasons: Iterable[int], *, force: bool = False):
        return self._load_next_gen_stats("ngs_rushing", seasons, force=force)

    def load_ngs_receiving(self, seasons: Iterable[int], *, force: bool = False):
        return self._load_next_gen_stats("ngs_receiving", seasons, force=force)
