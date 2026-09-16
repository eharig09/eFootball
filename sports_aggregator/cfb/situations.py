"""Situational context: the spots that most often explain an unexpected result.

Efficiency numbers describe how good two teams are. They do not describe the
circumstances a game is played in, and circumstance is where surprises come
from. These are the situations college-football analysts consistently point to,
restricted to the ones this store can actually compute:

* **Look-ahead** -- a weak opponent immediately before a much harder one.
* **Letdown** -- the week after a major win, when attention is hardest to hold.
* **Sandwich** -- a hard game on both sides of this one.
* **Revenge** -- a rematch with the team that beat them last season.
* **Rest imbalance** -- one team off a bye, or on a short week.
* **Travel** -- distance, time zones, and an early kickoff for a western team.
* **Altitude** -- a sea-level team visiting a high-elevation venue.
* **Availability** -- injury and depth-chart reporting for either side.

Every signal is a description of circumstance with the facts attached. None of
them predicts a result, and a "trap spot" is a statement about the calendar, not
about who will win.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.providers.geo import haversine_miles, timezone_for, zone_index


#: Elo gaps that define "much weaker" and "much stronger" opponents.
TRAP_OPPONENT_GAP = 250
TRAP_NEXT_GAP = 150
#: An opponent this far above a team is a "major" test, for letdown purposes.
MAJOR_OPPONENT_GAP = 120

#: A trip beyond this many miles is worth naming.
LONG_TRIP_MILES = 1200
#: Time-zone shifts of this size or more affect kickoff body clock.
NOTABLE_TIMEZONE_SHIFT = 2
#: Kickoffs at or before this local hour are early for a travelling team.
EARLY_KICKOFF_HOUR = 13

#: CFBD publishes venue elevation in metres. 1,500 m is roughly 4,900 feet,
#: which is where altitude is conventionally treated as a factor.
ALTITUDE_METRES = 1500
LOWLAND_METRES = 500

#: Rest, in days between kickoffs. Six days is a routine Saturday-to-Friday
#: turnaround; five or fewer is a genuinely short week.
SHORT_WEEK_DAYS = 5
BYE_WEEK_DAYS = 12

#: Topics that describe availability rather than opinion.
AVAILABILITY_TOPICS = ("INJURY", "DEPTH_CHART", "ROSTER")

#: Content types that can carry availability news.
#:
#: The topic alone let in a Big 12 power-rankings video and a fall-camp
#: podcast, both of which mention rosters without reporting anything about
#: one. Availability is reported or said on the record; it is not previewed,
#: reacted to, or ranked.
AVAILABILITY_TYPES = ("REPORTING", "INTERVIEW")

#: Most teams an item may resolve to and still be about a team.
#:
#: "BIG 12 POWER RANKINGS ARE HERE" resolved to six at once. Nothing that
#: mentions six programmes is telling a reader who is available on Saturday.
AVAILABILITY_MAX_TEAMS = 3

def _timezone_for(longitude: float | None) -> str | None:
    return timezone_for(longitude)


def _zone_index(name: str | None) -> int | None:
    return zone_index(name)


def _venues(repository: CFBRepository) -> dict[int, dict[str, Any]]:
    try:
        return repository.team_venues()
    except Exception:
        return {}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _team_games(connection, season: int, team_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        """SELECT game_id,week,start_date,completed,home_team,away_team,
           home_team_id,away_team_id,home_points,away_points
           FROM games WHERE season=? AND (home_team_id=? OR away_team_id=?)
           ORDER BY start_date""", (season, team_id, team_id))]


def schedule_spot(repository: CFBRepository, game: dict[str, Any],
                  elo: dict[int, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Look-ahead, letdown, sandwich, revenge and rest spots for either team."""
    elo = elo if elo is not None else repository.team_elo(game["season"])
    kickoff = _parse(game.get("start_date"))
    signals: list[dict[str, Any]] = []
    with repository._reader() as connection:
        for team_id, team_name, opponent_id, opponent_name in (
            (game["home_team_id"], game["home_team"], game["away_team_id"], game["away_team"]),
            (game["away_team_id"], game["away_team"], game["home_team_id"], game["home_team"]),
        ):
            schedule = _team_games(connection, game["season"], team_id)
            index = next((position for position, row in enumerate(schedule)
                          if row["game_id"] == game["game_id"]), None)
            if index is None:
                continue
            previous = schedule[index - 1] if index > 0 else None
            following = schedule[index + 1] if index + 1 < len(schedule) else None
            own_elo = (elo.get(team_id) or {}).get("elo")
            this_elo = (elo.get(opponent_id) or {}).get("elo")

            def opponent_of(row):
                other = (row["away_team_id"] if row["home_team_id"] == team_id
                         else row["home_team_id"])
                name = (row["away_team"] if row["home_team_id"] == team_id
                        else row["home_team"])
                return other, name

            next_elo = next_name = previous_elo = previous_name = None
            if following:
                next_id, next_name = opponent_of(following)
                next_elo = (elo.get(next_id) or {}).get("elo")
            if previous:
                previous_id, previous_name = opponent_of(previous)
                previous_elo = (elo.get(previous_id) or {}).get("elo")

            if None not in (own_elo, this_elo, next_elo):
                if own_elo - this_elo >= TRAP_OPPONENT_GAP and next_elo - this_elo >= TRAP_NEXT_GAP:
                    signals.append({
                        "type": "LOOK_AHEAD", "team": team_name,
                        "headline": f"{team_name} plays {next_name} next",
                        "detail": (f"{opponent_name} rates {own_elo - this_elo:.0f} Elo below "
                                   f"{team_name}, while {next_name} rates "
                                   f"{next_elo - this_elo:.0f} above this week's opponent"),
                    })
                if (previous_elo is not None
                        and own_elo - this_elo >= TRAP_OPPONENT_GAP
                        and previous_elo - this_elo >= TRAP_NEXT_GAP
                        and next_elo - this_elo >= TRAP_NEXT_GAP):
                    signals.append({
                        "type": "SANDWICH", "team": team_name,
                        "headline": f"{team_name} is sandwiched between {previous_name} and {next_name}",
                        "detail": ("a lesser opponent with a much harder game on either side"),
                    })

            # Letdown needs a played result, so it appears once the season starts.
            if previous and previous["completed"]:
                own_points = (previous["home_points"] if previous["home_team_id"] == team_id
                              else previous["away_points"])
                other_points = (previous["away_points"] if previous["home_team_id"] == team_id
                                else previous["home_points"])
                won = (own_points or 0) > (other_points or 0)
                if won and previous_elo is not None and own_elo is not None:
                    if previous_elo - own_elo >= -MAJOR_OPPONENT_GAP:
                        signals.append({
                            "type": "LETDOWN", "team": team_name,
                            "headline": f"{team_name} is coming off a win over {previous_name}",
                            "detail": (f"beat a team rated {previous_elo:.0f} Elo last week, "
                                       f"then faces {opponent_name}"),
                        })

            # Revenge: the same opponent beat them in the previous season.
            prior = connection.execute(
                """SELECT home_team_id,away_team_id,home_points,away_points,season
                   FROM games WHERE season=? AND completed=1
                   AND ((home_team_id=? AND away_team_id=?) OR (home_team_id=? AND away_team_id=?))
                   ORDER BY start_date DESC LIMIT 1""",
                (game["season"] - 1, team_id, opponent_id, opponent_id, team_id)).fetchone()
            if prior:
                own_points = (prior["home_points"] if prior["home_team_id"] == team_id
                              else prior["away_points"])
                other_points = (prior["away_points"] if prior["home_team_id"] == team_id
                                else prior["home_points"])
                if own_points is not None and other_points is not None and other_points > own_points:
                    signals.append({
                        "type": "REVENGE", "team": team_name,
                        "headline": f"{team_name} lost this matchup last season",
                        "detail": (f"{opponent_name} won {other_points}-{own_points} "
                                   f"in {prior['season']}"),
                    })

            # Rest imbalance.
            if previous and kickoff:
                last_kickoff = _parse(previous["start_date"])
                if last_kickoff:
                    days = (kickoff - last_kickoff).days
                    if days <= SHORT_WEEK_DAYS:
                        signals.append({
                            "type": "SHORT_WEEK", "team": team_name,
                            "headline": f"{team_name} is on {days} days rest",
                            "detail": f"played {previous_name} {days} days earlier",
                        })
                    elif days >= BYE_WEEK_DAYS:
                        signals.append({
                            "type": "EXTRA_REST", "team": team_name,
                            "headline": f"{team_name} is coming off a bye",
                            "detail": f"{days} days since playing {previous_name}",
                        })
    return signals


