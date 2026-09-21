"""Historical weather for completed CFB games, via Open-Meteo's archive API.

The live forecast integration (sports_aggregator.providers.weather) only
covers the ~16-day forward horizon, so every completed game beyond that
window has no weather in game_weather at all -- there was nothing to study
weather's effect on scoring/line movement against beyond one thin season.

Confirmed live before writing this: archive-api.open-meteo.com/v1/archive
works with no API key and no payment under the same non-commercial terms as
the forecast API already integrated (the pricing page's "Professional Plan"
requirement is about commercial/SLA access, not free non-commercial use --
verified by an actual unauthenticated call returning real data, not a 402).
It also accepts comma-separated multi-location batches sharing one date
range, confirmed to return one response entry per location in request
order. This backfill exploits that: every outdoor game on a given UTC
calendar date shares one archive call (chunked at BATCH_SIZE locations,
since the busiest real dates have ~70 games) instead of one call per game
-- roughly 850-1,700 calls for an 11-season backfill rather than ~9,000,
paced with a courteous delay between calls on what is still a free, keyless
API being asked to stay reasonable.

Reuses the forecast integration's Forecast/at_kickoff/weather_flags/
store_weather rather than a parallel implementation: an archive reading is
stored through the exact same game_weather table and rendering path as a
live forecast snapshot, distinguished only by source="open-meteo-archive".
Existing weather-panel display code needs no changes to show it.
"""
from __future__ import annotations

import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from sports_aggregator.cfb import external
from sports_aggregator.cfb.external import store_weather
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.providers.weather import HOURLY_VARIABLES, OpenMeteoClient, weather_flags

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
SOURCE = "open-meteo-archive"
#: The busiest real game dates have ~70 outdoor venues; chunking well under
#: that avoids relying on an undocumented per-request location cap.
BATCH_SIZE = 40
#: Courteous pacing on a free, keyless, non-commercial API well under its
#: documented 600/minute fair-use limit.
REQUEST_PAUSE_SECONDS = 0.5


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.setdefault("User-Agent", "cfb-intelligence/1.0 (weather-backfill)")
    retry = Retry(
        total=4, connect=3, read=3, status=4, backoff_factor=0.75,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}), respect_retry_after_header=True,
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _outdoor_games(repository: CFBRepository, start_season: int, end_season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        return [dict(r) for r in connection.execute(
            """SELECT g.game_id,g.start_date,g.venue,v.latitude,v.longitude
               FROM games g JOIN venues v ON v.venue_id=g.venue_id
               WHERE g.season BETWEEN ? AND ? AND g.completed=1
                 AND v.latitude IS NOT NULL AND v.longitude IS NOT NULL
                 AND (v.dome IS NULL OR v.dome=0)
               ORDER BY date(g.start_date)""",
            (int(start_season), int(end_season)),
        )]


def backfill(repository: CFBRepository, *, start_season: int = 2015, end_season: int = 2025,
            force: bool = False, session: requests.Session | None = None) -> dict[str, int]:
    external.initialize(repository)
    games = _outdoor_games(repository, start_season, end_season)
    if not force:
        with repository._reader() as connection:
            already = {int(row[0]) for row in connection.execute(
                "SELECT game_id FROM game_weather WHERE source=?", (SOURCE,)
            )}
        games = [g for g in games if int(g["game_id"]) not in already]

    by_date: dict[str, list[dict[str, Any]]] = {}
    for game in games:
        by_date.setdefault(str(game["start_date"])[:10], []).append(game)

    http = session or _session()
    stored = calls = failures = 0
    for date_key in sorted(by_date):
        day_games = by_date[date_key]
        for start in range(0, len(day_games), BATCH_SIZE):
            chunk = day_games[start:start + BATCH_SIZE]
            params = {
                "latitude": ",".join(f"{g['latitude']:.4f}" for g in chunk),
                "longitude": ",".join(f"{g['longitude']:.4f}" for g in chunk),
                "start_date": date_key, "end_date": date_key,
                "hourly": ",".join(HOURLY_VARIABLES),
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "precipitation_unit": "inch", "timezone": "UTC",
            }
            try:
                response = http.get(ARCHIVE_URL, params=params, timeout=40)
                response.raise_for_status()
                calls += 1
                payload = response.json()
                entries = payload if isinstance(payload, list) else [payload]
                for game, entry in zip(chunk, entries):
                    forecast = OpenMeteoClient.at_kickoff(entry, str(game["start_date"]))
                    if forecast is None:
                        continue
                    store_weather(
                        repository, int(game["game_id"]), forecast,
                        flags=weather_flags(forecast), venue=str(game["venue"] or ""),
                        latitude=float(game["latitude"]), longitude=float(game["longitude"]),
                        indoor=False, generated_at=forecast.kickoff, source=SOURCE,
                    )
                    stored += 1
            except requests.RequestException:
                failures += len(chunk)
            time.sleep(REQUEST_PAUSE_SECONDS)
    return {"games_considered": len(games), "stored": stored, "api_calls": calls, "failures": failures}
