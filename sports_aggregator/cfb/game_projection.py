"""Live game projections: xDrives x xPlaysPerDrive x xPassRate x xYards, for one matchup.

`xdrives.py`/`xplays.py`/`xvolume.py`/`xyards.py` each backtest their own
Baseline C (a team's own trailing tendency blended with what its opponent
has allowed) against *historical* games that already have a completed
`cfb_team_game_pace` row, batch-building a whole season's dataset at once.
A matchup page needs the same formula for one specific pair of teams as of
right now (or as of a specific game's kickoff) -- including a genuinely
upcoming game that has no played-game row at all yet, so it can never appear
in those batch-built dataset tables.

This module is that live path: `team_snapshot` reads a team's own trailing
history directly from `cfb_team_game_pace` (the same leak-safe,
recency-weighted walk, just evaluated once for one team instead of batched
across a season), and `project_matchup` combines two snapshots with the same
Baseline C formula each layer's own backtesting found competitive with more
complex alternatives (see docs/CFB_XDRIVES.md section 7, docs/CFB_XVOLUME.md,
docs/CFB_XYARDS.md) -- deliberately not a different, untested combination
rule.

`as_of_date` matters even for a page that only ever asks about the future:
passing a completed game's own kickoff time keeps a backtest-style leak-safe
guarantee (the game's own result can never appear in its own projection),
and for a truly upcoming game it is simply "right now."
"""
from __future__ import annotations

from contextlib import closing
from typing import Any, Iterable, Sequence

from sports_aggregator.cfb.xdrives import (
    RECENCY_LAMBDA,
    TRAILING_WINDOW_GAMES,
    league_prior_drives_live,
    load_model as load_drives_model,
    predict_drives,
)

_OWN_FIELDS = ("meaningful_drives", "plays_per_meaningful_drive", "pass_rate",
              "yards_per_dropback", "yards_per_rush", "seconds_per_play", "success_rate")
_SNAPSHOT_KEYS = ("drives", "plays_per_drive", "pass_rate", "yards_per_dropback", "yards_per_rush",
                  "seconds_per_play", "success_rate")


def _decay_weights(count: int, lam: float) -> list[float]:
    import math
    return [math.exp(-lam * (count - 1 - i)) for i in range(count)]


def _weighted_mean(weights: Sequence[float], values: Iterable[Any]) -> float | None:
    pairs = [(w, float(v)) for w, v in zip(weights, values) if v is not None]
    total_weight = sum(w for w, _ in pairs)
    if not pairs or total_weight <= 0:
        return None
    return sum(w * v for w, v in pairs) / total_weight


def _weighted_ratio(weights: Sequence[float], numerators: Iterable[Any],
                    denominators: Iterable[Any]) -> float | None:
    numerator = denominator = 0.0
    for weight, top, bottom in zip(weights, numerators, denominators):
        if top is None or bottom is None:
            continue
        numerator += weight * float(top)
        denominator += weight * float(bottom)
    return numerator / denominator if denominator else None


def team_snapshot(repository, team: str, *, before_date: str, window: int = TRAILING_WINDOW_GAMES,
                  half_life_games: float | None = None) -> dict[str, Any]:
    """One team's current leak-safe trailing state, strictly before `before_date`.

    Always returns a dict, never `None` -- a team with zero stored games
    before this date gets `games=0` and every field `None`, the same
    cold-start signal `xdrives.py` etc. use, so callers don't need a
    separate "team not found" branch.
    """
    import math
    from sports_aggregator.cfb.team_game_pace import METRIC_VERSION as PACE_VERSION
    from sports_aggregator.cfb.team_game_pace import initialize as initialize_pace

    lam = RECENCY_LAMBDA if half_life_games is None else (
        0.0 if half_life_games == math.inf else math.log(2.0) / half_life_games)

    initialize_pace(repository)
    empty = {"team": team, "games": 0,
             **{key: None for key in _SNAPSHOT_KEYS}, **{f"{key}_allowed": None for key in _SNAPSHOT_KEYS}}

    with closing(repository._connect()) as connection:
        rows = [dict(row) for row in connection.execute(f"""
          SELECT a.game_id, a.opponent, a.meaningful_drives, a.plays_per_meaningful_drive,
                 a.pass_rate, a.yards_per_dropback, a.yards_per_rush,
                 a.seconds_per_play, a.success_rate
          FROM cfb_team_game_pace a JOIN games g ON g.game_id=a.game_id
          WHERE a.metric_version=? AND a.team=? AND g.start_date<?
          ORDER BY g.start_date DESC LIMIT ?
        """, (PACE_VERSION, team, before_date, window)).fetchall()]
        if not rows:
            return empty
        rows.reverse()  # oldest first, so index -1 is "most recent" for decay weighting

        game_ids = [row["game_id"] for row in rows]
        placeholders = ",".join("?" for _ in game_ids)
        opponent_rows = {
            (row["game_id"], row["team"]): dict(row)
            for row in connection.execute(
                f"""SELECT game_id, team, meaningful_drives, plays_per_meaningful_drive, pass_rate,
                           yards_per_dropback, yards_per_rush, seconds_per_play, success_rate
                    FROM cfb_team_game_pace WHERE metric_version=? AND game_id IN ({placeholders})""",
                (PACE_VERSION, *game_ids))
        }

    weights = _decay_weights(len(rows), lam)
    snapshot: dict[str, Any] = {"team": team, "games": len(rows)}
    for field, key in zip(_OWN_FIELDS, _SNAPSHOT_KEYS):
        snapshot[key] = _weighted_mean(weights, (row[field] for row in rows))
        snapshot[f"{key}_allowed"] = _weighted_mean(
            weights,
            (opponent_rows.get((row["game_id"], row["opponent"]), {}).get(field) for row in rows),
        )
    return snapshot


