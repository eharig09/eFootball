"""Static NFL stadium geography, for travel/altitude context and weather forecasts.

Unlike the teams table (fully replaced from nflverse on every sync), this is
hand-maintained and rarely changes -- a relocation or a new stadium is a
once-a-decade event, not something worth re-deriving from a feed. Coordinates
are city-level accurate, which is all a 16-day hourly forecast or a
great-circle travel distance needs.

`dome` marks a venue where weather is never a factor (a fixed roof, including
a nominally-retractable one that in practice stays closed, or a translucent
fixed canopy like SoFi's). For an actually-retractable roof (ARI, ATL, DAL,
HOU, IND), the per-game `roof` column already recorded by nflverse -- open,
closed, dome, outdoors -- is the authoritative signal for that specific game;
`dome` here is only the fallback when that column is missing.
"""

from __future__ import annotations

from typing import Any

#: team -> (venue_name, latitude, longitude, elevation_meters, dome)
TEAM_VENUES: dict[str, tuple[str, float, float, float, bool]] = {
    "ARI": ("State Farm Stadium", 33.5276, -112.2626, 331, False),
    "ATL": ("Mercedes-Benz Stadium", 33.7554, -84.4008, 320, False),
    "BAL": ("M&T Bank Stadium", 39.2780, -76.6227, 20, False),
    "BUF": ("Highmark Stadium", 42.7738, -78.7870, 204, False),
    "CAR": ("Bank of America Stadium", 35.2258, -80.8528, 229, False),
    "CHI": ("Soldier Field", 41.8623, -87.6167, 180, False),
    "CIN": ("Paycor Stadium", 39.0954, -84.5160, 148, False),
    "CLE": ("Huntington Bank Field", 41.5061, -81.6995, 176, False),
    "DAL": ("AT&T Stadium", 32.7473, -97.0945, 168, False),
    "DEN": ("Empower Field at Mile High", 39.7439, -105.0201, 1610, False),
    "DET": ("Ford Field", 42.3400, -83.0456, 183, True),
    "GNB": ("Lambeau Field", 44.5013, -88.0622, 201, False),
    "HOU": ("NRG Stadium", 29.6847, -95.4107, 13, False),
    "IND": ("Lucas Oil Stadium", 39.7601, -86.1639, 218, False),
    "JAC": ("EverBank Stadium", 30.3239, -81.6373, 6, False),
    "KAN": ("GEHA Field at Arrowhead Stadium", 39.0489, -94.4839, 276, False),
    "LAC": ("SoFi Stadium", 33.9535, -118.3392, 32, True),
    "LAR": ("SoFi Stadium", 33.9535, -118.3392, 32, True),
    "LV": ("Allegiant Stadium", 36.0909, -115.1833, 610, True),
    "MIA": ("Hard Rock Stadium", 25.9580, -80.2389, 3, False),
    "MIN": ("U.S. Bank Stadium", 44.9735, -93.2575, 253, True),
    "NOR": ("Caesars Superdome", 29.9511, -90.0812, 3, True),
    "NWE": ("Gillette Stadium", 42.0909, -71.2643, 86, False),
    "NYG": ("MetLife Stadium", 40.8135, -74.0745, 9, False),
    "NYJ": ("MetLife Stadium", 40.8135, -74.0745, 9, False),
    "PHI": ("Lincoln Financial Field", 39.9008, -75.1675, 12, False),
    "PIT": ("Acrisure Stadium", 40.4468, -80.0158, 229, False),
    "SEA": ("Lumen Field", 47.5952, -122.3316, 9, False),
    "SFO": ("Levi's Stadium", 37.4032, -121.9698, 15, False),
    "TAM": ("Raymond James Stadium", 27.9759, -82.5033, 5, False),
    "TEN": ("Nissan Stadium", 36.1665, -86.7713, 149, False),
    "WAS": ("Northwest Stadium", 38.9077, -76.8645, 46, False),
}


def venue_for(team: str) -> dict[str, Any] | None:
    entry = TEAM_VENUES.get(team)
    if not entry:
        return None
    venue_name, latitude, longitude, elevation, dome = entry
    return {"venue_name": venue_name, "latitude": latitude, "longitude": longitude,
            "elevation_meters": elevation, "dome": dome}
