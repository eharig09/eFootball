"""Build descriptive, leak-resistant context for an NFL matchup page."""

from __future__ import annotations

from datetime import date
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.pff import NFLPFFService
from sports_aggregator.nfl.personnel import significant_movements
from sports_aggregator.nfl.usage import team_usage_context
from sports_aggregator.nfl.teams import unit_continuity


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
    return {
        "baseline_season": baseline_season,
        "records": {away: repository.team_record(season, away, before_week=before_week),
                    home: repository.team_record(season, home, before_week=before_week)},
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
        "personnel": {away: _personnel(repository, pff, season, away),
                      home: _personnel(repository, pff, season, home)},
    }