def team_scoring_snapshot(repository, team: str, *, before_date: str,
                          window: int = TRAILING_WINDOW_GAMES,
                          half_life_games: float | None = None) -> dict[str, Any]:
    """Pregame turnover/red-zone rates from Milestone 9 actuals."""
    import math
    from sports_aggregator.cfb.team_game_scoring import METRIC_VERSION as SCORING_VERSION
    from sports_aggregator.cfb.team_game_scoring import initialize as initialize_scoring

    initialize_scoring(repository)
    lam = RECENCY_LAMBDA if half_life_games is None else (
        0.0 if half_life_games == math.inf else math.log(2.0) / half_life_games)
    keys = ("giveaway_rate", "trips_per_drive", "red_zone_td_rate")
    ratios = {
        "giveaway_rate": ("giveaways", "competitive_plays"),
        "trips_per_drive": ("red_zone_trips", "meaningful_drives"),
        "red_zone_td_rate": ("red_zone_touchdowns", "red_zone_trips"),
    }
    empty = {"team": team, "games": 0,
             **{key: None for key in keys}, **{f"{key}_allowed": None for key in keys}}
    with closing(repository._connect()) as connection:
        rows = [dict(row) for row in connection.execute("""
          SELECT s.*,CAST(s.red_zone_trips AS REAL)/NULLIF(s.meaningful_drives,0) AS trips_per_drive
          FROM cfb_team_game_scoring s JOIN games g USING(game_id)
          WHERE s.metric_version=? AND s.team=? AND g.start_date<?
          ORDER BY g.start_date DESC LIMIT ?
        """, (SCORING_VERSION, team, before_date, window)).fetchall()]
        if not rows:
            return empty
        rows.reverse()
        game_ids = [row["game_id"] for row in rows]
        placeholders = ",".join("?" for _ in game_ids)
        opponents = {(row["game_id"], row["team"]): dict(row) for row in connection.execute(f"""
          SELECT *,CAST(red_zone_trips AS REAL)/NULLIF(meaningful_drives,0) AS trips_per_drive
          FROM cfb_team_game_scoring
          WHERE metric_version=? AND game_id IN ({placeholders})
        """, (SCORING_VERSION, *game_ids))}
    weights = _decay_weights(len(rows), lam)
    result = {"team": team, "games": len(rows)}
    for key in keys:
        numerator, denominator = ratios[key]
        result[key] = _weighted_ratio(
            weights, (row.get(numerator) for row in rows),
            (row.get(denominator) for row in rows))
        opponent_history = [
            opponents.get((row["game_id"], row["opponent"]), {}) for row in rows]
        result[f"{key}_allowed"] = _weighted_ratio(
            weights, (row.get(numerator) for row in opponent_history),
            (row.get(denominator) for row in opponent_history))
    return result


def scoring_environment(repository, *, before_date: str) -> dict[str, float | None]:
    """League priors available before kickoff, used only for turnover shrinkage."""
    from sports_aggregator.cfb.team_game_scoring import METRIC_VERSION as SCORING_VERSION
    with repository._reader() as connection:
        row = connection.execute("""
          SELECT SUM(s.giveaways) AS giveaways,SUM(s.competitive_plays) AS plays,
                 SUM(s.red_zone_trips) AS trips,SUM(s.red_zone_touchdowns) AS touchdowns,
                 SUM(s.meaningful_drives) AS drives
          FROM cfb_team_game_scoring s JOIN games g USING(game_id)
          WHERE s.metric_version=? AND g.start_date<?
        """, (SCORING_VERSION, before_date)).fetchone()
    return {
        "giveaway_rate": (row["giveaways"] / row["plays"] if row and row["plays"] else None),
        "trips_per_drive": (row["trips"] / row["drives"] if row and row["drives"] else None),
        "red_zone_td_rate": (row["touchdowns"] / row["trips"] if row and row["trips"] else None),
    }


