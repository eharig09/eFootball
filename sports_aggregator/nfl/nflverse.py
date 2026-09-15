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
        clock=time.time,
        today=None,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.session = session or requests.Session()
        self.ttl_seconds = max(0, int(ttl_seconds))
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
        return self.clock() - path.stat().st_mtime < self.ttl_seconds

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
