"""Sharing and discovery metadata for college-football pages.

Every preview page here assembles material that exists nowhere else in one
place: series history, unit comparisons, kickoff weather, attributed reporting.
None of that survives a link being pasted into a group chat unless the page
says what it is. This module builds that description once, per page kind, so a
card rendered by a chat client, a search crawler, and the on-page ``<title>``
cannot drift apart.

Descriptions are assembled from stored data rather than written by hand. A page
with no games, no record, and no reporting says so plainly instead of inheriting
a generic site blurb, because a card that promises detail the page does not have
is worse than no card at all.

Structured data follows schema.org. The vocabulary is deliberately conservative:
only ``SportsEvent``, ``SportsTeam``, and ``Person`` are emitted, and only from
fields the repository actually owns. Speculative markup is a liability, not a
ranking advantage.
"""

from __future__ import annotations

from typing import Any


SITE_NAME = "Sports News Aggregator"

#: Chat clients and search engines truncate well past this, but a description
#: that reads as a complete thought beats one cut mid-clause.
DESCRIPTION_LIMIT = 200


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _sentence(*parts: Any) -> str:
    """Join non-empty clauses into one description, trimmed at a word boundary."""
    text = " ".join(_clean(part) for part in parts if _clean(part))
    if len(text) <= DESCRIPTION_LIMIT:
        return text
    clipped = text[:DESCRIPTION_LIMIT].rsplit(" ", 1)[0]
    return clipped.rstrip(",;:.") + "…"


def page_meta(title: str, description: str, *, path: str = "",
              image: str | None = None, kind: str = "website",
              structured_data: dict[str, Any] | list[dict[str, Any]] | None = None,
              ) -> dict[str, Any]:
    """One page's sharing card, canonical path, and optional structured data."""
    return {
        "title": _clean(title),
        "description": _sentence(description),
        "path": path,
        "image": image,
        "kind": kind,
        "site_name": SITE_NAME,
        "structured_data": structured_data,
    }


def _record_label(record: dict[str, Any] | None) -> str:
    if not record:
        return ""
    wins, losses = record.get("wins"), record.get("losses")
    if wins is None or losses is None:
        return ""
    return f"{wins}-{losses}"


def _count(number: int, singular: str, plural: str | None = None) -> str:
    """`1 game`, not `1 games`. Counts appear in cards that people read."""
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


def _team_label(brand: dict[str, Any] | None, school: str | None = None) -> str:
    brand = brand or {}
    return _clean(brand.get("school") or school)


def game_meta(game: dict[str, Any], away_brand: dict[str, Any] | None,
              home_brand: dict[str, Any] | None, *,
              weather: dict[str, Any] | None = None,
              story_count: int = 0) -> dict[str, Any]:
    """A matchup card that names the game, the window, and what the page adds."""
    away = _team_label(away_brand, game.get("away_team"))
    home = _team_label(home_brand, game.get("home_team"))
    title = f"{away} at {home}"
    if game.get("neutral_site"):
        title = f"{away} vs {home}"

    window = _clean(game.get("start_label"))
    venue = _clean(game.get("venue"))
    where = f"{venue}." if venue else ""

    # Say what this page has that a scoreboard does not. Only claim the layers
    # that actually populated for this game.
    layers = ["series history", "unit-by-unit comparison"]
    if weather and weather.get("available"):
        layers.append("kickoff forecast")
    if story_count:
        layers.append("attributed reporting")
    adds = "Preview with " + ", ".join(layers) + "."

    description = _sentence(f"{title}.", window + "." if window else "", where, adds)
    return page_meta(
        f"{title} | Game Preview",
        description,
        path=f"/college-football/games/{game.get('game_id')}/",
        image=(home_brand or {}).get("logo") or (away_brand or {}).get("logo"),
        kind="article",
        structured_data=sports_event_ld(game, away_brand, home_brand),
    )


def team_meta(team: dict[str, Any], brand: dict[str, Any] | None, season: int, *,
              record: dict[str, Any] | None = None,
              next_game: dict[str, Any] | None = None) -> dict[str, Any]:
    school = _team_label(brand, team.get("school"))
    conference = _clean(team.get("conference") or (brand or {}).get("conference"))
    record_label = _record_label(record)

    standing = " ".join(part for part in (record_label, conference) if part)
    opening = f"{school} {season}."
    if standing:
        opening = f"{school} {season}, {standing}."

    upcoming = ""
    if next_game:
        # Schedule rows are raw game records, so which side is the opponent
        # depends on where this team is playing.
        team_id = team.get("team_id")
        if next_game.get("home_team_id") == team_id:
            opponent, prefix = _clean(next_game.get("away_team")), "vs"
        else:
            opponent, prefix = _clean(next_game.get("home_team")), "at"
        if opponent:
            upcoming = f"Next: {prefix} {opponent}."

    description = _sentence(
        opening, upcoming,
        "Schedule, roster, returning production, depth, and team reporting.")
    return page_meta(
        f"{school} | {season} Team Intelligence",
        description,
        path=f"/college-football/teams/{team.get('team_id')}/",
        image=(brand or {}).get("logo"),
        kind="profile",
        structured_data=sports_team_ld(team, brand),
    )


