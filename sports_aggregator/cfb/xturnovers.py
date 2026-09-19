"""Leak-safe turnover-rate model (Milestone 9).

Turnovers are modeled as a per-play probability and explicitly shrunk toward
the pregame league rate.  The live game projection multiplies that rate by
xPlays; this module evaluates the rate independently so volume error is not
mistaken for turnover-model skill.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.team_game_scoring import METRIC_VERSION as SCORING_VERSION
from sports_aggregator.cfb.team_game_scoring import initialize as initialize_scoring
from sports_aggregator.cfb.xdrives import RECENCY_LAMBDA, TRAILING_WINDOW_GAMES

DATASET_VERSION = "xturnovers-dataset-v1"
SHRINKAGE_PSEUDO_GAMES = 6.0


@schema_once("xturnovers")
def initialize(repository) -> None:
    initialize_scoring(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_xturnovers_dataset (
          game_id INTEGER NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
          season INTEGER NOT NULL, week INTEGER, home_away TEXT NOT NULL,
          dataset_version TEXT NOT NULL,
          actual_giveaways INTEGER NOT NULL, actual_giveaway_rate REAL,
          team_prior_games INTEGER NOT NULL, team_prior_giveaways REAL,
          team_prior_giveaway_rate REAL,
          opponent_prior_games INTEGER NOT NULL, opponent_prior_takeaways REAL,
          opponent_prior_takeaway_rate REAL,
          league_prior_giveaway_rate REAL, built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,dataset_version)
        );
        """)
        connection.commit()


def _weights(count: int) -> list[float]:
    import math
    return [math.exp(-RECENCY_LAMBDA * (count - 1 - i)) for i in range(count)]


def _mean(rows, key: str) -> float | None:
    weights = _weights(len(rows))
    pairs = [(weight, row.get(key)) for weight, row in zip(weights, rows)
             if row.get(key) is not None]
    total = sum(weight for weight, _ in pairs)
    return (sum(weight * float(value) for weight, value in pairs) / total
            if pairs and total else None)


def _ratio(rows, numerator: str, denominator: str) -> float | None:
    weighted_numerator = weighted_denominator = 0.0
    for weight, row in zip(_weights(len(rows)), rows):
        if row.get(numerator) is None or row.get(denominator) is None:
            continue
        weighted_numerator += weight * float(row[numerator])
        weighted_denominator += weight * float(row[denominator])
    return weighted_numerator / weighted_denominator if weighted_denominator else None


def build_dataset(repository, *, from_season: int | None = None,
                  to_season: int | None = None,
                  dataset_version: str = DATASET_VERSION) -> dict[str, Any]:
    initialize(repository)
    clauses = ["s.metric_version=?"]
    params: list[Any] = [SCORING_VERSION]
    if from_season is not None:
        clauses.append("g.season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("g.season<=?"); params.append(int(to_season))
    with repository._reader() as connection:
        actuals = [dict(row) for row in connection.execute(f"""
          SELECT s.*,g.season,g.week,g.start_date,g.home_team,g.away_team
          FROM cfb_team_game_scoring s JOIN games g USING(game_id)
          WHERE {' AND '.join(clauses)}
          ORDER BY g.start_date,g.game_id,s.team
        """, params).fetchall()]

    own: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    allowed: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    league_giveaways = league_plays = 0
    now = datetime.now(timezone.utc).isoformat()
    output = []
    by_game: dict[int, list[dict[str, Any]]] = defaultdict(list)
    order = []
    for row in actuals:
        if row["game_id"] not in by_game:
            order.append(row["game_id"])
        by_game[row["game_id"]].append(row)
    for game_id in order:
        game_rows = by_game[game_id]
        for row in game_rows:
            team, opponent = row["team"], row["opponent"]
            team_window, opponent_allowed = own[team], allowed[opponent]
            output.append((
                game_id, team, opponent, row["season"], row["week"],
                "home" if team == row["home_team"] else "away", dataset_version,
                row["giveaways"], row["giveaway_rate"], len(team_window),
                _mean(team_window, "giveaways"), _ratio(team_window, "giveaways", "competitive_plays"),
                len(opponent_allowed), _mean(opponent_allowed, "giveaways"),
                _ratio(opponent_allowed, "giveaways", "competitive_plays"),
                league_giveaways / league_plays if league_plays else None, now,
            ))
        for row in game_rows:
            own[row["team"]].append(row)
            allowed[row["opponent"]].append(row)
            league_giveaways += int(row["giveaways"])
            league_plays += int(row["competitive_plays"])

    with closing(repository._connect()) as connection:
        if from_season is not None or to_season is not None:
            filters, values = [], []
            if from_season is not None:
                filters.append("season>=?"); values.append(int(from_season))
            if to_season is not None:
                filters.append("season<=?"); values.append(int(to_season))
            connection.execute(f"""DELETE FROM cfb_xturnovers_dataset
              WHERE dataset_version=? AND game_id IN
              (SELECT game_id FROM games WHERE {' AND '.join(filters)})""",
              (dataset_version, *values))
        else:
            connection.execute("DELETE FROM cfb_xturnovers_dataset WHERE dataset_version=?",
                               (dataset_version,))
        connection.executemany("""INSERT INTO cfb_xturnovers_dataset VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)
        connection.commit()
    return {"dataset_version": dataset_version, "rows": len(output)}


def _finish(errors: list[tuple[float, float]]) -> dict[str, Any]:
    import math
    if not errors:
        return {"games": 0, "mae": None, "rmse": None, "bias": None}
    diffs = [predicted - actual for predicted, actual in errors]
    return {"games": len(errors),
            "mae": round(sum(abs(value) for value in diffs) / len(diffs), 6),
            "rmse": round(math.sqrt(sum(value * value for value in diffs) / len(diffs)), 6),
            "bias": round(sum(diffs) / len(diffs), 6)}


def evaluate_baselines(repository, *, from_season: int | None = None,
                       to_season: int | None = None,
                       dataset_version: str = DATASET_VERSION) -> dict[str, Any]:
    initialize(repository)
    clauses = ["dataset_version=?"]
    params: list[Any] = [dataset_version]
    if from_season is not None:
        clauses.append("season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("season<=?"); params.append(int(to_season))
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            f"SELECT * FROM cfb_xturnovers_dataset WHERE {' AND '.join(clauses)}", params)]
    errors = {key: [] for key in ("A_league_prior", "B_team_rate", "C_matchup_blend", "D_shrunk_blend")}
    dropped = 0
    for row in rows:
        actual = row.get("actual_giveaway_rate")
        league = row.get("league_prior_giveaway_rate")
        team = row.get("team_prior_giveaway_rate")
        takeaways = row.get("opponent_prior_takeaway_rate")
        if actual is None or league is None or team is None or takeaways is None:
            dropped += 1
            continue
        blend = (team + takeaways) / 2
        sample = min(row["team_prior_games"], row["opponent_prior_games"])
        weight = sample / (sample + SHRINKAGE_PSEUDO_GAMES)
        predictions = (league, team, blend, weight * blend + (1 - weight) * league)
        for key, predicted in zip(errors, predictions):
            errors[key].append((predicted, actual))
    return {"dataset_version": dataset_version, "target": "giveaways_per_play",
            "rows": len(rows), "rows_dropped_cold_start": dropped,
            "overall": {key: _finish(value) for key, value in errors.items()}}
