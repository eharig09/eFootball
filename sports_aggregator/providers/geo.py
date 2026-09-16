"""Sport-agnostic distance and US time-zone helpers for travel context.

Originally written inline for CFB's situational trap-spot module; extracted
here so NFL travel context can share the exact same math instead of a
second, drifting copy.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

#: Rough longitude bands for US time zones, used only to describe a shift.
TIMEZONE_BOUNDS = ((-180, -115, "Pacific"), (-115, -100, "Mountain"),
                   (-100, -85, "Central"), (-85, 180, "Eastern"))

_ZONE_ORDER = {"Pacific": 0, "Mountain": 1, "Central": 2, "Eastern": 3}


def haversine_miles(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Great-circle distance between two lat/long pairs."""
    lat1, lon1 = radians(first[0]), radians(first[1])
    lat2, lon2 = radians(second[0]), radians(second[1])
    step = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * asin(sqrt(step)) * 3958.8


def timezone_for(longitude: float | None) -> str | None:
    if longitude is None:
        return None
    for west, east, name in TIMEZONE_BOUNDS:
        if west <= longitude < east:
            return name
    return None


def zone_index(name: str | None) -> int | None:
    return _ZONE_ORDER.get(name or "")
