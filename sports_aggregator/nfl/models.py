"""Canonical NFL entities normalized from observed nflverse release schemas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sports_aggregator.nfl.naming import canon_position, canon_team


def _missing(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip() in {"", "nan", "NaN", "NaT", "<NA>", "None"}


def optional_text(value: Any) -> str | None:
    return None if _missing(value) else str(value).strip()


def optional_int(value: Any) -> int | None:
    if _missing(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def optional_float(value: Any) -> float | None:
    if _missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Team:
    abbreviation: str
    name: str
    nickname: str
    conference: str
    division: str
    color: str | None
    alternate_color: str | None
    logo_url: str | None

    @classmethod
    def from_nflverse(cls, row: Mapping[str, Any]) -> "Team":
        return cls(
            abbreviation=canon_team(row.get("team_abbr")),
            name=str(row["team_name"]).strip(),
            nickname=str(row["team_nick"]).strip(),
            conference=str(row["team_conf"]).strip(),
            division=str(row["team_division"]).strip(),
            color=optional_text(row.get("team_color")),
            alternate_color=optional_text(row.get("team_color2")),
            logo_url=optional_text(row.get("team_logo_espn") or row.get("team_logo_wikipedia")),
        )


@dataclass(frozen=True, slots=True)
class Game:
    game_id: str
    season: int
    season_type: str
    week: int
    game_date: str
    game_time: str | None
    away_team: str
    home_team: str
    away_score: int | None
    home_score: int | None
    overtime: bool
    division_game: bool
    stadium: str | None
    roof: str | None
    surface: str | None
    temperature: float | None
    wind: float | None
    spread_line: float | None
    total_line: float | None
    away_rest: int | None = None
    home_rest: int | None = None
    away_moneyline: float | None = None
    home_moneyline: float | None = None
    away_spread_odds: float | None = None
    home_spread_odds: float | None = None
    under_odds: float | None = None
    over_odds: float | None = None
    away_coach: str | None = None
    home_coach: str | None = None
    weekday: str | None = None

    @property
    def completed(self) -> bool:
        return self.away_score is not None and self.home_score is not None

    @classmethod
    def from_nflverse(cls, row: Mapping[str, Any]) -> "Game":
        return cls(
            game_id=str(row["game_id"]), season=int(row["season"]),
            season_type=str(row["game_type"]), week=int(row["week"]),
            game_date=str(row["gameday"]), game_time=optional_text(row.get("gametime")),
            away_team=canon_team(row.get("away_team")), home_team=canon_team(row.get("home_team")),
            away_score=optional_int(row.get("away_score")), home_score=optional_int(row.get("home_score")),
            overtime=bool(optional_int(row.get("overtime")) or 0),
            division_game=bool(optional_int(row.get("div_game")) or 0),
            stadium=optional_text(row.get("stadium")), roof=optional_text(row.get("roof")),
            surface=optional_text(row.get("surface")), temperature=optional_float(row.get("temp")),
            wind=optional_float(row.get("wind")), spread_line=optional_float(row.get("spread_line")),
            total_line=optional_float(row.get("total_line")),
            away_rest=optional_int(row.get("away_rest")), home_rest=optional_int(row.get("home_rest")),
            away_moneyline=optional_float(row.get("away_moneyline")),
            home_moneyline=optional_float(row.get("home_moneyline")),
            away_spread_odds=optional_float(row.get("away_spread_odds")),
            home_spread_odds=optional_float(row.get("home_spread_odds")),
            under_odds=optional_float(row.get("under_odds")), over_odds=optional_float(row.get("over_odds")),
            away_coach=optional_text(row.get("away_coach")), home_coach=optional_text(row.get("home_coach")),
            weekday=optional_text(row.get("weekday")),
        )


@dataclass(frozen=True, slots=True)
class Player:
    season: int
    player_id: str
    team: str
    full_name: str
    first_name: str
    last_name: str
    position: str | None
    depth_position: str | None
    jersey_number: int | None
    status: str | None
    birth_date: str | None
    height: float | None
    weight: int | None
    college: str | None
    years_experience: int | None
    headshot_url: str | None
    pff_id: str | None
    pfr_id: str | None
    espn_id: str | None

    @classmethod
    def from_nflverse(cls, row: Mapping[str, Any]) -> "Player":
        player_id = optional_text(row.get("gsis_id"))
        if not player_id:
            raise ValueError("nflverse roster row has no gsis_id")
        return cls(
            season=int(row["season"]), player_id=player_id,
            team=canon_team(row.get("team")), full_name=str(row["full_name"]).strip(),
            first_name=str(row.get("first_name") or "").strip(),
            last_name=str(row.get("last_name") or "").strip(),
            position=optional_text(canon_position(row.get("position"))),
            depth_position=optional_text(row.get("depth_chart_position")),
            jersey_number=optional_int(row.get("jersey_number")), status=optional_text(row.get("status")),
            birth_date=optional_text(row.get("birth_date")), height=optional_float(row.get("height")),
            weight=optional_int(row.get("weight")), college=optional_text(row.get("college")),
            years_experience=optional_int(row.get("years_exp")), headshot_url=optional_text(row.get("headshot_url")),
            pff_id=optional_text(row.get("pff_id")), pfr_id=optional_text(row.get("pfr_id")),
            espn_id=optional_text(row.get("espn_id")),
        )


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
