"""Canonical college-football entities backed by CFBD identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from functools import lru_cache
from typing import Any


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@lru_cache(maxsize=65536)
def normalize_alias(value: str) -> str:
    """Normalize punctuation and whitespace without guessing ambiguous identities."""
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


@lru_cache(maxsize=65536)
def normalize_person_name(value: str) -> str:
    """Normalize a person name, collapsing initials into one token.

    Rosters write "CJ Carr" while articles write "C.J. Carr". Stripping
    punctuation alone turns the second into "c j carr", which never matches the
    first, so a well-covered quarterback resolved to nothing. Adjacent
    single-letter tokens are merged so both forms land on "cj carr".

    Team aliases deliberately keep `normalize_alias`: merging letters there would
    rewrite established aliases for no gain.
    """
    tokens = normalize_alias(value).split()
    merged: list[str] = []
    for token in tokens:
        if len(token) == 1 and merged and len(merged[-1]) <= 2 and merged[-1].isalpha():
            merged[-1] += token
        else:
            merged.append(token)
    return " ".join(merged)


@dataclass(frozen=True, slots=True)
class Team:
    team_id: int
    school: str
    mascot: str | None
    abbreviation: str | None
    conference: str | None
    division: str | None
    classification: str | None
    color: str | None
    alternate_color: str | None
    logos: tuple[str, ...]
    aliases: tuple[str, ...]
    venue_id: int | None
    venue_name: str | None

    @classmethod
    def from_cfbd(cls, item: dict[str, Any]) -> "Team":
        location = item.get("location") or {}
        alternate_names = tuple(str(name) for name in (item.get("alternateNames") or []) if name)
        mascot = item.get("mascot")
        school = str(item["school"])
        aliases = list(alternate_names)
        aliases.extend(filter(None, (school, item.get("abbreviation"))))
        if mascot:
            aliases.extend((str(mascot), f"{school} {mascot}"))
        return cls(
            team_id=int(item["id"]),
            school=school,
            mascot=str(mascot) if mascot else None,
            abbreviation=item.get("abbreviation"),
            conference=item.get("conference"),
            division=item.get("division"),
            classification=item.get("classification"),
            color=item.get("color"),
            alternate_color=item.get("alternateColor"),
            logos=tuple(item.get("logos") or ()),
            aliases=tuple(dict.fromkeys(alias for alias in aliases if alias)),
            venue_id=optional_int(location.get("id")),
            venue_name=location.get("name"),
        )


@dataclass(frozen=True, slots=True)
class Game:
    game_id: int
    season: int
    week: int
    season_type: str
    start_date: datetime
    start_time_tbd: bool
    completed: bool
    neutral_site: bool
    conference_game: bool
    venue_id: int | None
    venue: str | None
    home_team_id: int
    home_team: str
    home_conference: str | None
    home_points: int | None
    home_pregame_elo: int | None
    away_team_id: int
    away_team: str
    away_conference: str | None
    away_points: int | None
    away_pregame_elo: int | None
    excitement_index: float | None
    notes: str | None

    @classmethod
    def from_cfbd(cls, item: dict[str, Any]) -> "Game":
        return cls(
            game_id=int(item["id"]),
            season=int(item["season"]),
            week=int(item["week"]),
            season_type=str(item["seasonType"]),
            start_date=parse_datetime(str(item["startDate"])),
            start_time_tbd=bool(item.get("startTimeTBD")),
            completed=bool(item.get("completed")),
            neutral_site=bool(item.get("neutralSite")),
            conference_game=bool(item.get("conferenceGame")),
            venue_id=optional_int(item.get("venueId")),
            venue=item.get("venue"),
            home_team_id=int(item["homeId"]),
            home_team=str(item["homeTeam"]),
            home_conference=item.get("homeConference"),
            home_points=optional_int(item.get("homePoints")),
            home_pregame_elo=optional_int(item.get("homePregameElo")),
            away_team_id=int(item["awayId"]),
            away_team=str(item["awayTeam"]),
            away_conference=item.get("awayConference"),
            away_points=optional_int(item.get("awayPoints")),
            away_pregame_elo=optional_int(item.get("awayPregameElo")),
            excitement_index=optional_float(item.get("excitementIndex")),
            notes=item.get("notes"),
        )


@dataclass(frozen=True, slots=True)
class Player:
    player_id: str
    season: int
    first_name: str
    last_name: str
    team: str
    position: str | None
    jersey: int | None
    height: float | None
    weight: int | None
    class_year: int | None

    @property
    def name(self) -> str: return f"{self.first_name} {self.last_name}".strip()
    @classmethod
    def from_cfbd(cls, item, season):
        return cls(str(item['id']),season,str(item.get('firstName') or ''),str(item.get('lastName') or ''),
                   str(item['team']),item.get('position'),optional_int(item.get('jersey')),
                   optional_float(item.get('height')),optional_int(item.get('weight')),optional_int(item.get('year')))


@dataclass(frozen=True, slots=True)
class PollRanking:
    season: int
    season_type: str
    week: int
    poll: str
    is_final: bool
    rank: int
    team_id: int | None
    school: str
    conference: str | None
    first_place_votes: int | None
    points: int | None


@dataclass(frozen=True, slots=True)
class SyncDatasetResult:
    dataset: str
    count: int
    status: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class SyncReport:
    season: int
    started_at: datetime
    finished_at: datetime
    datasets: tuple[SyncDatasetResult, ...]

    @property
    def succeeded(self) -> bool:
        return all(item.status in {"success", "skipped"} for item in self.datasets)