def travel_burden(repository: CFBRepository, game: dict[str, Any]) -> dict[str, Any] | None:
    """Distance, time-zone change, altitude and kickoff timing for the visitor."""
    venues = _venues(repository)
    home = venues.get(game["home_team_id"])
    away = venues.get(game["away_team_id"])
    if not home or not away:
        return None
    miles = round(haversine_miles(
        (away["latitude"], away["longitude"]), (home["latitude"], home["longitude"])))
    away_zone = _timezone_for(away["longitude"])
    home_zone = _timezone_for(home["longitude"])
    shift = None
    if _zone_index(away_zone) is not None and _zone_index(home_zone) is not None:
        shift = _zone_index(home_zone) - _zone_index(away_zone)

    notes = [f"{game['away_team']} travels {miles:,} miles"]
    if shift:
        direction = "east" if shift > 0 else "west"
        notes[0] += f" and {abs(shift)} time zone{'s' if abs(shift) != 1 else ''} {direction}"

    # An early eastern kickoff is materially earlier on a western body clock.
    kickoff = _parse(game.get("start_date"))
    body_clock = None
    if kickoff and shift and shift > 0:
        local_hour = kickoff.hour - 4  # UTC to eastern, close enough to describe
        if local_hour <= EARLY_KICKOFF_HOUR:
            body_clock = (f"kickoff is {abs(shift)} hour{'s' if abs(shift) != 1 else ''} "
                          f"earlier on {game['away_team']}'s body clock")
            notes.append(body_clock)

    altitude = None
    home_elevation, away_elevation = home.get("elevation"), away.get("elevation")
    if (home_elevation is not None and home_elevation >= ALTITUDE_METRES
            and (away_elevation is None or away_elevation <= LOWLAND_METRES)):
        altitude = (f"{home.get('venue_name') or 'the venue'} sits at "
                    f"{round(home_elevation * 3.28081):,} feet")
        notes.append(altitude)

    notable = (miles >= LONG_TRIP_MILES
               or (shift is not None and abs(shift) >= NOTABLE_TIMEZONE_SHIFT)
               or bool(altitude) or bool(body_clock))
    return {
        "miles": miles, "away_zone": away_zone, "home_zone": home_zone,
        "timezone_shift": shift, "notable": notable,
        "detail": "; ".join(notes),
        "body_clock": body_clock, "altitude": altitude,
        "venue": home.get("venue_name"), "dome": bool(home.get("dome")),
        "elevation_metres": home_elevation,
    }