def team_points_snapshot(repository, team: str, *, before_date: str,
                         window: int = TRAILING_WINDOW_GAMES,
                         half_life_games: float | None = None) -> dict[str, Any]:
    """Drive outcomes, opponent-quality residuals and recent results before kickoff."""
    import math
    from sports_aggregator.cfb.xpoints import DATASET_VERSION as XPOINTS_VERSION
    from sports_aggregator.cfb.xpoints import initialize as initialize_xpoints
    initialize_xpoints(repository)
    lam = RECENCY_LAMBDA if half_life_games is None else (
        0.0 if half_life_games == math.inf else math.log(2.0) / half_life_games)
    empty = {"team": team, "games": 0, "points_per_drive": None,
             "points_per_drive_allowed": None, "touchdown_rate": None,
             "touchdown_rate_allowed": None, "field_goal_rate": None,
             "field_goal_rate_allowed": None, "offense_adjusted_residual": None,
             "defense_adjusted_residual": None, "recent_margin": None}
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute("""
          SELECT x.*,g.start_date,g.home_team,g.away_team,g.home_points,g.away_points
          FROM cfb_xpoints_dataset x JOIN games g USING(game_id)
          WHERE x.dataset_version=? AND x.team=? AND g.start_date<?
          ORDER BY g.start_date DESC LIMIT ?
        """, (XPOINTS_VERSION, team, before_date, window))]
        defense_rows = [dict(row) for row in connection.execute("""
          SELECT x.* FROM cfb_xpoints_dataset x JOIN games g USING(game_id)
          WHERE x.dataset_version=? AND x.opponent=? AND g.start_date<?
          ORDER BY g.start_date DESC LIMIT ?
        """, (XPOINTS_VERSION, team, before_date, window))]
    if not rows:
        return empty
    rows.reverse(); defense_rows.reverse()
    weights = _decay_weights(len(rows), lam)
    defense_weights = _decay_weights(len(defense_rows), lam)
    result = {"team": team, "games": len(rows)}
    result["points_per_drive"] = _weighted_mean(weights, (row["actual_points_per_drive"] for row in rows))
    result["touchdown_rate"] = _weighted_mean(weights, (row["actual_touchdown_rate"] for row in rows))
    result["field_goal_rate"] = _weighted_mean(weights, (row["actual_field_goal_rate"] for row in rows))
    result["points_per_drive_allowed"] = _weighted_mean(
        defense_weights, (row["actual_points_per_drive"] for row in defense_rows))
    result["touchdown_rate_allowed"] = _weighted_mean(
        defense_weights, (row["actual_touchdown_rate"] for row in defense_rows))
    result["field_goal_rate_allowed"] = _weighted_mean(
        defense_weights, (row["actual_field_goal_rate"] for row in defense_rows))
    result["offense_adjusted_residual"] = _weighted_mean(
        weights, ((row["actual_points_per_drive"] - row["opponent_prior_points_per_drive_allowed"])
                  if row["opponent_prior_points_per_drive_allowed"] is not None else None
                  for row in rows))
    result["defense_adjusted_residual"] = _weighted_mean(
        defense_weights, ((row["actual_points_per_drive"] - row["team_prior_points_per_drive"])
                          if row["team_prior_points_per_drive"] is not None else None
                          for row in defense_rows))
    margins = []
    for row in rows:
        if row["home_points"] is None or row["away_points"] is None:
            margins.append(None)
        elif team == row["home_team"]:
            margins.append(float(row["home_points"]) - float(row["away_points"]))
        else:
            margins.append(float(row["away_points"]) - float(row["home_points"]))
    result["recent_margin"] = _weighted_mean(weights, margins)
    return result


def points_environment(repository, *, before_date: str) -> dict[str, float | None]:
    """League drive-outcome rates available before kickoff."""
    from sports_aggregator.cfb.team_game_drive_outcomes import METRIC_VERSION as OUTCOME_VERSION
    with repository._reader() as connection:
        row = connection.execute("""
          SELECT SUM(o.offensive_points) points,SUM(o.touchdowns) touchdowns,
                 SUM(o.field_goals) field_goals,SUM(o.meaningful_drives) drives
          FROM cfb_team_game_drive_outcomes o JOIN games g USING(game_id)
          WHERE o.metric_version=? AND g.start_date<?
        """, (OUTCOME_VERSION, before_date)).fetchone()
    drives = float(row["drives"]) if row and row["drives"] else 0.0
    return {"points_per_drive": row["points"] / drives if drives else None,
            "touchdown_rate": row["touchdowns"] / drives if drives else None,
            "field_goal_rate": row["field_goals"] / drives if drives else None}


