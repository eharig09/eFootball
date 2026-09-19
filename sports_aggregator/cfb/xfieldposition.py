"""Leak-safe field-position, punting, and kicking baselines (Milestone 10)."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.team_game_special_teams import METRIC_VERSION as SPECIAL_VERSION
from sports_aggregator.cfb.team_game_special_teams import initialize as initialize_special
from sports_aggregator.cfb.xdrives import RECENCY_LAMBDA, TRAILING_WINDOW_GAMES

DATASET_VERSION = "xfieldposition-dataset-v1"


@schema_once("xfieldposition")
def initialize(repository) -> None:
    initialize_special(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_xfieldposition_dataset (
          game_id INTEGER NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
          season INTEGER NOT NULL, week INTEGER, home_away TEXT NOT NULL,
          dataset_version TEXT NOT NULL,
          actual_start_yards_to_goal REAL, actual_start_after_punt REAL,
          actual_field_goal_accuracy REAL, actual_field_goal_attempts INTEGER NOT NULL,
          team_prior_games INTEGER NOT NULL, team_prior_start_yards_to_goal REAL,
          team_prior_start_after_punt REAL, team_prior_field_goal_accuracy REAL,
          team_prior_field_goal_distance REAL,
          opponent_prior_games INTEGER NOT NULL,
          opponent_prior_start_yards_to_goal_allowed REAL,
          opponent_prior_start_after_punt_imposed REAL,
          league_prior_start_yards_to_goal REAL, league_prior_start_after_punt REAL,
          league_prior_field_goal_accuracy REAL, built_at TEXT NOT NULL,
          actual_net_punt_yards REAL, team_prior_net_punt_yards REAL,
          league_prior_net_punt_yards REAL,
          PRIMARY KEY(game_id,team,dataset_version)
        );
        """)
        existing = {str(row[1]) for row in connection.execute(
            "PRAGMA table_info(cfb_xfieldposition_dataset)")}
        for column in ("actual_net_punt_yards", "team_prior_net_punt_yards",
                       "league_prior_net_punt_yards"):
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE cfb_xfieldposition_dataset ADD COLUMN {column} REAL")
        connection.commit()


def _weights(count: int) -> list[float]:
    import math
    return [math.exp(-RECENCY_LAMBDA * (count - 1 - i)) for i in range(count)]


def _ratio(rows, numerator: str, denominator: str) -> float | None:
    top = bottom = 0.0
    for weight, row in zip(_weights(len(rows)), rows):
        if row.get(numerator) is None or row.get(denominator) is None:
            continue
        top += weight * float(row[numerator]); bottom += weight * float(row[denominator])
    return top / bottom if bottom else None


