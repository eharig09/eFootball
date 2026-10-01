"""Historical NFL game-window weather for backtesting.

Two kinds of rows are stored in nfl_weather_history, both aggregated over the
game window (the kickoff hour and the three after it):

  observed   Open-Meteo archive (ERA5 reanalysis). Complete for every game from
             2013, unlike the patchy nflverse temperature/wind columns.
  forecast   Open-Meteo Previous Runs: the forecast issued `lead_days` before
             the valid hour. Temperature history starts in 2021; wind, gust and
             precipitation only in 2024, so earlier seasons carry NULLs.

Observed weather is what a model can be *fitted* on; the forecast rows say how
much of that signal survives when only a forecast exists, which is what a live
pick actually has. Units match nflverse (degF, mph, inches).

Fetches go through an on-disk cache keyed by request, so a run interrupted by
the free tier's daily quota resumes without repeating work.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Iterable

import requests

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.weather import kickoff_utc

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
VARIABLES = ("temperature_2m", "wind_speed_10m", "wind_gusts_10m", "precipitation")
WINDOW_HOURS = 4
INDOOR_ROOFS = frozenset({"dome", "closed"})
FORECAST_LEADS = (1, 2)

# name -> (latitude, longitude, default roof is indoor). Several names are one
# building (renames); relocations get their own entries. Reanalysis cells are
# ~10 km, so coordinates only need to be right to a kilometre or so.
_VENUES = {
    "AT&T Stadium": (32.7473, -97.0945, False),
    "Acrisure Stadium": (40.4468, -80.0158, False), "Heinz Field": (40.4468, -80.0158, False),
    "Allegiant Stadium": (36.0909, -115.1833, True),
    "Allianz Arena": (48.2188, 11.6247, False), "FC Bayern Munich Stadium": (48.2188, 11.6247, False),
    "Arena Corinthians": (-23.5453, -46.4742, False),
    "Arrowhead Stadium": (39.0489, -94.4839, False), "GEHA Field at Arrowhead Stadium": (39.0489, -94.4839, False),
    "Azteca Stadium": (19.3029, -99.1505, False), "Estadio Banorte": (19.3029, -99.1505, False),
    "Bank of America Stadium": (35.2258, -80.8528, False),
    "Bernabeu": (40.4531, -3.6883, False),
    "Caesars Superdome": (29.9511, -90.0812, True), "Mercedes-Benz Superdome": (29.9511, -90.0812, True),
    "Candlestick Park": (37.7135, -122.3862, False),
    "CenturyLink Field": (47.5952, -122.3316, False), "Lumen Field": (47.5952, -122.3316, False),
    "Deutsche Bank Park": (50.0686, 8.6455, False),
    "Edward Jones Dome": (38.6328, -90.1885, True),
    "Empower Field at Mile High": (39.7439, -105.0201, False),
    "Sports Authority Field at Mile High": (39.7439, -105.0201, False),
    "EverBank Field": (30.3239, -81.6373, False), "EverBank Stadium": (30.3239, -81.6373, False),
    "TIAA Bank Stadium": (30.3239, -81.6373, False),
    "FedExField": (38.9077, -76.8645, False), "Northwest Stadium": (38.9077, -76.8645, False),
    "FirstEnergy Stadium": (41.5061, -81.6995, False), "Huntington Bank Field": (41.5061, -81.6995, False),
    "Ford Field": (42.3400, -83.0456, True),
    "Georgia Dome": (33.7577, -84.4008, True),
    "Gillette Stadium": (42.0909, -71.2643, False),
    "Hard Rock Stadium": (25.9580, -80.2389, False), "Sun Life Stadium": (25.9580, -80.2389, False),
    "Highmark Stadium": (42.7738, -78.7870, False), "New Era Field": (42.7738, -78.7870, False),
    "Ralph Wilson Stadium": (42.7738, -78.7870, False),
    "LP Field": (36.1665, -86.7713, False), "Nissan Stadium": (36.1665, -86.7713, False),
    "Lambeau Field": (44.5013, -88.0622, False),
    "Levi's Stadium": (37.4032, -121.9698, False),
    "Lincoln Financial Field": (39.9008, -75.1675, False),
    "Los Angeles Memorial Coliseum": (34.0141, -118.2879, False),
    "Lucas Oil Stadium": (39.7601, -86.1639, False),
    "M&T Bank Stadium": (39.2780, -76.6227, False),
    "Maracana Stadium": (-22.9121, -43.2302, False),
    "Mall of America Field": (44.9737, -93.2581, True), "U.S. Bank Stadium": (44.9735, -93.2575, True),
    "Melbourne Cricket Ground": (-37.8200, 144.9834, False),
    "Mercedes-Benz Stadium": (33.7554, -84.4010, False),
    "MetLife Stadium": (40.8135, -74.0745, False),
    "NRG Stadium": (29.6847, -95.4107, False), "Reliant Stadium": (29.6847, -95.4107, False),
    "O.co Coliseum": (37.7516, -122.2005, False), "Oakland-Alameda County Coliseum": (37.7516, -122.2005, False),
    "Ring Central Coliseum": (37.7516, -122.2005, False),
    "Paul Brown Stadium": (39.0954, -84.5160, False), "Paycor Stadium": (39.0954, -84.5160, False),
    "Qualcomm Stadium": (32.7831, -117.1196, False),
    "Raymond James Stadium": (27.9759, -82.5033, False),
    "Rogers Centre": (43.6414, -79.3894, True),
    "SoFi Stadium": (33.9535, -118.3392, True),
    "Soldier Field": (41.8623, -87.6167, False),
    "Stade de France": (48.9245, 2.3601, False),
    "State Farm Stadium": (33.5276, -112.2626, False), "University of Phoenix Stadium": (33.5276, -112.2626, False),
    "StubHub Center": (33.8644, -118.2611, False),
    "TCF Bank Stadium": (44.9765, -93.2248, False),
    "Tottenham Hotspur Stadium": (51.6043, -0.0662, False), "Tottenham Stadium": (51.6043, -0.0662, False),
    "Twickenham Stadium": (51.4559, -0.3417, False),
    "Wembley Stadium": (51.5560, -0.2795, False),
}


def venue(stadium: str | None) -> tuple[float, float, bool] | None:
    return _VENUES.get(str(stadium or "").strip())


def is_indoor(roof: str | None, stadium: str | None) -> bool:
    """A recorded roof wins; only an unrecorded roof falls back to the venue default."""
    if roof:
        return str(roof) in INDOOR_ROOFS
    v = venue(stadium)
    return bool(v and v[2])


def window_times(kickoff: str, hours: int = WINDOW_HOURS) -> list[str]:
    """Open-Meteo-style hourly timestamps covering the game, starting at kickoff's hour."""
    start = datetime.fromisoformat(kickoff).astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0, tzinfo=None)
    return [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(hours)]