def matchup_quality_snapshot(repository, game_id: int) -> dict[str, Any]:
    """Pregame Elo/FPI/CORE/Vegas edges on a common team-margin scale."""
    from sports_aggregator.cfb.lines import initialize as initialize_lines
    initialize_lines(repository)
    with repository._reader() as connection:
        game = connection.execute("SELECT * FROM games WHERE game_id=?", (int(game_id),)).fetchone()
        if not game:
            return {}
        game = dict(game)
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        line = connection.execute("""SELECT AVG(spread) spread FROM game_lines
          WHERE game_id=? AND spread IS NOT NULL""", (int(game_id),)).fetchone()
        fpi = {}
        if "fpi_game_projections" in tables:
            fpi = {int(row["team_id"]): row["pred_point_diff"] for row in connection.execute(
                "SELECT team_id,pred_point_diff FROM fpi_game_projections WHERE game_id=?",
                (int(game_id),))}
        core = {}
        for team in (game["home_team"], game["away_team"]):
            row = connection.execute("""SELECT overall FROM core_ratings
              WHERE season=? AND team=? AND through_week<?
              ORDER BY through_week DESC LIMIT 1""",
                (game["season"], team, game["week"])).fetchone()
            core[team] = float(row["overall"]) if row else None

    spread = float(line["spread"]) if line and line["spread"] is not None else None
    home_elo, away_elo = game["home_pregame_elo"], game["away_pregame_elo"]
    home_core, away_core = core.get(game["home_team"]), core.get(game["away_team"])
    home_components = {
        "elo": ((float(home_elo) - float(away_elo)) / 25.0
                if home_elo is not None and away_elo is not None else None),
        "fpi": fpi.get(int(game["home_team_id"])),
        "core": (home_core - away_core
                 if home_core is not None and away_core is not None else None),
        "vegas": -spread if spread is not None else None,
    }
    def side(components: dict[str, float | None]) -> dict[str, Any]:
        values = [float(value) for value in components.values() if value is not None]
        return {"components": components,
                "blend": sum(values) / len(values) if values else None,
                "source_count": len(values)}
    away_components = {key: (-value if value is not None else None)
                       for key, value in home_components.items()}
    return {"home": side(home_components), "away": side(away_components),
            "scale": "estimated point-margin edge",
            "method": "equal-weight mean of available sources"}


def team_special_teams_snapshot(repository, team: str, *, before_date: str,
                                window: int = TRAILING_WINDOW_GAMES,
                                half_life_games: float | None = None) -> dict[str, Any]:
    """Pregame field-position and kicking rates from Milestone 10 actuals."""
    import math
    from sports_aggregator.cfb.team_game_special_teams import METRIC_VERSION as SPECIAL_VERSION
    from sports_aggregator.cfb.team_game_special_teams import initialize as initialize_special
    initialize_special(repository)
    lam = RECENCY_LAMBDA if half_life_games is None else (
        0.0 if half_life_games == math.inf else math.log(2.0) / half_life_games)
    ratios = {
        "start_yards_to_goal": ("start_yards_to_goal_total", "offensive_starts"),
        "start_after_punt": ("punt_start_yards_to_goal_total", "punt_returns"),
        "opponent_start_after_punt": ("opponent_punt_start_yards_to_goal_total", "punts"),
        "net_punt_yards": ("net_punt_yards_total", "punts"),
        "field_goal_accuracy": ("field_goals_made", "field_goal_attempts"),
        "field_goal_distance": ("field_goal_distance_total", "field_goal_attempts"),
    }
    empty = {"team": team, "games": 0, **{key: None for key in ratios},
             "start_yards_to_goal_allowed": None}
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute("""
          SELECT s.* FROM cfb_team_game_special_teams s JOIN games g USING(game_id)
          WHERE s.metric_version=? AND s.team=? AND g.start_date<?
          ORDER BY g.start_date DESC LIMIT ?
        """, (SPECIAL_VERSION, team, before_date, window))]
        if not rows:
            return empty
        rows.reverse()
        game_ids = [row["game_id"] for row in rows]
        placeholders = ",".join("?" for _ in game_ids)
        opponents = {(row["game_id"], row["team"]): dict(row) for row in connection.execute(f"""
          SELECT * FROM cfb_team_game_special_teams
          WHERE metric_version=? AND game_id IN ({placeholders})
        """, (SPECIAL_VERSION, *game_ids))}
    weights = _decay_weights(len(rows), lam)
    result = {"team": team, "games": len(rows)}
    for key, (numerator, denominator) in ratios.items():
        result[key] = _weighted_ratio(
            weights, (row.get(numerator) for row in rows),
            (row.get(denominator) for row in rows))
    opponent_history = [opponents.get((row["game_id"], row["opponent"]), {}) for row in rows]
    result["start_yards_to_goal_allowed"] = _weighted_ratio(
        weights, (row.get("start_yards_to_goal_total") for row in opponent_history),
        (row.get("offensive_starts") for row in opponent_history))
    return result


def field_position_environment(repository, *, before_date: str) -> dict[str, float | None]:
    """Pregame league field-position priors; the winning Milestone 10 baselines."""
    from sports_aggregator.cfb.team_game_special_teams import METRIC_VERSION as SPECIAL_VERSION
    with repository._reader() as connection:
        row = connection.execute("""
          SELECT SUM(s.start_yards_to_goal_total) AS start_total,
                 SUM(s.offensive_starts) AS starts,
                 SUM(s.punt_start_yards_to_goal_total) AS punt_start_total,
                 SUM(s.punt_returns) AS punt_returns,
                 SUM(s.field_goals_made) AS fgm,SUM(s.field_goal_attempts) AS fga,
                 SUM(s.net_punt_yards_total) AS net_punt_total,SUM(s.punts) AS punts
          FROM cfb_team_game_special_teams s JOIN games g USING(game_id)
          WHERE s.metric_version=? AND g.start_date<?
        """, (SPECIAL_VERSION, before_date)).fetchone()
    return {
        "start_yards_to_goal": row["start_total"] / row["starts"] if row and row["starts"] else None,
        "start_after_punt": (row["punt_start_total"] / row["punt_returns"]
                             if row and row["punt_returns"] else None),
        "field_goal_accuracy": row["fgm"] / row["fga"] if row and row["fga"] else None,
        "net_punt_yards": row["net_punt_total"] / row["punts"] if row and row["punts"] else None,
    }