def build_dataset(repository, *, from_season: int | None = None,
                  to_season: int | None = None,
                  dataset_version: str = DATASET_VERSION) -> dict[str, Any]:
    initialize(repository)
    clauses = ["s.metric_version=?"]
    params: list[Any] = [SPECIAL_VERSION]
    if from_season is not None:
        clauses.append("g.season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("g.season<=?"); params.append(int(to_season))
    with repository._reader() as connection:
        actuals = [dict(row) for row in connection.execute(f"""
          SELECT s.*,g.season,g.week,g.start_date,g.home_team,g.away_team
          FROM cfb_team_game_special_teams s JOIN games g USING(game_id)
          WHERE {' AND '.join(clauses)} ORDER BY g.start_date,g.game_id,s.team
        """, params)]

    own: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    allowed: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    league = defaultdict(float)
    now = datetime.now(timezone.utc).isoformat()
    output = []
    by_game: dict[int, list[dict[str, Any]]] = defaultdict(list); order = []
    for row in actuals:
        if row["game_id"] not in by_game:
            order.append(row["game_id"])
        by_game[row["game_id"]].append(row)
    for game_id in order:
        rows = by_game[game_id]
        for row in rows:
            team, opponent = row["team"], row["opponent"]
            team_window, defense_window, opponent_window = own[team], allowed[opponent], own[opponent]
            output.append((
                game_id, team, opponent, row["season"], row["week"],
                "home" if team == row["home_team"] else "away", dataset_version,
                row["average_start_yards_to_goal"], row["average_start_after_punt"],
                row["field_goal_accuracy"], row["field_goal_attempts"],
                len(team_window), _ratio(team_window, "start_yards_to_goal_total", "offensive_starts"),
                _ratio(team_window, "punt_start_yards_to_goal_total", "punt_returns"),
                _ratio(team_window, "field_goals_made", "field_goal_attempts"),
                _ratio(team_window, "field_goal_distance_total", "field_goal_attempts"),
                len(defense_window),
                _ratio(defense_window, "start_yards_to_goal_total", "offensive_starts"),
                _ratio(opponent_window, "opponent_punt_start_yards_to_goal_total", "punts"),
                league["start_total"] / league["starts"] if league["starts"] else None,
                league["punt_start_total"] / league["punt_returns"] if league["punt_returns"] else None,
                league["fgm"] / league["fga"] if league["fga"] else None, now,
                row["average_net_punt_yards"],
                _ratio(team_window, "net_punt_yards_total", "punts"),
                league["net_punt_total"] / league["punts"] if league["punts"] else None,
            ))
        for row in rows:
            own[row["team"]].append(row); allowed[row["opponent"]].append(row)
            league["start_total"] += row["start_yards_to_goal_total"] or 0
            league["starts"] += row["offensive_starts"] or 0
            league["punt_start_total"] += row["punt_start_yards_to_goal_total"] or 0
            league["punt_returns"] += row["punt_returns"] or 0
            league["fgm"] += row["field_goals_made"] or 0
            league["fga"] += row["field_goal_attempts"] or 0
            league["net_punt_total"] += row["net_punt_yards_total"] or 0
            league["punts"] += row["punts"] or 0

    with closing(repository._connect()) as connection:
        if from_season is not None or to_season is not None:
            filters, values = [], []
            if from_season is not None:
                filters.append("season>=?"); values.append(int(from_season))
            if to_season is not None:
                filters.append("season<=?"); values.append(int(to_season))
            connection.execute(f"""DELETE FROM cfb_xfieldposition_dataset
              WHERE dataset_version=? AND game_id IN
              (SELECT game_id FROM games WHERE {' AND '.join(filters)})""",
              (dataset_version, *values))
        else:
            connection.execute("DELETE FROM cfb_xfieldposition_dataset WHERE dataset_version=?",
                               (dataset_version,))
        connection.executemany("""INSERT INTO cfb_xfieldposition_dataset(
          game_id,team,opponent,season,week,home_away,dataset_version,
          actual_start_yards_to_goal,actual_start_after_punt,actual_field_goal_accuracy,
          actual_field_goal_attempts,team_prior_games,team_prior_start_yards_to_goal,
          team_prior_start_after_punt,team_prior_field_goal_accuracy,
          team_prior_field_goal_distance,opponent_prior_games,
          opponent_prior_start_yards_to_goal_allowed,
          opponent_prior_start_after_punt_imposed,league_prior_start_yards_to_goal,
          league_prior_start_after_punt,league_prior_field_goal_accuracy,built_at,
          actual_net_punt_yards,team_prior_net_punt_yards,league_prior_net_punt_yards)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)
        connection.commit()
    return {"dataset_version": dataset_version, "rows": len(output)}


def _finish(errors: list[tuple[float, float]]) -> dict[str, Any]:
    import math
    if not errors:
        return {"games": 0, "mae": None, "rmse": None, "bias": None}
    diffs = [predicted - actual for predicted, actual in errors]
    return {"games": len(errors),
            "mae": round(sum(abs(value) for value in diffs) / len(diffs), 5),
            "rmse": round(math.sqrt(sum(value * value for value in diffs) / len(diffs)), 5),
            "bias": round(sum(diffs) / len(diffs), 5)}


def evaluate_baselines(repository, *, from_season: int | None = None,
                       to_season: int | None = None,
                       dataset_version: str = DATASET_VERSION) -> dict[str, Any]:
    initialize(repository)
    clauses = ["dataset_version=?"]; params: list[Any] = [dataset_version]
    if from_season is not None:
        clauses.append("season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("season<=?"); params.append(int(to_season))
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            f"SELECT * FROM cfb_xfieldposition_dataset WHERE {' AND '.join(clauses)}", params)]
    definitions = {
        "start_yards_to_goal": ("actual_start_yards_to_goal", "team_prior_start_yards_to_goal",
                                "opponent_prior_start_yards_to_goal_allowed",
                                "league_prior_start_yards_to_goal"),
        "start_after_punt": ("actual_start_after_punt", "team_prior_start_after_punt",
                             "opponent_prior_start_after_punt_imposed",
                             "league_prior_start_after_punt"),
    }
    reports = {}
    for label, keys in definitions.items():
        errors = {name: [] for name in ("A_league_prior", "B_team_rate", "C_matchup_blend")}
        for row in rows:
            actual, team, opponent, league = (row.get(key) for key in keys)
            if any(value is None for value in (actual, team, opponent, league)):
                continue
            for name, predicted in zip(errors, (league, team, (team + opponent) / 2)):
                errors[name].append((predicted, actual))
        reports[label] = {name: _finish(values) for name, values in errors.items()}
    fg_errors = {"A_league_prior": [], "B_team_rate": []}
    for row in rows:
        actual = row.get("actual_field_goal_accuracy")
        league = row.get("league_prior_field_goal_accuracy")
        team = row.get("team_prior_field_goal_accuracy")
        if actual is None or league is None or team is None:
            continue
        fg_errors["A_league_prior"].append((league, actual))
        fg_errors["B_team_rate"].append((team, actual))
    reports["field_goal_accuracy"] = {name: _finish(values) for name, values in fg_errors.items()}
    punt_errors = {"A_league_prior": [], "B_team_rate": []}
    for row in rows:
        actual = row.get("actual_net_punt_yards")
        league = row.get("league_prior_net_punt_yards")
        team = row.get("team_prior_net_punt_yards")
        if actual is None or league is None or team is None:
            continue
        punt_errors["A_league_prior"].append((league, actual))
        punt_errors["B_team_rate"].append((team, actual))
    reports["net_punt_yards"] = {name: _finish(values) for name, values in punt_errors.items()}
    return {"dataset_version": dataset_version, "rows": len(rows), **reports}
