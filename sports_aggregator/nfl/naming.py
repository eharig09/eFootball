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

# Free-text nicknames, as they appear in manually-curated exports (a Google
# Sheet listing "Vikings", not "MIN") rather than any feed's abbreviation.
TEAM_NICKNAMES = {
    "CARDINALS": "ARI", "FALCONS": "ATL", "RAVENS": "BAL", "BILLS": "BUF",
    "PANTHERS": "CAR", "BEARS": "CHI", "BENGALS": "CIN", "BROWNS": "CLE",
    "COWBOYS": "DAL", "BRONCOS": "DEN", "LIONS": "DET", "PACKERS": "GNB",
    "TEXANS": "HOU", "COLTS": "IND", "JAGUARS": "JAC", "CHIEFS": "KAN",
    "CHARGERS": "LAC", "RAMS": "LAR", "RAIDERS": "LV", "DOLPHINS": "MIA",
    "VIKINGS": "MIN", "SAINTS": "NOR", "PATRIOTS": "NWE", "GIANTS": "NYG",
    "JETS": "NYJ", "EAGLES": "PHI", "STEELERS": "PIT", "SEAHAWKS": "SEA",
    "49ERS": "SFO", "NINERS": "SFO", "BUCCANEERS": "TAM", "TITANS": "TEN",
    "COMMANDERS": "WAS",
}

POSITION_ALIASES = {
    "HB": "RB", "FB": "RB", "RB": "RB",
    "WR": "WR", "TE": "TE", "QB": "QB",
}

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def canon_team(abbreviation: object) -> str:
    key = str(abbreviation or "").strip().upper()
    if key in TEAM_ALIASES:
        return TEAM_ALIASES[key]
    return TEAM_NICKNAMES.get(key, key)


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
