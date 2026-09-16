"""Build descriptive, leak-resistant context for an NFL matchup page."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.pff import NFLPFFService
from sports_aggregator.nfl.personnel import significant_movements
from sports_aggregator.nfl.usage import team_usage_context
from sports_aggregator.nfl.teams import unit_continuity
from sports_aggregator.nfl.weather import kickoff_utc, weather_for_game
from sports_aggregator.providers.geo import haversine_miles, timezone_for, zone_index


#: Elo gaps that define "much weaker" and "much stronger" opponents -- the
#: same calibration CFB's trap-spot detector uses, and NFL's own current
#: rating spread (roughly 570 points top to bottom across 32 teams) sits in
#: the same relative range, so the same thresholds carry over without
#: needing a separate NFL-specific tuning pass.
TRAP_OPPONENT_GAP = 250
TRAP_NEXT_GAP = 150
MAJOR_OPPONENT_GAP = 120

#: A trip beyond this many miles is worth naming.
LONG_TRIP_MILES = 1200
#: Time-zone shifts of this size or more affect kickoff body clock.
NOTABLE_TIMEZONE_SHIFT = 2
#: Kickoffs at or before this local hour are early for a travelling team.
EARLY_KICKOFF_HOUR = 13
#: nflverse publishes venue elevation in feet already at the schedule level
#: for most stadiums, but team_venues stores metres to match CFBD/CFB's
#: convention; 1,500 m is roughly 4,900 feet, the conventional altitude
#: threshold.
ALTITUDE_METRES = 1500
LOWLAND_METRES = 500


UNIT_METRICS = (
    ("Overall", "epa_per_play", "defensive_epa_allowed", "signed2"),
    ("Dropback", "pass_epa_per_play", "defensive_pass_epa_allowed", "signed2"),
    ("Rushing", "rush_epa_per_play", "defensive_rush_epa_allowed", "signed2"),
    ("Success rate", "success_rate", "defensive_success_allowed", "rate"),
    ("Explosive rate", "explosive_rate", "defensive_explosive_allowed", "rate"),
)

PRODUCTION_METRICS = (
    ("Points / game", "points_per_game", "f1", False),
    ("Points allowed", "points_allowed_per_game", "f1", True),
    ("Pass yards / game", "passing_yards_per_game", "f1", False),
    ("Rush yards / game", "rushing_yards_per_game", "f1", False),
    ("Sacks allowed / game", "sacks_allowed_per_game", "f1", True),
    ("Turnovers / game", "turnovers_per_game", "f1", True),
)

LEADER_METRICS = (
    ("Passing", "passing_yards", "Passing yards"),
    ("Pass TD", "passing_tds", "Passing touchdowns"),
    ("Rushing", "rushing_yards", "Rushing yards"),
    ("Carries", "carries", "Carries"),
    ("Receiving", "receiving_yards", "Receiving yards"),
    ("Targets", "targets", "Targets"),
    ("Receptions", "receptions", "Receptions"),
    ("Pressure", "def_sacks", "Sacks"),
    ("QB hits", "def_qb_hits", "Quarterback hits"),
    ("Takeaways", "def_interceptions", "Interceptions"),
)


def _ranks(profiles: list[dict[str, Any]], key: str, *, lower: bool = False) -> dict[str, int]:
    available = [row for row in profiles if row.get(key) is not None]
    available.sort(key=lambda row: row[key], reverse=not lower)
    return {row["team"]: index for index, row in enumerate(available, 1)}


def _unit_card(profiles: dict[str, dict[str, Any]], ranks: dict[str, dict[str, int]],
               offense: str, defense: str) -> dict[str, Any]:
    attacking = profiles.get(offense, {})
    resisting = profiles.get(defense, {})
    rows = []
    for label, offense_key, defense_key, value_format in UNIT_METRICS:
        offense_rank = ranks.get(offense_key, {}).get(offense)
        defense_rank = ranks.get(defense_key, {}).get(defense)
        if offense_rank is None or defense_rank is None:
            lean = None
        elif offense_rank + 5 <= defense_rank:
            lean = offense
        elif defense_rank + 5 <= offense_rank:
            lean = defense
        else:
            lean = "Even"
        rows.append({
            "label": label,
            "offense_value": attacking.get(offense_key), "offense_rank": offense_rank,
            "defense_value": resisting.get(defense_key), "defense_rank": defense_rank,
            "format": value_format, "lean": lean,
            "separation": (abs(offense_rank - defense_rank)
                           if offense_rank is not None and defense_rank is not None else None),
            "strength": ("strong" if offense_rank is not None and defense_rank is not None
                         and abs(offense_rank - defense_rank) >= 12 else
                         "moderate" if offense_rank is not None and defense_rank is not None
                         and abs(offense_rank - defense_rank) >= 5 else "even"),
        })
    return {"offense": offense, "defense": defense, "rows": rows}


def _recent(repository: NFLRepository, game: dict[str, Any], team: str,
            identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = repository.recent_team_games(
        game["season"], team, before_game_id=game["game_id"], limit=5,
    )
    if not rows:
        previous = [row for row in repository.schedule(game["season"] - 1, team=team)
                    if row["completed"]]
        rows = list(reversed(previous[-5:]))
        for row in rows:
            away = row["away_team"] == team
            own = row["away_score"] if away else row["home_score"]
            other = row["home_score"] if away else row["away_score"]
            row["opponent"] = row["home_team"] if away else row["away_team"]
            row["site"] = "at" if away else "vs"
            row["result"] = "W" if own > other else ("L" if own < other else "T")
            row["score"] = f"{own}-{other}"
    for row in rows:
        identity = identities.get(row["opponent"], {})
        row["opponent_logo"] = identity.get("logo_url")
        row["opponent_color"] = identity.get("color")
    return rows


def _leaders(repository: NFLRepository, season: int, team: str, before_week: int,
             *, stats_season: int | None = None) -> list[dict[str, Any]]:
    output = []
    stats_season = stats_season or season
    current_ids = {row["player_id"] for row in repository.team_roster(season, team)}
    rows = repository.player_leaders_for_metrics(
        stats_season, (metric for _, metric, _ in LEADER_METRICS), team=team,
        before_week=before_week if stats_season == season else None,
    )
    by_metric = {metric: [] for _, metric, _ in LEADER_METRICS}
    for row in rows:
        by_metric.setdefault(row["metric"], []).append(row)
    for label, metric, value_label in LEADER_METRICS:
        candidates = by_metric.get(metric, [])
        if stats_season != season:
            candidates = [row for row in candidates if row["player_id"] in current_ids]
        if not candidates:
            continue
        for rank, leader in enumerate(candidates[:2], 1):
            output.append({
                **leader, "category": label, "category_rank": rank,
                "value_label": value_label, "stat_season": stats_season,
                "player_url": f"/nfl/players/{leader['player_id']}/?season={stats_season}",
            })
    return output


def _game_shape(production: dict[str, dict[str, Any]], recent: dict[str, list[dict[str, Any]]],
                situational: dict[str, dict[str, Any]], game: dict[str, Any],
                away: str, home: str) -> dict[str, Any]:
    """Blend season, recent, and market context without claiming a betting model."""
    away_attack = production[away].get("points_per_game")
    home_defense = production[home].get("points_allowed_per_game")
    home_attack = production[home].get("points_per_game")
    away_defense = production[away].get("points_allowed_per_game")
    away_points = ((away_attack + home_defense) / 2
                   if away_attack is not None and home_defense is not None else None)
    home_points = ((home_attack + away_defense) / 2
                   if home_attack is not None and away_defense is not None else None)
    season_away = away_points; season_home = home_points
    components = [{"label": "Season offense / opponent defense",
                   "away": season_away, "home": season_home, "weight": .7}]
    def recent_scoring(team: str) -> float | None:
        values = []
        for row in recent.get(team, [])[:3]:
            try:
                values.append(float(str(row["score"]).split("-", 1)[0]))
            except (KeyError, TypeError, ValueError):
                continue
        return sum(values) / len(values) if values else None
    recent_away, recent_home = recent_scoring(away), recent_scoring(home)
    if recent_away is not None and recent_home is not None:
        components.append({"label": "Last-three scoring form", "away": recent_away,
                           "home": recent_home, "weight": .2})
    if game.get("total_line") is not None and game.get("spread_line") is not None:
        # nflverse stores the away team's spread (negative means away favored).
        market_home = game["total_line"] / 2 + game["spread_line"] / 2
        market_away = game["total_line"] - market_home
        components.append({"label": "Listed total / spread midpoint", "away": market_away,
                           "home": market_home, "weight": .1})
    available_weight = sum(item["weight"] for item in components
                           if item["away"] is not None and item["home"] is not None)
    if available_weight:
        away_points = sum(item["away"] * item["weight"] for item in components
                          if item["away"] is not None and item["home"] is not None) / available_weight
        home_points = sum(item["home"] * item["weight"] for item in components
                          if item["away"] is not None and item["home"] is not None) / available_weight
    combined = away_points + home_points if away_points is not None and home_points is not None else None
    pace_values = [situational[team].get("plays_per_game") for team in (away, home)
                   if situational.get(team, {}).get("plays_per_game") is not None]
    drive_values = [situational[team].get("drives_per_game") for team in (away, home)
                    if situational.get(team, {}).get("drives_per_game") is not None]
    expected_plays = sum(pace_values) / len(pace_values) if pace_values else None
    expected_drives = sum(drive_values) / len(drive_values) if drive_values else None
    return {
        "away_points": away_points, "home_points": home_points,
        "combined_points": combined,
        "margin": (home_points - away_points
                   if away_points is not None and home_points is not None else None),
        "range_low": combined - 7 if combined is not None else None,
        "range_high": combined + 7 if combined is not None else None,
        "shape": ("Higher-volume scoring environment" if combined is not None and combined >= 48
                  else "Lower-volume, possession-sensitive environment" if combined is not None and combined <= 42
                  else "Balanced scoring environment" if combined is not None else "Awaiting sample"),
        "components": components,
        "expected_plays_per_team": expected_plays,
        "expected_possessions_per_team": expected_drives,
        "pace_label": ("Fast / high-play environment" if expected_plays is not None and expected_plays >= 66
                       else "Slow / possession-limited environment" if expected_plays is not None and expected_plays <= 61
                       else "Typical play volume" if expected_plays is not None else "Pace sample pending"),
        "situational": situational,
    }


def _matchup_watches(cards: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    watches = []
    for card in cards:
        for row in card["rows"]:
            if row["offense_rank"] is None or row["defense_rank"] is None:
                continue
            watches.append({
                "offense": card["offense"], "defense": card["defense"],
                "label": row["label"], "lean": row["lean"],
                "offense_rank": row["offense_rank"], "defense_rank": row["defense_rank"],
                "separation": abs(row["offense_rank"] - row["defense_rank"]),
            })
    watches.sort(key=lambda row: (-row["separation"], row["label"]))
    return watches[:4]


def _history(repository: NFLRepository, game: dict[str, Any]) -> dict[str, Any]:
    away = game["away_team"]
    home = game["home_team"]
    meetings = repository.prior_matchups(away, home, before_game_id=game["game_id"], limit=25)
    away_wins = home_wins = ties = 0
    for row in meetings:
        if row["away_score"] == row["home_score"]:
            ties += 1
        else:
            winner = row["away_team"] if row["away_score"] > row["home_score"] else row["home_team"]
            away_wins += winner == away
            home_wins += winner == home
        row["score_label"] = f"{row['away_team']} {row['away_score']}, {row['home_team']} {row['home_score']}"
        row["game_url"] = f"/nfl/games/{row['game_id']}/"
    def record(team: str) -> dict[str, Any]:
        wins = sum(1 for row in meetings if ((row["away_team"] == team and row["away_score"] > row["home_score"]) or
                                             (row["home_team"] == team and row["home_score"] > row["away_score"])))
        losses = sum(1 for row in meetings if row["away_score"] != row["home_score"]) - wins
        points_for = sum(row["away_score"] if row["away_team"] == team else row["home_score"] for row in meetings)
        points_against = sum(row["home_score"] if row["away_team"] == team else row["away_score"] for row in meetings)
        streak = []
        for row in meetings:
            own = row["away_score"] if row["away_team"] == team else row["home_score"]
            other = row["home_score"] if row["away_team"] == team else row["away_score"]
            result = "W" if own > other else "L" if own < other else "T"
            if streak and streak[0] != result: break
            streak.append(result)
        return {"record": f"{wins}-{losses}" + (f"-{ties}" if ties else ""),
                "ppg_for": points_for / len(meetings) if meetings else None,
                "ppg_against": points_against / len(meetings) if meetings else None,
                "streak": f"{streak[0]}{len(streak)}" if streak else None}
    return {
        "meetings": len(meetings), "away_wins": away_wins,
        "home_wins": home_wins, "ties": ties, "recent": meetings[:8],
        "away_record": record(away), "home_record": record(home),
        "coverage": repository.history_coverage(),
    }


def _situation(game: dict[str, Any], recent: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    items = []
    game_day = date.fromisoformat(game["game_date"])
    for team in (game["away_team"], game["home_team"]):
        previous = recent[team][0] if recent[team] else None
        side = "away" if team == game["away_team"] else "home"
        rest = game.get(f"{side}_rest")
        if rest is None:
            rest = (game_day - date.fromisoformat(previous["game_date"])).days if previous else None
        items.append({
            "label": f"{team} rest", "value": f"{rest} days" if rest is not None else "Season opener",
            "detail": (f"Previous: {previous['result']} {previous['score']} {previous['site']} {previous['opponent']}"
                       if previous else "No earlier completed game this season"),
            "tone": "warning" if rest is not None and rest <= 6 else "normal",
        })
        road_run = 0
        for prior in recent[team]:
            if prior.get("site") != "at": break
            road_run += 1
        if side == "away" and road_run >= 2:
            items.append({"label": f"{team} travel sequence", "value": f"{road_run + 1} road games",
                          "detail": "Consecutive road exposure entering this matchup.", "tone": "warning"})
    if game.get("division_game"):
        items.append({"label": "Division game", "value": "Yes",
                      "detail": "The schedule identifies this as an intra-division matchup."})
    roof = (game.get("roof") or "").lower()
    if roof:
        items.append({"label": "Venue", "value": game.get("roof", "").title(),
                      "detail": f"{game.get('stadium') or 'Venue TBD'} · {game.get('surface') or 'surface TBD'}"})
    if game.get("temperature") is not None or game.get("wind") is not None:
        conditions = []
        if game.get("temperature") is not None:
            conditions.append(f"{round(game['temperature'])}°F")
        if game.get("wind") is not None:
            conditions.append(f"{round(game['wind'])} mph wind")
        items.append({"label": "Recorded conditions", "value": " · ".join(conditions),
                      "detail": "Schedule-level weather observation; indoor venues may report no conditions."})
    return items


def _trap_spots(repository: NFLRepository, game: dict[str, Any],
                elo_ratings: dict[str, float]) -> list[dict[str, Any]]:
    """Look-ahead, letdown, sandwich and revenge spots for either team.

    Mirrors the CFB side's schedule_spot() -- same five signal types, same
    Elo-gap calibration -- adapted to NFL's flat team-abbreviation schedule
    rows instead of CFBD's team_id join.
    """
    season = game["season"]
    signals: list[dict[str, Any]] = []
    for team, opponent in ((game["away_team"], game["home_team"]),
                           (game["home_team"], game["away_team"])):
        schedule = repository.schedule(season, team=team)
        index = next((position for position, row in enumerate(schedule)
                      if row["game_id"] == game["game_id"]), None)
        if index is None:
            continue
        previous = schedule[index - 1] if index > 0 else None
        following = schedule[index + 1] if index + 1 < len(schedule) else None
        own_elo = elo_ratings.get(team)
        this_elo = elo_ratings.get(opponent)

        def opponent_of(row: dict[str, Any]) -> str:
            return row["home_team"] if row["away_team"] == team else row["away_team"]

        next_elo = next_name = previous_elo = previous_name = None
        if following:
            next_name = opponent_of(following)
            next_elo = elo_ratings.get(next_name)
        if previous:
            previous_name = opponent_of(previous)
            previous_elo = elo_ratings.get(previous_name)

        if None not in (own_elo, this_elo, next_elo):
            if own_elo - this_elo >= TRAP_OPPONENT_GAP and next_elo - this_elo >= TRAP_NEXT_GAP:
                signals.append({
                    "type": "LOOK_AHEAD", "headline": f"{team} plays {next_name} next",
                    "detail": (f"{opponent} rates {own_elo - this_elo:.0f} Elo below {team}, "
                              f"while {next_name} rates {next_elo - this_elo:.0f} above "
                              f"this week's opponent"),
                })
            if (previous_elo is not None and own_elo - this_elo >= TRAP_OPPONENT_GAP
                    and previous_elo - this_elo >= TRAP_NEXT_GAP
                    and next_elo - this_elo >= TRAP_NEXT_GAP):
                signals.append({
                    "type": "SANDWICH",
                    "headline": f"{team} is sandwiched between {previous_name} and {next_name}",
                    "detail": "a lesser opponent with a much harder game on either side",
                })

        if previous and previous["completed"]:
            own_points = (previous["home_score"] if previous["home_team"] == team
                         else previous["away_score"])
            other_points = (previous["away_score"] if previous["home_team"] == team
                           else previous["home_score"])
            won = (own_points or 0) > (other_points or 0)
            if won and previous_elo is not None and own_elo is not None:
                if previous_elo - own_elo >= -MAJOR_OPPONENT_GAP:
                    signals.append({
                        "type": "LETDOWN",
                        "headline": f"{team} is coming off a win over {previous_name}",
                        "detail": (f"beat a team rated {previous_elo:.0f} Elo last week, "
                                  f"then faces {opponent}"),
                    })

        with closing(repository._connect()) as connection:
            prior = connection.execute(
                """SELECT home_team,away_team,home_score,away_score,season
                   FROM games WHERE season=? AND completed=1
                   AND ((home_team=? AND away_team=?) OR (home_team=? AND away_team=?))
                   ORDER BY game_date DESC LIMIT 1""",
                (season - 1, team, opponent, opponent, team),
            ).fetchone()
        if prior:
            own_points = prior["home_score"] if prior["home_team"] == team else prior["away_score"]
            other_points = prior["away_score"] if prior["home_team"] == team else prior["home_score"]
            if own_points is not None and other_points is not None and other_points > own_points:
                signals.append({
                    "type": "REVENGE", "headline": f"{team} lost this matchup last season",
                    "detail": f"{opponent} won {other_points}-{own_points} in {prior['season']}",
                })
    return signals


def _travel(repository: NFLRepository, game: dict[str, Any]) -> dict[str, Any] | None:
    """Distance, time-zone change, altitude and kickoff timing for the visitor."""
    venues = repository.team_venues()
    home = venues.get(game["home_team"])
    away = venues.get(game["away_team"])
    if not home or not away:
        return None
    miles = round(haversine_miles(
        (away["latitude"], away["longitude"]), (home["latitude"], home["longitude"])))
    away_zone = timezone_for(away["longitude"])
    home_zone = timezone_for(home["longitude"])
    shift = None
    if zone_index(away_zone) is not None and zone_index(home_zone) is not None:
        shift = zone_index(home_zone) - zone_index(away_zone)

    notes = [f"{game['away_team']} travels {miles:,} miles"]
    if shift:
        direction = "east" if shift > 0 else "west"
        notes[0] += f" and {abs(shift)} time zone{'s' if abs(shift) != 1 else ''} {direction}"

    body_clock = None
    kickoff = kickoff_utc(game)
    if kickoff and shift and shift > 0:
        try:
            local_hour = datetime.fromisoformat(kickoff).astimezone(timezone.utc).hour - 4
        except ValueError:
            local_hour = None
        if local_hour is not None and local_hour <= EARLY_KICKOFF_HOUR:
            body_clock = (f"kickoff is {abs(shift)} hour{'s' if abs(shift) != 1 else ''} "
                          f"earlier on {game['away_team']}'s body clock")
            notes.append(body_clock)

    altitude = None
    home_elevation = home.get("elevation_meters")
    away_elevation = away.get("elevation_meters")
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
        "timezone_shift": shift, "notable": notable, "detail": "; ".join(notes),
        "body_clock": body_clock, "altitude": altitude,
        "venue": home.get("venue_name"), "dome": bool(home.get("dome")),
        "elevation_metres": home_elevation,
    }


def _weather(repository: NFLRepository, game: dict[str, Any]) -> dict[str, Any] | None:
    """The live forecast if one has been fetched, otherwise nflverse's own
    after-the-fact recorded temperature/wind for a completed game."""
    venue = repository.team_venues().get(game["home_team"])
    forecast = weather_for_game(repository, game["game_id"])
    if forecast.get("available"):
        latest = forecast["latest"]
        return {
            "available": True, "indoor": forecast["indoor"],
            "condition": "Indoors" if forecast["indoor"] else latest.get("condition"),
            "temperature": latest.get("temperature"), "wind": latest.get("sustained_wind"),
            "gusts": latest.get("wind_gust"),
            "precipitation_probability": latest.get("precipitation_probability"),
            "venue": latest.get("venue"), "flags": forecast["flags"],
            "snapshots": forecast["snapshots"], "movement": forecast["movement"],
            "live": True,
        }
    if game.get("temperature") is not None or game.get("wind") is not None:
        return {
            "available": True, "indoor": (game.get("roof") or "").lower() in {"dome", "closed"},
            "condition": None, "temperature": game.get("temperature"), "wind": game.get("wind"),
            "gusts": None, "precipitation_probability": None,
            "venue": venue.get("venue_name") if venue else game.get("stadium"),
            "flags": [], "snapshots": 0, "movement": {}, "live": False,
        }
    return None


def _market(repository: NFLRepository, game: dict[str, Any]) -> dict[str, Any]:
    line = game.get("spread_line")
    away_coach = repository.coach_against_numbers(
        game.get("away_coach"), before_game_id=game["game_id"], current_spread=line,
        current_total=game.get("total_line"))
    home_coach = repository.coach_against_numbers(
        game.get("home_coach"), before_game_id=game["game_id"],
        current_spread=-line if line is not None else None,
        current_total=game.get("total_line"))
    for side, coach in (("away", away_coach), ("home", home_coach)):
        if not coach:
            continue
        team_line = line if side == "away" else (-line if line is not None else None)
        if team_line is None or team_line == 0:
            coach["role_label"] = None; coach["role"] = None
        elif team_line < 0:
            coach["role_label"] = "As favorite"; coach["role"] = coach["favorite"]
        else:
            coach["role_label"] = "As underdog"; coach["role"] = coach["underdog"]
        coach["site_label"] = "On road" if side == "away" else "At home"
        coach["site"] = coach[side]
    return {
        "line": game.get("spread_line"), "total": game.get("total_line"),
        "away_moneyline": game.get("away_moneyline"), "home_moneyline": game.get("home_moneyline"),
        "away_spread_odds": game.get("away_spread_odds"),
        "home_spread_odds": game.get("home_spread_odds"),
        "over_odds": game.get("over_odds"), "under_odds": game.get("under_odds"),
        "coaches": {game["away_team"]: away_coach, game["home_team"]: home_coach},
    }


def _personnel(repository: NFLRepository, pff: NFLPFFService, season: int, team: str) -> dict[str, Any]:
    movement = significant_movements(repository, pff, season, team)
    # significant_movements is already ordered by evidence-weighted impact.
    # Preserve that ordering instead of allowing transaction type or position
    # to promote a lower-impact player above a core contributor.
    arrivals = movement["arrivals"]
    for row in arrivals:
        row["url"] = f"/nfl/players/{row['player_id']}/?season={season}"
    for row in movement["departures"]:
        row["url"] = f"/nfl/players/{row['player_id']}/?season={season - 1}"
    return {"arrival_count": movement["arrival_count"], "departure_count": movement["departure_count"],
            "arrivals": arrivals[:8], "departures": movement["departures"][:8],
            "prior_season": movement["prior_season"],
            "units": unit_continuity(repository, season, team)}


def matchup_context(repository: NFLRepository, game: dict[str, Any],
                    identities: dict[str, dict[str, Any]], pff: NFLPFFService) -> dict[str, Any]:
    """Return the season context shared by matchup HTML and JSON views."""
    season = game["season"]
    before_week = game["week"]
    away = game["away_team"]
    home = game["home_team"]
    baseline_season = season
    league = repository.league_efficiency(season, before_week=before_week)
    profiles = {row["team"]: row for row in league}
    if away not in profiles or home not in profiles:
        baseline_season = season - 1
        league = repository.league_efficiency(baseline_season)
        profiles = {row["team"]: row for row in league}
    ranks: dict[str, dict[str, int]] = {}
    for _label, offense_key, defense_key, _format in UNIT_METRICS:
        ranks[offense_key] = _ranks(league, offense_key)
        ranks[defense_key] = _ranks(league, defense_key, lower=True)

    production = {
        away: repository.team_season_summary(
            baseline_season, away, before_week=before_week if baseline_season == season else None),
        home: repository.team_season_summary(
            baseline_season, home, before_week=before_week if baseline_season == season else None),
    }
    production_rows = []
    for label, key, value_format, lower_is_better in PRODUCTION_METRICS:
        away_value = production[away].get(key); home_value = production[home].get(key)
        lean = None
        if away_value is not None and home_value is not None and away_value != home_value:
            away_better = away_value < home_value if lower_is_better else away_value > home_value
            lean = away if away_better else home
        production_rows.append({
            "label": label, "format": value_format, "away": away_value,
            "home": home_value, "lean": lean, "lower_is_better": lower_is_better,
        })

    recent = {
        away: _recent(repository, game, away, identities),
        home: _recent(repository, game, home, identities),
    }
    situational = {
        team: repository.team_situational_profile(
            baseline_season, team, before_week=before_week if baseline_season == season else None,
        ) for team in (away, home)
    }
    unit_cards = (
        _unit_card(profiles, ranks, away, home),
        _unit_card(profiles, ranks, home, away),
    )
    elo_ratings = {row["team"]: row["rating"] for row in repository.elo_ratings()}
    return {
        "baseline_season": baseline_season,
        "records": {away: repository.team_record(season, away, before_week=before_week),
                    home: repository.team_record(season, home, before_week=before_week)},
        "elo": {away: round(elo_ratings.get(away, 1500)), home: round(elo_ratings.get(home, 1500))},
        "profiles": {away: profiles.get(away), home: profiles.get(home)},
        "unit_cards": unit_cards,
        "matchup_watches": _matchup_watches(unit_cards),
        "production": production_rows,
        "game_shape": _game_shape(production, recent, situational, game, away, home),
        "recent": recent,
        "leaders": {
            away: _leaders(repository, season, away, before_week, stats_season=baseline_season),
            home: _leaders(repository, season, home, before_week, stats_season=baseline_season),
        },
        "usage": {
            away: team_usage_context(
                repository, season, away, before_week=before_week,
                preferred_season=baseline_season,
            ),
            home: team_usage_context(
                repository, season, home, before_week=before_week,
                preferred_season=baseline_season,
            ),
        },
        "history": _history(repository, game),
        "player_history": {
            away: repository.players_vs_opponent(season, away, home, before_game_id=game["game_id"]),
            home: repository.players_vs_opponent(season, home, away, before_game_id=game["game_id"]),
        },
        "market": _market(repository, game),
        "situation": _situation(game, recent),
        "trap_spots": _trap_spots(repository, game, elo_ratings),
        "travel": _travel(repository, game),
        "weather": _weather(repository, game),
        "personnel": {away: _personnel(repository, pff, season, away),
                      home: _personnel(repository, pff, season, home)},
    }
