"""Canonical NFL team, position, and player-name normalization."""

from __future__ import annotations

import re
import unicodedata


TEAM_ALIASES = {
    "JAX": "JAC",
    "WSH": "WAS", "WFT": "WAS",
    "LA": "LAR", "STL": "LAR",
    "SD": "LAC",
    "OAK": "LV", "LVR": "LV",
    "ARZ": "ARI",
    "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    "NO": "NOR", "NOS": "NOR",
    "TB": "TAM", "TBB": "TAM",
    "KC": "KAN", "KCC": "KAN",
    "SF": "SFO", "GB": "GNB", "NE": "NWE",
}

TEAMS = (
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GNB", "HOU", "IND", "JAC", "KAN",
    "LAC", "LAR", "LV", "MIA", "MIN", "NOR", "NWE", "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SFO", "TAM", "TEN", "WAS",
)

POSITION_ALIASES = {
    "HB": "RB", "FB": "RB", "RB": "RB",
    "WR": "WR", "TE": "TE", "QB": "QB",
}

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def canon_team(abbreviation: object) -> str:
    key = str(abbreviation or "").strip().upper()
    return TEAM_ALIASES.get(key, key)


def canon_position(position: object) -> str:
    key = str(position or "").strip().upper()
    return POSITION_ALIASES.get(key, key)


def normalize_name(name: object) -> str:
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.lower().replace("'", "").replace("`", "")
    text = re.sub(r"[.\-]", " ", text)
    return " ".join(
        part for part in re.split(r"\s+", text)
        if part and part not in SUFFIXES
    )