def _blend(offense_value: float | None, defense_allowed_value: float | None) -> float | None:
    if offense_value is None or defense_allowed_value is None:
        return None
    return (offense_value + defense_allowed_value) / 2


#: Shrinkage constants for the two red-zone rates, each picked by sweeping
#: pseudo-games against xredzone.py's own dataset (cfb_xredzone_dataset,
#: 12,150 team-games, cold-start dropped) and comparing MAE/RMSE to the
#: baselines xredzone.evaluate_baselines() already reports. xturnovers.py's
#: own D_shrunk_blend baseline was checked the same way for giveaways and
#: did not clear the league prior already live below (see comment there);
#: red-zone trips and TD rate did:
#:   trips_per_drive:  matchup blend (no shrink) mae=0.12301 -> shrunk (pseudo=4) mae=0.12262
#:   red_zone_td_rate:      league prior (pseudo=inf) mae=0.24719 -> shrunk (pseudo=10) mae=0.24544
def _shrunk_blend(team_value: float | None, allowed_value: float | None,
                  league_value: float | None, sample_games: int | None,
                  pseudo_games: float) -> float | None:
    if league_value is None:
        return _blend(team_value, allowed_value)
    blend = _blend(team_value, allowed_value)
    if blend is None or not sample_games:
        return league_value
    weight = sample_games / (sample_games + pseudo_games)
    return weight * blend + (1 - weight) * league_value


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def _project_side(offense: dict[str, Any], defense: dict[str, Any],
                  scoring_offense: dict[str, Any], scoring_defense: dict[str, Any],
                  league: dict[str, float | None],
                  special_offense: dict[str, Any] | None = None,
                  special_defense: dict[str, Any] | None = None,
                  field_environment: dict[str, float | None] | None = None,
                  points_offense: dict[str, Any] | None = None,
                  points_defense: dict[str, Any] | None = None,
                  points_league: dict[str, float | None] | None = None,
                  quality: dict[str, Any] | None = None,
                  points_model: dict[str, Any] | None = None,
                  drives_model: dict[str, Any] | None = None,
                  drives_league_prior: float | None = None,
                  team_elo: float | None = None,
                  opponent_elo: float | None = None,
                  home_away: str | None = None) -> dict[str, Any]:
    matchup_drives = _blend(offense["drives"], defense["drives_allowed"])
    model_drives = None
    if drives_model:
        drives_features = {
            "team_prior_drives": offense["drives"],
            "opponent_prior_drives_allowed": defense["drives_allowed"],
            "league_prior_drives": drives_league_prior,
            "team_prior_seconds_per_play": offense["seconds_per_play"],
            "opponent_prior_seconds_per_play": defense["seconds_per_play"],
            "team_prior_pass_rate": offense["pass_rate"],
            "team_prior_success_rate": offense["success_rate"],
            "opponent_prior_success_rate": defense["success_rate"],
            "team_elo": team_elo,
            "opponent_elo": opponent_elo,
            "home_away": home_away,
        }
        model_drives = predict_drives(
            drives_model["coefficients"], drives_model["features"], drives_features)
    # The 2024-2026 walk-forward holdout (xdrives.evaluate_advanced_model)
    # found the persisted advanced regression beats the plain matchup blend
    # in every genuine out-of-sample season. Fall back to the blend whenever
    # the model hasn't been fit yet or a required input (e.g. a team with no
    # trailing history, or a game with no pregame Elo on record) is missing --
    # the same "simpler estimate when the richer one can't be computed"
    # pattern points_per_drive already uses just below.
    drives = model_drives if model_drives is not None else matchup_drives
    plays_per_drive = _blend(offense["plays_per_drive"], defense["plays_per_drive_allowed"])
    pass_rate = _blend(offense["pass_rate"], defense["pass_rate_allowed"])
    yards_per_dropback = _blend(offense["yards_per_dropback"], defense["yards_per_dropback_allowed"])
    yards_per_rush = _blend(offense["yards_per_rush"], defense["yards_per_rush_allowed"])

    plays = drives * plays_per_drive if None not in (drives, plays_per_drive) else None
    dropbacks = plays * pass_rate if None not in (plays, pass_rate) else None
    rush_attempts = plays * (1 - pass_rate) if None not in (plays, pass_rate) else None
    pass_yards = dropbacks * yards_per_dropback if None not in (dropbacks, yards_per_dropback) else None
    rush_yards = rush_attempts * yards_per_rush if None not in (rush_attempts, yards_per_rush) else None
    total_yards = (pass_yards + rush_yards) if None not in (pass_yards, rush_yards) else None
    matchup_turnover_rate = _blend(scoring_offense["giveaway_rate"],
                                   scoring_defense["giveaway_rate_allowed"])
    # The 2022-25 backtest found the league prior beats both the raw and
    # shrunk matchup blends. Preserve the matchup estimate for diagnosis, but
    # serve the simpler winner until a held-out model earns extra complexity.
    turnover_rate = league.get("giveaway_rate") or matchup_turnover_rate
    giveaways = plays * turnover_rate if None not in (plays, turnover_rate) else None
    matchup_trips_rate = _blend(scoring_offense["trips_per_drive"],
                                scoring_defense["trips_per_drive_allowed"])
    trips_sample = min(scoring_offense.get("games") or 0, scoring_defense.get("games") or 0)
    trips_rate = _shrunk_blend(
        scoring_offense["trips_per_drive"], scoring_defense["trips_per_drive_allowed"],
        league.get("trips_per_drive"), trips_sample, pseudo_games=4.0,
    )
    red_zone_trips = drives * trips_rate if None not in (drives, trips_rate) else None
    matchup_red_zone_td_rate = _blend(scoring_offense["red_zone_td_rate"],
                                      scoring_defense["red_zone_td_rate_allowed"])
    td_sample = min(scoring_offense.get("games") or 0, scoring_defense.get("games") or 0)
    red_zone_td_rate = _shrunk_blend(
        scoring_offense["red_zone_td_rate"], scoring_defense["red_zone_td_rate_allowed"],
        league.get("red_zone_td_rate"), td_sample, pseudo_games=10.0,
    )
    red_zone_touchdowns = (red_zone_trips * red_zone_td_rate
                           if None not in (red_zone_trips, red_zone_td_rate) else None)
    special_offense = special_offense or {}
    special_defense = special_defense or {}
    field_environment = field_environment or {}
    matchup_start = _blend(special_offense.get("start_yards_to_goal"),
                           special_defense.get("start_yards_to_goal_allowed"))
    matchup_punt_start = _blend(special_offense.get("start_after_punt"),
                                special_defense.get("opponent_start_after_punt"))
    start_yards_to_goal = field_environment.get("start_yards_to_goal") or matchup_start
    start_after_punt = field_environment.get("start_after_punt") or matchup_punt_start
    field_goal_accuracy = (field_environment.get("field_goal_accuracy")
                           or special_offense.get("field_goal_accuracy"))
    net_punt_yards = (field_environment.get("net_punt_yards")
                      or special_offense.get("net_punt_yards"))
    points_offense = points_offense or {}; points_defense = points_defense or {}
    points_league = points_league or {}
    matchup_ppd = _blend(points_offense.get("points_per_drive"),
                         points_defense.get("points_per_drive_allowed"))
    league_ppd = points_league.get("points_per_drive")
    offense_residual = points_offense.get("offense_adjusted_residual")
    defense_residual = points_defense.get("defense_adjusted_residual")
    residual_ppd = (max(0.0, league_ppd + (offense_residual or 0.0) + (defense_residual or 0.0))
                    if league_ppd is not None else None)
    quality_components = (quality or {}).get("components") or {}
    model_features = {
        "team_prior_points_per_drive": points_offense.get("points_per_drive"),
        "opponent_prior_points_per_drive_allowed": points_defense.get("points_per_drive_allowed"),
        "team_opponent_adjusted_residual": offense_residual,
        "opponent_defense_adjusted_residual": defense_residual,
        "team_recent_margin": points_offense.get("recent_margin"),
        "opponent_recent_margin": points_defense.get("recent_margin"),
        "elo_difference": (quality_components.get("elo") * 25.0
                           if quality_components.get("elo") is not None else None),
        "opponent_quality_blend": (quality or {}).get("blend"),
    }
    model_ppd = None
    if points_model:
        from sports_aggregator.cfb.xpoints import predict as predict_points
        model_ppd = predict_points(points_model, model_features)
    # The held-out test qualified the combined model, while the residual-only
    # formula lost to team-only. Fall back to the simple matchup blend whenever
    # a required external rating is missing.
    projected_ppd = model_ppd if model_ppd is not None else (matchup_ppd or league_ppd)
    expected_points = drives * projected_ppd if None not in (drives, projected_ppd) else None
    touchdown_rate = _blend(points_offense.get("touchdown_rate"),
                            points_defense.get("touchdown_rate_allowed"))
    field_goal_rate = _blend(points_offense.get("field_goal_rate"),
                             points_defense.get("field_goal_rate_allowed"))

    return {
        "drives": _round(drives),
        "matchup_drives": _round(matchup_drives),
        "drives_model_used": model_drives is not None,
        "plays_per_drive": _round(plays_per_drive, 2),
        "plays": _round(plays),
        "pass_rate": _round(pass_rate, 3),
        "dropbacks": _round(dropbacks), "rush_attempts": _round(rush_attempts),
        "yards_per_dropback": _round(yards_per_dropback, 2),
        "yards_per_rush": _round(yards_per_rush, 2),
        "pass_yards": _round(pass_yards), "rush_yards": _round(rush_yards),
        "total_yards": _round(total_yards),
        "turnover_rate": _round(turnover_rate, 4),
        "matchup_turnover_rate": _round(matchup_turnover_rate, 4),
        "giveaways": _round(giveaways, 2),
        "trips_per_drive": _round(trips_rate, 4),
        "matchup_trips_per_drive": _round(matchup_trips_rate, 4),
        "red_zone_trips": _round(red_zone_trips, 2),
        "red_zone_td_rate": _round(red_zone_td_rate, 3),
        "matchup_red_zone_td_rate": _round(matchup_red_zone_td_rate, 3),
        # trips_rate x red_zone_td_rate: expected red-zone TDs per drive.
        # margin_feature_ablation.py found the home-minus-away difference of
        # this exact quantity a real, walk-forward-validated margin-v2
        # feature (see live_margin_calibration.py's "plus_redzone" tier).
        "red_zone_scoring_rate": (
            _round(trips_rate * red_zone_td_rate, 4)
            if None not in (trips_rate, red_zone_td_rate) else None
        ),
        "red_zone_touchdowns": _round(red_zone_touchdowns, 2),
        "start_yards_to_goal": _round(start_yards_to_goal, 1),
        "matchup_start_yards_to_goal": _round(matchup_start, 1),
        "start_after_punt": _round(start_after_punt, 1),
        "matchup_start_after_punt": _round(matchup_punt_start, 1),
        "field_goal_accuracy": _round(field_goal_accuracy, 3),
        "net_punt_yards": _round(net_punt_yards, 1),
        "points_per_drive": _round(projected_ppd, 3),
        "matchup_points_per_drive": _round(matchup_ppd, 3),
        "residual_points_per_drive": _round(residual_ppd, 3),
        "points_model_used": model_ppd is not None,
        "expected_points": _round(expected_points, 1),
        "expected_touchdowns": _round(drives * touchdown_rate, 2)
            if None not in (drives, touchdown_rate) else None,
        "expected_field_goals": _round(drives * field_goal_rate, 2)
            if None not in (drives, field_goal_rate) else None,
        "recent_margin": _round(points_offense.get("recent_margin"), 1),
        "offense_schedule_adjustment": _round(offense_residual, 3),
        "opponent_defense_adjustment": _round(defense_residual, 3),
        "opponent_quality_edge": _round((quality or {}).get("blend"), 1),
        "opponent_quality_sources": (quality or {}).get("source_count", 0),
    }


