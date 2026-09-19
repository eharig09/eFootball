"""Pregame team-shape profiles and matchup interaction backtest.

A team shape is intentionally descriptive rather than outcome-targeted.  It is
built only from information available before kickoff, then evaluated in two
ways:

1. Similarity: do teams with similar pregame shapes produce similar next-game
   pace, efficiency and run/pass outcomes?
2. Interaction: does offense-shape x opponent-shape explain outcomes better
   than offense shape alone?

QB is deliberately left outside the base shape.  A later modifier can be
layered on and ablated cleanly against the football/system shape.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import math
from typing import Any, Iterable

from sports_aggregator.cfb.game_projection import team_points_snapshot, team_snapshot
from sports_aggregator.cfb.repository import CFBRepository, schema_once
from sports_aggregator.cfb.team_game_drive_outcomes import METRIC_VERSION as OUTCOME_VERSION
from sports_aggregator.cfb.team_game_pace import METRIC_VERSION as PACE_VERSION

SHAPE_VERSION = "team-shape-v1"

FEATURES = (
    "pace_drives",
    "pace_plays_per_drive",
    "off_points_per_drive",
    "off_yards_per_dropback",
    "off_yards_per_rush",
    "off_pass_rate",
    "def_drives_allowed",
    "def_points_per_drive_allowed",
    "def_yards_per_dropback_allowed",
    "def_yards_per_rush_allowed",
    "def_pass_rate_allowed",
)

OUTCOMES = (
    "actual_drives",
    "actual_points_per_drive",
    "actual_pass_rate",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfb_team_shape_backtest (
  game_id INTEGER NOT NULL,
  team TEXT NOT NULL,
  opponent TEXT NOT NULL,
  side TEXT NOT NULL,
  shape_version TEXT NOT NULL,
  season INTEGER NOT NULL,
  week INTEGER,
  kickoff TEXT NOT NULL,
  prior_games INTEGER NOT NULL,

  pace_drives REAL,
  pace_plays_per_drive REAL,
  off_points_per_drive REAL,
  off_yards_per_dropback REAL,
  off_yards_per_rush REAL,
  off_pass_rate REAL,
  def_drives_allowed REAL,
  def_points_per_drive_allowed REAL,
  def_yards_per_dropback_allowed REAL,
  def_yards_per_rush_allowed REAL,
  def_pass_rate_allowed REAL,

  actual_drives REAL,
  actual_points_per_drive REAL,
  actual_pass_rate REAL,
  actual_total_yards REAL,
  actual_score_points REAL,

  PRIMARY KEY(game_id, team, shape_version)
);
CREATE INDEX IF NOT EXISTS idx_cfb_team_shape_backtest_season
  ON cfb_team_shape_backtest(shape_version, season, week);
"""