def availability_reports(repository: CFBRepository, game: dict[str, Any],
                         limit: int = 8) -> list[dict[str, Any]]:
    """Injury, depth-chart and roster items resolved to either team."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    topics = ",".join("?" for _ in AVAILABILITY_TOPICS)
    types = ",".join("?" for _ in AVAILABILITY_TYPES)
    with repository._reader() as connection:
        try:
            rows = connection.execute(
                f"""SELECT c.content_id,c.platform,c.content_type,c.title,c.body_text,
                    c.canonical_url,c.published_at,c.source_role,c.publisher_name,
                    c.author_name,ct.confidence,t.school,t.color,e.name source_name,
                    MIN(tp.topic) topic
                    FROM content_items c
                    JOIN content_sport_decisions sd USING(content_id)
                    JOIN content_topics tp ON tp.content_id=c.content_id
                    JOIN content_teams ct ON ct.content_id=c.content_id
                    JOIN teams t ON t.team_id=ct.team_id
                    LEFT JOIN source_entities e USING(source_entity_id)
                    WHERE sd.eligible=1 AND tp.topic IN ({topics})
                    AND c.content_type IN ({types})
                    AND ct.team_id IN (?,?) AND ct.confidence>=0.75
                    AND c.published_at>=?
                    -- The subject, not a mention. An item carrying another
                    -- school more strongly is that school's news: "Former UNC
                    -- QB named starter at Wake Forest" reads as Wake Forest at
                    -- 0.98 and North Carolina at 0.90, and it is Wake Forest's.
                    AND ct.confidence >= (SELECT MAX(o.confidence)
                                          FROM content_teams o
                                          WHERE o.content_id=c.content_id)
                    AND (SELECT COUNT(*) FROM content_teams o
                         WHERE o.content_id=c.content_id) <= ?
                    -- One row per item: an item tagged both DEPTH_CHART and
                    -- ROSTER was being listed twice, which is the duplicate
                    -- visible on the page.
                    GROUP BY c.content_id, ct.team_id
                    ORDER BY c.published_at DESC LIMIT ?""",
                (*AVAILABILITY_TOPICS, *AVAILABILITY_TYPES,
                 game["home_team_id"], game["away_team_id"],
                 cutoff, AVAILABILITY_MAX_TEAMS, limit)).fetchall()
        except Exception:
            return []
    from sports_aggregator.social.content import display_text, display_timestamp, label_linked_piece
    reports = []
    for row in rows:
        item = dict(row)
        item["headline"] = display_text(item, limit=120)
        item["published_label"] = display_timestamp(item.get("published_at"))
        item.pop("body_text", None)
        reports.append(label_linked_piece(item))
    return reports


def game_situation(repository: CFBRepository, game: dict[str, Any],
                   elo: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Everything situational a preview should say about one game."""
    spots = schedule_spot(repository, game, elo)
    travel = travel_burden(repository, game)
    availability = availability_reports(repository, game)
    notes = [spot["headline"] for spot in spots]
    if travel and travel["notable"]:
        notes.append(travel["detail"])
    if availability:
        notes.append(f"{len(availability)} availability report"
                     f"{'s' if len(availability) != 1 else ''} in the last two weeks")
    return {"schedule_spots": spots, "travel": travel,
            "availability": availability, "notes": notes}
