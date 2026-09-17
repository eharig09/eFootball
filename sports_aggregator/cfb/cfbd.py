"""Current CFBD REST adapter with authentication, retries, and raw JSON caching."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LOGGER = logging.getLogger(__name__)
DEFAULT_BASE_URL = "https://api.collegefootballdata.com"

#: A week that is over never changes, so its box scores are cached for a year.
FINISHED_WEEK_TTL = 31536000

#: A week still being played changes every few minutes. Caching one of those
#: for a year would freeze a Saturday afternoon at whatever the first request
#: happened to catch, which is what the single TTL used to do. Long enough that
#: a pass every quarter hour is not four requests an hour for the same bytes,
#: short enough that a line score is never a refresh cycle stale.
LIVE_WEEK_TTL = 240


class CFBDConfigurationError(RuntimeError):
    pass


class CFBDRequestError(RuntimeError):
    pass


class RawJSONCache:
    """Filesystem cache that also preserves raw responses for later reprocessing."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _key(path: str, params: Mapping[str, Any]) -> str:
        payload = json.dumps([path, sorted(params.items())], separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _path(self, path: str, params: Mapping[str, Any]) -> Path:
        endpoint = path.strip("/").replace("/", "_") or "root"
        return self.root / f"{endpoint}-{self._key(path, params)}.json"

    def read(self, path: str, params: Mapping[str, Any], max_age_seconds: int) -> Any | None:
        cache_path = self._path(path, params)
        if not cache_path.exists():
            return None
        if time.time() - cache_path.stat().st_mtime > max_age_seconds:
            return None
        try:
            with cache_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)["data"]
        except (OSError, KeyError, json.JSONDecodeError):
            LOGGER.warning("Ignoring unreadable CFBD cache file %s", cache_path)
            return None

    def write(self, path: str, params: Mapping[str, Any], data: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(path, params)
        envelope = {
            "source": "CFBD",
            "endpoint": path,
            "params": dict(params),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.root, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(envelope, handle, ensure_ascii=False, separators=(",", ":"))
                temp_name = handle.name
            Path(temp_name).replace(target)
        finally:
            if temp_name and Path(temp_name).exists():
                Path(temp_name).unlink()


def _retrying_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


class CFBDClient:
    """Thin adapter over documented CFBD endpoints; downstream code sees plain JSON."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        raw_cache_path: str | Path = "instance/cfbd_raw",
        session: requests.Session | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        configured_key = api_key if api_key is not None else os.getenv("CFBD_API_KEY", "")
        self.api_key = configured_key.strip()
        self.base_url = base_url.rstrip("/")
        self.cache = RawJSONCache(raw_cache_path)
        self.session = session or _retrying_session()
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        cache_ttl_seconds: int = 900,
        force: bool = False,
    ) -> Any:
        if not self.configured:
            raise CFBDConfigurationError(
                "CFBD_API_KEY is not configured. Add it to .env or the process environment."
            )
        normalized_params = {
            key: value for key, value in (params or {}).items() if value is not None
        }
        if not force:
            cached = self.cache.read(path, normalized_params, cache_ttl_seconds)
            if cached is not None:
                return cached

        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self.session.get(
                url,
                params=normalized_params,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                    "User-Agent": "sports-news-aggregator/1.0",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise CFBDRequestError(f"CFBD {path} returned HTTP {status}") from exc
        except (requests.RequestException, ValueError) as exc:
            raise CFBDRequestError(f"CFBD {path} request failed: {exc}") from exc

        self.cache.write(path, normalized_params, data)
        LOGGER.info("Fetched CFBD %s with %d parameter(s)", path, len(normalized_params))
        return data

    def teams(self, year: int, force: bool = False) -> list[dict]:
        return self.get("/teams/fbs", {"year": year}, cache_ttl_seconds=604800, force=force)

    def conferences(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/conferences", {"year": year}, cache_ttl_seconds=604800, force=force
        )

    def games(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/games",
            {"year": year, "seasonType": "both", "classification": "fbs"},
            cache_ttl_seconds=900,
            force=force,
        )

    def roster(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/roster", {"year": year, "classification": "fbs"},
            cache_ttl_seconds=21600, force=force,
        )

    def game_media(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/games/media",
            {"year": year, "seasonType": "both", "classification": "fbs"},
            cache_ttl_seconds=3600,
            force=force,
        )

    def records(self, year: int, force: bool = False) -> list[dict]:
        return self.get("/records", {"year": year}, cache_ttl_seconds=1800, force=force)

    def coaches(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/coaches", {"year": year}, cache_ttl_seconds=21600, force=force
        )

    def game_team_box_scores(self, year: int, week: int,
                             force: bool = False,
                             cache_ttl_seconds: int = FINISHED_WEEK_TTL) -> list[dict]:
        return self.get(
            "/games/teams",
            {"year": year, "week": week, "seasonType": "both", "classification": "fbs"},
            cache_ttl_seconds=cache_ttl_seconds, force=force,
        )

    def game_player_box_scores(self, year: int, week: int,
                               force: bool = False,
                               cache_ttl_seconds: int = FINISHED_WEEK_TTL) -> list[dict]:
        return self.get(
            "/games/players",
            {"year": year, "week": week, "seasonType": "both", "classification": "fbs"},
            cache_ttl_seconds=cache_ttl_seconds, force=force,
        )

    def rankings(self, year: int, force: bool = False) -> list[dict]:
        return self.get("/rankings", {"year": year}, cache_ttl_seconds=1800, force=force)

    def team_stats(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/stats/season",
            {"year": year, "classification": "fbs"},
            cache_ttl_seconds=21600,
            force=force,
        )

    def player_season_stats(self, year: int, conference: str,
                            force: bool = False) -> list[dict]:
        return self.get(
            "/stats/player/season", {"year": year, "conference": conference},
            cache_ttl_seconds=21600, force=force,
        )

    def transfers(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/player/portal", {"year": year}, cache_ttl_seconds=21600, force=force
        )

    def returning_production(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/player/returning", {"year": year}, cache_ttl_seconds=21600, force=force
        )

    def draft_picks(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/draft/picks", {"year": year}, cache_ttl_seconds=604800, force=force
        )

    def advanced_team_stats(self, year: int, force: bool = False) -> list[dict]:
        return self.get(
            "/stats/season/advanced",
            {"year": year, "classification": "fbs", "excludeGarbageTime": "true"},
            cache_ttl_seconds=21600,
            force=force,
        )

    def ppa_players_season(self, year: int, force: bool = False) -> list[dict]:
        """Player-season predicted points added, split by play type.

        Unlike `player_season_stats`, this one call covers all of FBS -- no
        `conference` loop needed. Confirmed live: each row has `id` (not
        `playerId` like the stats endpoints), `averagePPA`/`totalPPA` dicts
        keyed `all`/`pass`/`rush`/... -- no play-count field, so a rank needs
        an external volume floor (see repository.player_ppa_rank).
        """
        return self.get(
            "/ppa/players/season",
            {"year": year, "excludeGarbageTime": "true"},
            cache_ttl_seconds=21600,
            force=force,
        )

    def recruits(self, year: int, force: bool = False) -> list[dict]:
        """Documented /recruiting/players endpoint for one signing class."""
        return self.get("/recruiting/players", {"year": year},
                        cache_ttl_seconds=604800, force=force)

    def venues(self, force: bool = False) -> list[dict]:
        """Documented /venues endpoint; stadium locations rarely change."""
        return self.get("/venues", {}, cache_ttl_seconds=2592000, force=force)

    def betting_lines(self, year: int, force: bool = False,
                      cache_ttl_seconds: int = 1800) -> list[dict]:
        """Documented /lines endpoint; quotes move, so the cache stays short.

        A finished season's quotes do not move at all, which is why the caller
        backfilling one passes FINISHED_WEEK_TTL instead.
        """
        return self.get(
            "/lines", {"year": year, "seasonType": "both"},
            cache_ttl_seconds=cache_ttl_seconds, force=force,
        )

    def core_ratings(self, year: int, force: bool = False) -> list[dict]:
        return self.get("/ratings/core", {"year": year}, cache_ttl_seconds=3600, force=force)
