"""NFL kickoff weather, mirroring the CFB side's Open-Meteo integration.

Shares sports_aggregator.providers.weather (no API key, free tier, on-disk
cache) rather than duplicating the HTTP client. Storage is a separate table
(nfl_game_weather) so this stays independent of CFB's schema and refresh
cadence, but the row shape and snapshot-accumulation behavior are identical
on purpose: a forecast taken far out and one taken on game morning are both
kept, and the difference between them is often the interesting part.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time, timezone
import json
from typing import Any
from zoneinfo import ZoneInfo

from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.venues import TEAM_VENUES
from sports_aggregator.providers.weather import (
    Forecast, OpenMeteoClient, WeatherQuotaExhausted, weather_condition,
    weather_emoji, weather_flags,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: nflverse documents `gametime` as Eastern local time regardless of venue;
#: zoneinfo resolves the correct EST/EDT offset for that specific date rather
#: than a fixed UTC-4/-5 guess.
_EASTERN = ZoneInfo("America/New_York")


def kickoff_utc(game: dict[str, Any]) -> str | None:
    """Best-effort UTC kickoff moment from nflverse's date + Eastern time-of-day.

    Approximate by nature (nflverse gives no venue-local offset), but only
    needs to land within Open-Meteo's hourly buckets, so being off by the
    rare data-entry quirk does not change which hour gets matched.
    """
    game_date = game.get("game_date")
    game_time = game.get("game_time")
    if not game_date or not game_time:
        return None
    try:
        day = date.fromisoformat(str(game_date))
        hour, minute = (int(part) for part in str(game_time).split(":")[:2])
        local = datetime.combine(day, time(hour, minute), tzinfo=_EASTERN)
    except (ValueError, TypeError):
        return None
    return local.astimezone(timezone.utc).isoformat()


def store_weather(repository: NFLRepository, game_id: str, forecast: Forecast, *,
                  flags: list[dict[str, str]], venue: str, latitude: float,
                  longitude: float, indoor: bool, generated_at: str,
                  source: str = "open-meteo") -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.execute(
            """INSERT INTO nfl_game_weather VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(game_id,forecast_generated_at) DO UPDATE SET
               temperature=excluded.temperature,
               precipitation_probability=excluded.precipitation_probability,
               precipitation_amount=excluded.precipitation_amount,
               sustained_wind=excluded.sustained_wind,wind_gust=excluded.wind_gust,
               humidity=excluded.humidity,visibility=excluded.visibility,
               weather_code=excluded.weather_code,condition=excluded.condition,
               flags_json=excluded.flags_json""",
            (game_id, generated_at, forecast.kickoff, forecast.forecast_hour,
             forecast.temperature, forecast.precipitation_probability,
             forecast.precipitation, forecast.wind_speed, forecast.wind_gusts,
             forecast.humidity, forecast.visibility, forecast.weather_code,
             forecast.condition, json.dumps(flags), int(bool(indoor)),
             venue, latitude, longitude, source, _utc_now()),
        )
        connection.commit()


def weather_for_game(repository: NFLRepository, game_id: str) -> dict[str, Any]:
    """The newest forecast for a game, plus how it has moved since the first."""
    repository.initialize()
    with closing(repository._connect()) as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT * FROM nfl_game_weather WHERE game_id=?
               ORDER BY forecast_generated_at DESC""", (game_id,))]
    if not rows:
        return {"available": False, "snapshots": 0, "flags": []}
    latest, first = rows[0], rows[-1]
    latest["flags"] = json.loads(latest.pop("flags_json") or "[]")
    if not latest.get("condition") or latest["condition"] == "Unknown":
        latest["condition"] = weather_condition(latest.get("weather_code"))
    latest["emoji"] = weather_emoji(latest.get("weather_code"), indoor=bool(latest.get("indoor")))
    movement = {}
    for field in ("temperature", "precipitation_probability", "sustained_wind"):
        if latest.get(field) is not None and first.get(field) is not None and len(rows) > 1:
            movement[field] = round(latest[field] - first[field], 1)
    return {
        "available": True, "snapshots": len(rows), "latest": latest,
        "flags": latest["flags"], "indoor": bool(latest.get("indoor")),
        "movement": movement, "first_forecast_at": first["forecast_generated_at"],
    }


def sync_game_weather(repository: NFLRepository, games: list[dict[str, Any]], *,
                      client: OpenMeteoClient | None = None,
                      force: bool = False) -> dict[str, int]:
    """Refresh forecasts for every upcoming game within Open-Meteo's horizon.

    One request per distinct venue (not per game) covers every home game at
    that stadium inside the 16-day window, matching the free tier's own
    guidance to stay reasonable with request volume.
    """
    client = client or OpenMeteoClient()
    generated_at = _utc_now()
    by_venue: dict[tuple[float, float], list[dict[str, Any]]] = {}
    for game in games:
        if game.get("completed"):
            continue
        kickoff = kickoff_utc(game)
        if not kickoff or not OpenMeteoClient.within_horizon(kickoff):
            continue
        home = canon_team(game.get("home_team"))
        entry = TEAM_VENUES.get(home)
        if not entry:
            continue
        venue_name, latitude, longitude, _elevation, dome = entry
        if dome:
            continue  # A fixed roof has no forecast worth fetching.
        by_venue.setdefault((latitude, longitude), []).append(
            {**game, "venue_name": venue_name, "kickoff_utc": kickoff})

    stored = skipped = failed = 0
    for (latitude, longitude), venue_games in by_venue.items():
        try:
            payload = client.venue_forecast(latitude, longitude, force=force)
        except WeatherQuotaExhausted:
            failed += len(venue_games)
            continue
        except Exception:
            failed += len(venue_games)
            continue
        for game in venue_games:
            forecast = client.at_kickoff(payload, game["kickoff_utc"])
            if forecast is None:
                skipped += 1
                continue
            flags = weather_flags(forecast)
            store_weather(
                repository, game["game_id"], forecast, flags=flags,
                venue=game["venue_name"], latitude=latitude, longitude=longitude,
                indoor=False, generated_at=generated_at,
            )
            stored += 1
    return {"stored": stored, "skipped": skipped, "failed": failed,
            "venues": len(by_venue)}
