"""Choose the smallest refresh profile a given moment needs."""

from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

GAME_WINDOW_HOURS = 6.0
#: A cron tick counts as "the top of the hour" for its first this-many minutes. Render starts cron
#: jobs a little after the minute, by an amount that varies (the production log shows ticks at
#: 02:01:37 and 06:01:17 as well as 01:00:13 and 08:00:21), and this used to require minute 0
#: exactly: a tick that landed on :01 was refused as "between scheduled ticks", so whether a
#: scheduled segment ran depended on cron jitter -- the 2 AM analytics and 6 AM core runs were both
#: skipped that way. The cron fires every 15 minutes, so only the first tick of an hour falls in
#: this window and each hour still yields one scheduled run.
TICK_WINDOW_MINUTES = 10
PREGAME_WINDOW_HOURS = 1.0


def _hours(name: str, default: str) -> set[int]:
    return {int(value.strip()) for value in os.getenv(name, default).split(",")
            if value.strip()}


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _on_tick(moment: datetime) -> bool:
    return moment.minute < TICK_WINDOW_MINUTES


def games_in_progress(repository, *, now: datetime | None = None,
                      season: int | None = None) -> int:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    opened = moment - timedelta(hours=GAME_WINDOW_HOURS)
    closes = moment + timedelta(hours=PREGAME_WINDOW_HOURS)
    repository.initialize()
    with closing(repository._connect()) as connection:
        if season is None:
            row = connection.execute("SELECT MAX(season) FROM games").fetchone()
            season = row[0] if row else None
        if season is None:
            return 0
        return connection.execute(
            """SELECT COUNT(*) FROM games
               WHERE season=? AND completed=0
                 AND start_date>=? AND start_date<=?""",
            (season, opened.isoformat().replace("+00:00", "Z"),
             closes.isoformat().replace("+00:00", "Z"))).fetchone()[0]


def profile_for(repository, *, now: datetime | None = None,
                season: int | None = None,
                timezone_name: str | None = None) -> dict[str, Any]:
    """Return one bounded profile, or None when this cron tick needs no work.

    Local/dev keeps the original quarter-hour live behavior by default. A
    constrained production deployment can set ``CFB_LIVE_REFRESH_EVERY_HOURS``
    to 2 (or more) so top-of-hour cron checks only launch live refreshes on that
    cadence. Outside games, scheduled work remains split into bounded segments.
    """
    zone = ZoneInfo(timezone_name or os.getenv("CFB_REFRESH_TIMEZONE",
                                               "America/New_York"))
    moment = (now or datetime.now(timezone.utc)).astimezone(zone)
    live = games_in_progress(repository, now=moment, season=season)

    if live:
        interval = _positive_int("CFB_LIVE_REFRESH_EVERY_HOURS", 1)
        # Preserve the old minute-level behavior when interval=1 so local
        # development and existing tests continue to get tiny score pulses.
        if interval == 1:
            profile = "results" if _on_tick(moment) else "scores"
            return {
                "profile": profile,
                "reason": "hourly_live_results" if profile == "results" else "games_in_progress",
                "games": live,
                "local_time": moment.isoformat(),
            }

        # Production cron wakes on the hour. Only every Nth hour launches the
        # heavier games + box scores + lines pass; intervening checks are free.
        if not _on_tick(moment) or moment.hour % interval != 0:
            return {
                "profile": None,
                "reason": "between_live_refreshes",
                "games": live,
                "local_time": moment.isoformat(),
            }
        return {
            "profile": "results",
            "reason": f"live_results_every_{interval}h",
            "games": live,
            "local_time": moment.isoformat(),
        }

    if not _on_tick(moment):
        return {"profile": None, "reason": "between_scheduled_ticks", "games": 0,
                "local_time": moment.isoformat()}

    # Heavy is retained only for backward-compatible explicit/manual requests;
    # production scheduling leaves this set empty.
    heavy = _hours("CFB_REFRESH_HEAVY_HOURS", "")
    light = _hours("CFB_REFRESH_HOURS", "2,4,6,10,12,16,18,22,23")
    news = _hours("CFB_REFRESH_NEWS_HOURS", "1,3,5,8,14,20")
    if moment.hour in heavy:
        return {"profile": "heavy", "reason": "scheduled_hour", "games": 0,
                "local_time": moment.isoformat()}
    if moment.hour in light:
        return {"profile": "light", "reason": "scheduled_segment", "games": 0,
                "local_time": moment.isoformat()}
    if moment.hour in news:
        return {"profile": "news", "reason": "scheduled_news_shard", "games": 0,
                "local_time": moment.isoformat()}
    return {"profile": None, "reason": "outside_refresh_hours", "games": 0,
            "local_time": moment.isoformat()}