def aggregate(hourly: dict[str, list], times: Iterable[str], suffix: str = "") -> dict[str, Any]:
    """Mean temp/wind, max gust, total precipitation over the window; None if no data."""
    index = {t: i for i, t in enumerate(hourly.get("time") or [])}
    picks = [index[t] for t in times if t in index]

    def column(name):
        series = hourly.get(name + suffix) or []
        return [series[i] for i in picks if i < len(series) and series[i] is not None]

    temp, wind, gust, rain = (column(v) for v in VARIABLES)
    return {
        "temperature": sum(temp) / len(temp) if temp else None,
        "wind_speed": sum(wind) / len(wind) if wind else None,
        "wind_gust": max(gust) if gust else None,
        "precipitation": sum(rain) if rain else None,
        "hours_used": max(len(temp), len(wind), len(gust), len(rain)),
    }


class WeatherQuota(RuntimeError):
    """The free tier's quota is spent; the run is resumable from the cache."""


class Fetcher:
    def __init__(self, cache: str | Path = "instance/weather_history", pause: float = 0.25,
                 session: requests.Session | None = None) -> None:
        self.cache = Path(cache)
        self.pause = pause
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "sports-aggregator/1.0 (weather backtest)")
        self.calls = 0

    def get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        key = hashlib.sha1((url + json.dumps(params, sort_keys=True)).encode()).hexdigest()[:20]
        path = self.cache / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        for attempt in range(5):
            response = self.session.get(url, params=params, timeout=90)
            self.calls += 1
            if response.status_code == 429:
                text = response.text.casefold()
                if "daily" in text or "limit" in text and "hour" not in text:
                    raise WeatherQuota(response.text[:200])
                time.sleep(2 ** attempt * 5)
                continue
            if response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            payload = response.json()
            self.cache.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            time.sleep(self.pause)
            return payload
        raise RuntimeError(f"Open-Meteo kept failing for {url}")