def player_meta(player: dict[str, Any], brand: dict[str, Any] | None) -> dict[str, Any]:
    name = _clean(player.get("name"))
    position = _clean(player.get("position"))
    school = _team_label(brand, player.get("team"))
    year = _clean(player.get("year") or player.get("class"))

    identity = ", ".join(part for part in (position, school, year) if part)
    description = _sentence(
        f"{name}." if not identity else f"{name} — {identity}.",
        "Career path, season statistics, grades, transfer and draft events, and reporting.")
    return page_meta(
        f"{name} | College Football Player",
        description,
        path=f"/college-football/players/{player.get('player_id')}/",
        image=(brand or {}).get("logo"),
        kind="profile",
        structured_data=person_ld(player, brand),
    )


def conference_meta(conference: dict[str, Any], season: int, *,
                    game_count: int = 0) -> dict[str, Any]:
    name = _clean(conference.get("conference"))
    games = f"{_count(game_count, 'upcoming game')}." if game_count else ""
    description = _sentence(
        f"{name}, {season}.", games,
        "Standings, player leaders, historical context, and conference reporting.")
    return page_meta(
        f"{name} | College Football",
        description,
        path=f"/college-football/conferences/{conference.get('slug')}/",
        kind="website",
    )


def scoreboard_meta(season: int, *, day: str | None = None,
                    games: int = 0) -> dict[str, Any]:
    """The card for one day's slate."""
    when = f"{day}." if day else ""
    count = f"{_count(games, 'game')}." if games else ""
    return page_meta(
        f"Scoreboard{f' — {day}' if day else ''} | College Football",
        _sentence(f"College football scoreboard for {season}.", when, count,
                  "Every game on the day, with the full matchup preview one click away."),
        path="/college-football/scoreboard/" + (f"?date={day}" if day else ""),
        kind="website")


def today_meta(season: int, *, game_count: int = 0, story_count: int = 0) -> dict[str, Any]:
    counts = []
    if game_count:
        counts.append(f"{_count(game_count, 'game')} worth watching")
    if story_count:
        counts.append(_count(story_count, "clustered story", "clustered stories"))
    detail = ", ".join(counts) + "." if counts else ""
    description = _sentence(
        f"College football for the {season} season.", detail,
        "Ranked matchups, reporting attributed to its source, and team intelligence.")
    return page_meta(
        "College Football Today | Sports News Aggregator",
        description,
        path="/college-football/",
        kind="website",
    )


def playoff_meta(season: int) -> dict[str, Any]:
    return page_meta(
        f"{season} College Football Playoff projection | College Football",
        f"Simulated {season} season: odds to make the 12-team College Football Playoff, "
        "win a conference title, earn a bye, and win the national championship.",
        path=f"/college-football/playoff/?season={season}",
        kind="website",
    )


def elo_meta(season: int, *, rated_teams: int = 0) -> dict[str, Any]:
    count = f"{_count(rated_teams, 'rated FBS team')}." if rated_teams else ""
    return page_meta(
        f"{season} FBS Elo ratings | College Football",
        _sentence(
            f"Current {season} college football Elo ratings.", count,
            "National ranks, conference strength, rating movement, and season ranges."),
        path=f"/college-football/elo/?season={season}",
        kind="website",
    )


# ---------------------------------------------------------------------------
# schema.org structured data
# ---------------------------------------------------------------------------

def sports_team_ld(team: dict[str, Any],
                   brand: dict[str, Any] | None = None) -> dict[str, Any]:
    brand = brand or {}
    payload: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "SportsTeam",
        "name": _team_label(brand, team.get("school")),
        "sport": "College Football",
    }
    if brand.get("logo"):
        payload["logo"] = brand["logo"]
    if team.get("mascot") or brand.get("mascot"):
        payload["alternateName"] = _clean(team.get("mascot") or brand.get("mascot"))
    conference = _clean(team.get("conference") or brand.get("conference"))
    if conference:
        payload["memberOf"] = {"@type": "SportsOrganization", "name": conference}
    return payload


def sports_event_ld(game: dict[str, Any], away_brand: dict[str, Any] | None,
                    home_brand: dict[str, Any] | None) -> dict[str, Any]:
    away = _team_label(away_brand, game.get("away_team"))
    home = _team_label(home_brand, game.get("home_team"))
    payload: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "SportsEvent",
        "name": f"{away} at {home}",
        "sport": "College Football",
        # The schema.org convention is that competitor order is not meaningful,
        # so home advantage is expressed by `homeTeam`/`awayTeam` instead.
        "awayTeam": sports_team_ld({"school": away}, away_brand),
        "homeTeam": sports_team_ld({"school": home}, home_brand),
    }
    if game.get("start_date"):
        payload["startDate"] = game["start_date"]
    venue = _clean(game.get("venue"))
    if venue:
        location: dict[str, Any] = {"@type": "Place", "name": venue}
        city, state = _clean(game.get("venue_city")), _clean(game.get("venue_state"))
        if city or state:
            location["address"] = {
                "@type": "PostalAddress",
                **({"addressLocality": city} if city else {}),
                **({"addressRegion": state} if state else {}),
            }
        payload["location"] = location
    if game.get("completed"):
        payload["eventStatus"] = "https://schema.org/EventScheduled"
    return payload


def person_ld(player: dict[str, Any],
              brand: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": _clean(player.get("name")),
    }
    position = _clean(player.get("position"))
    if position:
        payload["jobTitle"] = position
    school = _team_label(brand, player.get("team"))
    if school:
        payload["memberOf"] = {"@type": "SportsTeam", "name": school}
    if player.get("height_label"):
        payload["height"] = _clean(player["height_label"])
    if player.get("weight"):
        payload["weight"] = f"{_clean(player['weight'])} lb"
    return payload