_LOAD_POINTS_MODEL = object()
_LOAD_DRIVES_MODEL = object()


def _pregame_elo(repository, game_id: int | None) -> tuple[float | None, float | None]:
    """Raw (unscaled) home/away pregame Elo for the xdrives model's `elo_diff`
    feature -- deliberately not the /25 margin-scaled version
    matchup_quality_snapshot() returns for margin-v2, which was fit against
    a different scale."""
    if game_id is None:
        return None, None
    with repository._reader() as connection:
        row = connection.execute(
            "SELECT home_pregame_elo, away_pregame_elo FROM games WHERE game_id=?",
            (int(game_id),)).fetchone()
    if row is None:
        return None, None
    home_elo = row["home_pregame_elo"]
    away_elo = row["away_pregame_elo"]
    return (float(home_elo) if home_elo is not None else None,
            float(away_elo) if away_elo is not None else None)


def project_matchup(repository, home_team: str, away_team: str, *, as_of_date: str,
                    window: int = TRAILING_WINDOW_GAMES,
                    half_life_games: float | None = None,
                    game_id: int | None = None,
                    points_model_override: dict[str, Any] | None | object = _LOAD_POINTS_MODEL,
                    drives_model_override: dict[str, Any] | None | object = _LOAD_DRIVES_MODEL) -> dict[str, Any]:
    """Expected drives/plays/dropbacks/rush attempts/yardage for both sides of one game.

    Each side's numbers use its own opponent-adjusted Baseline C at every
    layer -- e.g. home's expected drives blend home's own trailing drives
    with away's trailing drives-allowed, not a shared "expected total drives"
    split in half. Nothing here is a score or a win probability; see
    docs/CFB_XYARDS.md for the chain this projects through and its own
    honestly-reported accuracy.
    """
    home_snapshot = team_snapshot(repository, home_team, before_date=as_of_date,
                                  window=window, half_life_games=half_life_games)
    away_snapshot = team_snapshot(repository, away_team, before_date=as_of_date,
                                  window=window, half_life_games=half_life_games)
    home_scoring = team_scoring_snapshot(repository, home_team, before_date=as_of_date,
                                         window=window, half_life_games=half_life_games)
    away_scoring = team_scoring_snapshot(repository, away_team, before_date=as_of_date,
                                         window=window, half_life_games=half_life_games)
    league = scoring_environment(repository, before_date=as_of_date)
    home_special = team_special_teams_snapshot(
        repository, home_team, before_date=as_of_date,
        window=window, half_life_games=half_life_games)
    away_special = team_special_teams_snapshot(
        repository, away_team, before_date=as_of_date,
        window=window, half_life_games=half_life_games)
    field_environment = field_position_environment(repository, before_date=as_of_date)
    home_points = team_points_snapshot(repository, home_team, before_date=as_of_date,
                                       window=window, half_life_games=half_life_games)
    away_points = team_points_snapshot(repository, away_team, before_date=as_of_date,
                                       window=window, half_life_games=half_life_games)
    points_league = points_environment(repository, before_date=as_of_date)
    quality = matchup_quality_snapshot(repository, game_id) if game_id is not None else {}
    if points_model_override is _LOAD_POINTS_MODEL:
        from sports_aggregator.cfb.xpoints import load_model as load_points_model
        points_model = load_points_model(repository)
    else:
        # Backtests can pass a fold-specific model trained only on seasons
        # available before this historical kickoff, or None to evaluate the
        # deployable matchup fallback without leaking today's persisted model.
        points_model = points_model_override
    if drives_model_override is _LOAD_DRIVES_MODEL:
        drives_model = load_drives_model(repository)
    else:
        drives_model = drives_model_override
    drives_league_prior = league_prior_drives_live(repository, before_date=as_of_date)
    home_elo, away_elo = _pregame_elo(repository, game_id)
    home = _project_side(home_snapshot, away_snapshot, home_scoring, away_scoring, league,
                         home_special, away_special, field_environment,
                         home_points, away_points, points_league, quality.get("home"), points_model,
                         drives_model, drives_league_prior, home_elo, away_elo, "home")
    away = _project_side(away_snapshot, home_snapshot, away_scoring, home_scoring, league,
                         away_special, home_special, field_environment,
                         away_points, home_points, points_league, quality.get("away"), points_model,
                         drives_model, drives_league_prior, away_elo, home_elo, "away")
    required = ("drives", "plays", "pass_rate", "dropbacks", "rush_attempts",
                "pass_yards", "rush_yards", "total_yards")
    return {
        "as_of_date": as_of_date,
        "home_team": home_team,
        "away_team": away_team,
        "home": home,
        "away": away,
        "home_snapshot": home_snapshot,
        "away_snapshot": away_snapshot,
        "home_scoring_snapshot": home_scoring,
        "away_scoring_snapshot": away_scoring,
        "scoring_environment": league,
        "home_special_teams_snapshot": home_special,
        "away_special_teams_snapshot": away_special,
        "field_position_environment": field_environment,
        "home_points_snapshot": home_points,
        "away_points_snapshot": away_points,
        "points_environment": points_league,
        "opponent_quality": quality,
        "points_model": ({"model_version": points_model["model_version"],
                          "training_rows": points_model["training_rows"],
                          "from_season": points_model["from_season"],
                          "to_season": points_model["to_season"]}
                         if points_model else None),
        "drives_model": ({"model_version": drives_model["model_version"],
                          "training_rows": drives_model["training_rows"],
                          "fitted_at": drives_model["fitted_at"]}
                         if drives_model else None),
        "insufficient_data": any(
            side[key] is None for side in (home, away) for key in required),
    }