def _games(repository: NFLRepository, start: int, end: int) -> list[dict[str, Any]]:
    with closing(repository._connect()) as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT game_id,season,game_date,game_time,stadium,roof FROM games
               WHERE season BETWEEN ? AND ? AND game_date IS NOT NULL AND game_time IS NOT NULL
                 AND stadium IS NOT NULL""", (int(start), int(end)))]
    out = []
    for g in rows:
        kick, v = kickoff_utc(g), venue(g["stadium"])
        if kick and v:
            out.append({**g, "kickoff": kick, "lat": v[0], "lon": v[1],
                        "indoor": is_indoor(g["roof"], g["stadium"])})
    return out


def unmapped_stadiums(repository: NFLRepository, start: int, end: int) -> dict[str, int]:
    with closing(repository._connect()) as connection:
        return {str(r["stadium"]): int(r["n"]) for r in connection.execute(
            "SELECT stadium,COUNT(*) n FROM games WHERE season BETWEEN ? AND ? GROUP BY stadium",
            (int(start), int(end))) if venue(r["stadium"]) is None}


def backfill(repository: NFLRepository, fetcher: Fetcher, start: int, end: int, *,
             forecasts: bool = True) -> dict[str, int]:
    """Fetch and store observed (and optionally forecast) weather for games in [start, end]."""
    repository.initialize()
    groups: dict[tuple[float, float, int], list[dict[str, Any]]] = defaultdict(list)
    for g in _games(repository, start, end):
        groups[(g["lat"], g["lon"], int(g["season"]))].append(g)
    counts = {"observed": 0, "forecast": 0, "venue_seasons": len(groups)}
    now = datetime.now(timezone.utc).isoformat()

    for (lat, lon, season), games in sorted(groups.items()):
        days = sorted({g["kickoff"][:10] for g in games})
        first = (date.fromisoformat(days[0]) - timedelta(days=1)).isoformat()
        last = (date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat()
        base = {"latitude": lat, "longitude": lon, "start_date": first, "end_date": last,
                "timezone": "GMT", "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph", "precipitation_unit": "inch"}
        observed = fetcher.get(ARCHIVE_URL, {**base, "hourly": ",".join(VARIABLES)})["hourly"]
        rows = []
        for g in games:
            rows.append({
                "game_id": g["game_id"], "kind": "observed", "lead_days": 0, "source": "open-meteo-archive",
                "kickoff_utc": g["kickoff"], "stadium": g["stadium"], "latitude": lat, "longitude": lon,
                "indoor": g["indoor"], "fetched_at": now,
                **aggregate(observed, window_times(g["kickoff"])),
            })
        counts["observed"] += repository.upsert_weather_history(rows)

        if forecasts and season >= 2021:
            previous = [f"{v}_previous_day{n}" for v in VARIABLES for n in FORECAST_LEADS]
            hourly = fetcher.get(PREVIOUS_RUNS_URL, {**base, "hourly": ",".join(previous)})["hourly"]
            rows = []
            for g in games:
                for lead in FORECAST_LEADS:
                    rows.append({
                        "game_id": g["game_id"], "kind": "forecast", "lead_days": lead,
                        "source": "open-meteo-previous-runs", "kickoff_utc": g["kickoff"],
                        "stadium": g["stadium"], "latitude": lat, "longitude": lon,
                        "indoor": g["indoor"], "fetched_at": now,
                        **aggregate(hourly, window_times(g["kickoff"]), f"_previous_day{lead}"),
                    })
            counts["forecast"] += repository.upsert_weather_history(rows)
    counts["http_calls"] = fetcher.calls
    return counts