@schema_once("team_shapes")
def initialize(repository: CFBRepository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def _metrics(pairs: Iterable[tuple[float, float]]) -> dict[str, Any]:
    values = [(float(p), float(a)) for p, a in pairs]
    if not values:
        return {"n": 0, "mae": None, "rmse": None, "bias": None}
    errors = [p - a for p, a in values]
    return {
        "n": len(values),
        "mae": round(sum(abs(e) for e in errors) / len(errors), 4),
        "rmse": round(math.sqrt(sum(e * e for e in errors) / len(errors)), 4),
        "bias": round(sum(errors) / len(errors), 4),
    }


def _pearson(pairs: Iterable[tuple[float, float]]) -> float | None:
    values = [(float(x), float(y)) for x, y in pairs]
    if len(values) < 3:
        return None
    mx = sum(x for x, _ in values) / len(values)
    my = sum(y for _, y in values) / len(values)
    num = sum((x - mx) * (y - my) for x, y in values)
    dx = math.sqrt(sum((x - mx) ** 2 for x, _ in values))
    dy = math.sqrt(sum((y - my) ** 2 for _, y in values))
    return round(num / (dx * dy), 4) if dx and dy else None


def build(repository: CFBRepository, *, from_season: int, to_season: int,
          shape_version: str = SHAPE_VERSION, min_prior_games: int = 1) -> dict[str, Any]:
    """Persist leak-safe pregame shape vectors and the subsequent game outcomes."""
    initialize(repository)
    with repository._reader() as connection:
        games = [dict(row) for row in connection.execute(
            """SELECT game_id,season,week,start_date,home_team,away_team,
                      home_points,away_points
               FROM games
               WHERE completed=1 AND season BETWEEN ? AND ?
                 AND start_date IS NOT NULL
               ORDER BY start_date,game_id""",
            (int(from_season), int(to_season)),
        )]
        pace_rows = {
            (int(row["game_id"]), str(row["team"])): dict(row)
            for row in connection.execute(
                """SELECT game_id,team,meaningful_drives,scrimmage_plays,pass_plays,
                          pass_yards,rush_yards
                   FROM cfb_team_game_pace WHERE metric_version=?""",
                (PACE_VERSION,),
            )
        }
        outcome_rows = {
            (int(row["game_id"]), str(row["team"])): dict(row)
            for row in connection.execute(
                """SELECT game_id,team,points_per_drive
                   FROM cfb_team_game_drive_outcomes WHERE metric_version=?""",
                (OUTCOME_VERSION,),
            )
        }

    rows = []
    skipped = 0
    for game in games:
        for side in ("home", "away"):
            team = str(game[f"{side}_team"])
            other = "away" if side == "home" else "home"
            opponent = str(game[f"{other}_team"])
            kickoff = str(game["start_date"])
            pace = team_snapshot(repository, team, before_date=kickoff)
            points = team_points_snapshot(repository, team, before_date=kickoff)
            prior_games = int(pace.get("games") or 0)
            if prior_games < int(min_prior_games):
                skipped += 1
                continue
            actual_pace = pace_rows.get((int(game["game_id"]), team), {})
            actual_points = outcome_rows.get((int(game["game_id"]), team), {})
            plays = actual_pace.get("scrimmage_plays")
            pass_plays = actual_pace.get("pass_plays")
            actual_pass_rate = (
                float(pass_plays) / float(plays)
                if pass_plays is not None and plays not in (None, 0) else None
            )
            pass_yards = actual_pace.get("pass_yards")
            rush_yards = actual_pace.get("rush_yards")
            total_yards = (
                float(pass_yards) + float(rush_yards)
                if pass_yards is not None and rush_yards is not None else None
            )
            score = game["home_points"] if side == "home" else game["away_points"]
            rows.append((
                int(game["game_id"]), team, opponent, side, shape_version,
                int(game["season"]), game.get("week"), kickoff, prior_games,
                pace.get("drives"), pace.get("plays_per_drive"),
                points.get("points_per_drive"),
                pace.get("yards_per_dropback"), pace.get("yards_per_rush"),
                pace.get("pass_rate"),
                pace.get("drives_allowed"),
                points.get("points_per_drive_allowed"),
                pace.get("yards_per_dropback_allowed"),
                pace.get("yards_per_rush_allowed"),
                pace.get("pass_rate_allowed"),
                actual_pace.get("meaningful_drives"),
                actual_points.get("points_per_drive"),
                actual_pass_rate, total_yards, score,
            ))

    with repository.transaction() as connection:
        connection.execute(
            """DELETE FROM cfb_team_shape_backtest
               WHERE shape_version=? AND season BETWEEN ? AND ?""",
            (shape_version, int(from_season), int(to_season)),
        )
        connection.executemany(
            """INSERT INTO cfb_team_shape_backtest (
               game_id,team,opponent,side,shape_version,season,week,kickoff,prior_games,
               pace_drives,pace_plays_per_drive,off_points_per_drive,
               off_yards_per_dropback,off_yards_per_rush,off_pass_rate,
               def_drives_allowed,def_points_per_drive_allowed,
               def_yards_per_dropback_allowed,def_yards_per_rush_allowed,
               def_pass_rate_allowed,actual_drives,actual_points_per_drive,
               actual_pass_rate,actual_total_yards,actual_score_points
             ) VALUES (
               ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    return {
        "shape_version": shape_version,
        "from_season": int(from_season),
        "to_season": int(to_season),
        "rows": len(rows),
        "skipped_team_games": skipped,
        "features": list(FEATURES),
    }


def _fit_scaler(rows: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    scaler = {}
    for key in FEATURES:
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        if not values:
            scaler[key] = (0.0, 1.0)
            continue
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        scaler[key] = (mean, math.sqrt(variance) or 1.0)
    return scaler


def _vector(row: dict[str, Any], scaler: dict[str, tuple[float, float]]) -> list[float] | None:
    if any(row.get(key) is None for key in FEATURES):
        return None
    return [
        (float(row[key]) - scaler[key][0]) / scaler[key][1]
        for key in FEATURES
    ]


def _distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)) / len(a))


def _kmeans(vectors: list[list[float]], *, k: int = 8, iterations: int = 40) -> list[list[float]]:
    if not vectors:
        return []
    k = max(1, min(int(k), len(vectors)))
    centroids = [vectors[0][:]]
    while len(centroids) < k:
        candidate = max(
            vectors,
            key=lambda vector: min(_distance(vector, centroid) for centroid in centroids),
        )
        centroids.append(candidate[:])
    for _ in range(iterations):
        groups = [[] for _ in centroids]
        for vector in vectors:
            index = min(range(len(centroids)), key=lambda i: _distance(vector, centroids[i]))
            groups[index].append(vector)
        updated = []
        for centroid, group in zip(centroids, groups):
            if not group:
                updated.append(centroid)
                continue
            updated.append([
                sum(vector[i] for vector in group) / len(group)
                for i in range(len(centroid))
            ])
        if all(_distance(a, b) < 1e-6 for a, b in zip(centroids, updated)):
            centroids = updated
            break
        centroids = updated
    return centroids


def _cluster(vector: list[float], centroids: list[list[float]]) -> int:
    return min(range(len(centroids)), key=lambda i: _distance(vector, centroids[i]))


def _archetype_label(centroid: list[float]) -> str:
    index = {key: i for i, key in enumerate(FEATURES)}
    pace = (centroid[index["pace_drives"]] + centroid[index["pace_plays_per_drive"]]) / 2
    mix = centroid[index["off_pass_rate"]]
    offense = (
        centroid[index["off_points_per_drive"]]
        + centroid[index["off_yards_per_dropback"]]
        + centroid[index["off_yards_per_rush"]]
    ) / 3
    # Lower allowed efficiency is better defense.
    defense = -(
        centroid[index["def_points_per_drive_allowed"]]
        + centroid[index["def_yards_per_dropback_allowed"]]
        + centroid[index["def_yards_per_rush_allowed"]]
    ) / 3
    pace_label = "Fast" if pace > .45 else "Slow" if pace < -.45 else "Balanced pace"
    mix_label = "Pass-heavy" if mix > .45 else "Run-heavy" if mix < -.45 else "Balanced mix"
    off_label = "Efficient O" if offense > .45 else "Low-eff O" if offense < -.45 else "Average O"
    def_label = "Strong D" if defense > .45 else "Weak D" if defense < -.45 else "Average D"
    return " / ".join((pace_label, mix_label, off_label, def_label))


def _neighbor_analysis(train: list[dict[str, Any]], test: list[dict[str, Any]],
                       scaler: dict[str, tuple[float, float]], *, neighbors: int = 25) -> dict[str, Any]:
    train_vectors = [(row, _vector(row, scaler)) for row in train]
    train_vectors = [(row, vec) for row, vec in train_vectors if vec is not None]
    result = {}
    for outcome in OUTCOMES:
        predictions = []
        global_values = [float(row[outcome]) for row in train if row.get(outcome) is not None]
        global_mean = _mean(global_values)
        baseline = []
        distance_error = []
        for row in test:
            actual = row.get(outcome)
            vec = _vector(row, scaler)
            if actual is None or vec is None:
                continue
            candidates = [
                (_distance(vec, other_vec), other)
                for other, other_vec in train_vectors
                if other["team"] != row["team"] and other.get(outcome) is not None
            ]
            candidates.sort(key=lambda item: item[0])
            nearest = candidates[:neighbors]
            if not nearest:
                continue
            prediction = sum(float(item[1][outcome]) for item in nearest) / len(nearest)
            avg_distance = sum(item[0] for item in nearest) / len(nearest)
            predictions.append((prediction, float(actual)))
            distance_error.append((avg_distance, abs(prediction - float(actual))))
            if global_mean is not None:
                baseline.append((global_mean, float(actual)))
        result[outcome] = {
            "nearest_neighbor": _metrics(predictions),
            "global_mean_baseline": _metrics(baseline),
            "prediction_correlation": _pearson(predictions),
            "distance_vs_absolute_error_correlation": _pearson(distance_error),
            "neighbors": neighbors,
            "same_team_excluded": True,
        }
    return result


def _interaction_analysis(train: list[dict[str, Any]], test: list[dict[str, Any]],
                          scaler: dict[str, tuple[float, float]],
                          centroids: list[list[float]], *, min_pair_rows: int = 25) -> dict[str, Any]:
    # Attach each row's opponent profile from the same game.
    train_by_game = {(int(row["game_id"]), str(row["team"])): row for row in train}
    test_by_game = {(int(row["game_id"]), str(row["team"])): row for row in test}

    def assignments(rows, lookup):
        output = []
        for row in rows:
            vec = _vector(row, scaler)
            opponent = lookup.get((int(row["game_id"]), str(row["opponent"])))
            opp_vec = _vector(opponent, scaler) if opponent else None
            if vec is None or opp_vec is None:
                continue
            output.append((row, _cluster(vec, centroids), _cluster(opp_vec, centroids)))
        return output

    train_assigned = assignments(train, train_by_game)
    test_assigned = assignments(test, test_by_game)
    result = {}
    cells = []
    for outcome in OUTCOMES:
        offense_values: dict[int, list[float]] = defaultdict(list)
        pair_values: dict[tuple[int, int], list[float]] = defaultdict(list)
        for row, own, opp in train_assigned:
            if row.get(outcome) is None:
                continue
            value = float(row[outcome])
            offense_values[own].append(value)
            pair_values[(own, opp)].append(value)
        baseline_pairs = []
        interaction_pairs = []
        for row, own, opp in test_assigned:
            if row.get(outcome) is None or not offense_values.get(own):
                continue
            actual = float(row[outcome])
            base = sum(offense_values[own]) / len(offense_values[own])
            pair = pair_values.get((own, opp), [])
            interaction = sum(pair) / len(pair) if len(pair) >= min_pair_rows else base
            baseline_pairs.append((base, actual))
            interaction_pairs.append((interaction, actual))
        base_metrics = _metrics(baseline_pairs)
        interaction_metrics = _metrics(interaction_pairs)
        result[outcome] = {
            "own_shape_only": base_metrics,
            "shape_interaction": interaction_metrics,
            "mae_delta_vs_own_shape": (
                round(interaction_metrics["mae"] - base_metrics["mae"], 4)
                if interaction_metrics["mae"] is not None and base_metrics["mae"] is not None
                else None
            ),
        }
        for (own, opp), values in pair_values.items():
            if len(values) < min_pair_rows or not offense_values.get(own):
                continue
            cells.append({
                "outcome": outcome,
                "own_shape": own,
                "opponent_shape": opp,
                "training_rows": len(values),
                "pair_mean": round(sum(values) / len(values), 4),
                "own_shape_mean": round(sum(offense_values[own]) / len(offense_values[own]), 4),
                "interaction_effect": round(
                    sum(values) / len(values) - sum(offense_values[own]) / len(offense_values[own]), 4),
            })
    return {"outcomes": result, "cells": cells, "min_pair_rows": min_pair_rows}


def report(repository: CFBRepository, *, test_season: int,
           shape_version: str = SHAPE_VERSION, clusters: int = 8,
           neighbors: int = 25, min_prior_games: int = 3) -> dict[str, Any]:
    initialize(repository)
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT * FROM cfb_team_shape_backtest
               WHERE shape_version=? AND prior_games>=?
               ORDER BY season,week,kickoff,game_id,side""",
            (shape_version, int(min_prior_games)),
        )]
    train = [row for row in rows if int(row["season"]) < int(test_season)]
    test = [row for row in rows if int(row["season"]) == int(test_season)]
    complete_train = [row for row in train if all(row.get(key) is not None for key in FEATURES)]
    scaler = _fit_scaler(complete_train)
    train_vectors = [_vector(row, scaler) for row in complete_train]
    train_vectors = [vec for vec in train_vectors if vec is not None]
    centroids = _kmeans(train_vectors, k=clusters)

    archetypes = []
    for index, centroid in enumerate(centroids):
        members = [
            row for row in complete_train
            if _cluster(_vector(row, scaler), centroids) == index
        ]
        archetypes.append({
            "id": index,
            "label": _archetype_label(centroid),
            "training_rows": len(members),
            "centroid_z": {
                key: round(centroid[i], 3) for i, key in enumerate(FEATURES)
            },
        })

    return {
        "shape_version": shape_version,
        "test_season": int(test_season),
        "train_seasons": (
            [min(int(row["season"]) for row in train), max(int(row["season"]) for row in train)]
            if train else None
        ),
        "training_rows": len(train),
        "test_rows": len(test),
        "features": list(FEATURES),
        "archetypes": archetypes,
        "similarity": _neighbor_analysis(train, test, scaler, neighbors=neighbors),
        "interactions": _interaction_analysis(train, test, scaler, centroids),
        "qb_modifier": {
            "included": False,
            "reason": (
                "Base shape is intentionally QB-agnostic. Add QB as a separate pregame modifier "
                "and compare incremental nearest-neighbor / interaction performance once a stable "
                "historical QB-quality feature is wired in."
            ),
        },
        "notes": [
            "All shape features are reconstructed strictly before kickoff.",
            "Nearest-neighbor evaluation excludes the same team to test whether shape transfers across programs.",
            "Archetypes are fit only on seasons before the held-out test season.",
            "Interaction cells compare own-shape x opponent-shape history against own-shape history alone.",
        ],
    }