def narrative(projection: dict[str, Any]) -> list[str]:
    """A short plain-English line per team -- the seed of a fuller sports-report
    format (spec section 31): each line is already built from named
    components (`home`/`away` dicts) a future version can cite individually
    ("+X from tempo, -Y from the opponent's pass defense") instead of
    rewriting this function.
    """
    lines: list[str] = []
    for team_key, opponent_key in (("home", "away"), ("away", "home")):
        team = projection[f"{team_key}_team"]
        opponent = projection[f"{opponent_key}_team"]
        side = projection[team_key]
        snapshot = projection[f"{team_key}_snapshot"]
        if any(side[key] is None for key in (
                "drives", "plays", "pass_rate", "dropbacks", "rush_attempts",
                "pass_yards", "rush_yards", "total_yards")):
            lines.append(
                f"Not enough trailing data to project {team} yet "
                f"({snapshot['games']} game{'s' if snapshot['games'] != 1 else ''} on record)."
            )
            continue
        points_clause = (f" The drive-outcome model projects {side['expected_points']:.1f} "
                         f"offensive points ({side['points_per_drive']:.2f} per drive)."
                         if side.get("expected_points") is not None else "")
        lines.append(
            f"{team} projects for {side['drives']:.1f} drives and {side['plays']:.1f} plays against "
            f"{opponent}, leaning {side['pass_rate'] * 100:.0f}% pass "
            f"({side['dropbacks']:.1f} dropbacks, {side['rush_attempts']:.1f} rush attempts) for "
            f"roughly {side['pass_yards']:.0f} passing and {side['rush_yards']:.0f} rushing yards "
            f"({side['total_yards']:.0f} total yards).{points_clause}"
        )
    return lines
