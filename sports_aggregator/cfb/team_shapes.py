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


def robustness_report(repository: CFBRepository, *, test_season: int,
                      shape_version: str = SHAPE_VERSION,
                      cluster_grid: tuple[int, ...] = (6, 8, 10, 12),
                      neighbor_grid: tuple[int, ...] = (10, 25, 50),
                      min_prior_games: int = 3) -> dict[str, Any]:
    """Sweep reasonable shape definitions and summarize held-out stability."""
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

    similarity_grid = []
    for neighbors in neighbor_grid:
        similarity = _neighbor_analysis(train, test, scaler, neighbors=int(neighbors))
        row = {"neighbors": int(neighbors)}
        for outcome, payload in similarity.items():
            nn = payload["nearest_neighbor"]
            base = payload["global_mean_baseline"]
            row[outcome] = {
                "nearest_neighbor_mae": nn["mae"],
                "baseline_mae": base["mae"],
                "mae_delta_vs_baseline": (
                    round(nn["mae"] - base["mae"], 4)
                    if nn["mae"] is not None and base["mae"] is not None else None
                ),
                "prediction_correlation": payload["prediction_correlation"],
                "distance_error_correlation": payload["distance_vs_absolute_error_correlation"],
            }
        similarity_grid.append(row)

    interaction_grid = []
    for clusters in cluster_grid:
        centroids = _kmeans(train_vectors, k=int(clusters))
        interaction = _interaction_analysis(train, test, scaler, centroids)
        row = {"clusters": int(clusters)}
        for outcome, payload in interaction["outcomes"].items():
            row[outcome] = {
                "own_shape_mae": payload["own_shape_only"]["mae"],
                "interaction_mae": payload["shape_interaction"]["mae"],
                "mae_delta_vs_own_shape": payload["mae_delta_vs_own_shape"],
            }
        interaction_grid.append(row)

    def stability(grid: list[dict[str, Any]], outcome: str, delta_key: str) -> dict[str, Any]:
        deltas = [
            float(item[outcome][delta_key])
            for item in grid
            if item.get(outcome, {}).get(delta_key) is not None
        ]
        if not deltas:
            return {"runs": 0, "improved_runs": 0, "improved_rate": None,
                    "mean_mae_delta": None, "best_mae_delta": None, "worst_mae_delta": None}
        return {
            "runs": len(deltas),
            "improved_runs": sum(1 for value in deltas if value < 0),
            "improved_rate": round(sum(1 for value in deltas if value < 0) / len(deltas), 4),
            "mean_mae_delta": round(sum(deltas) / len(deltas), 4),
            "best_mae_delta": round(min(deltas), 4),
            "worst_mae_delta": round(max(deltas), 4),
        }

    return {
        "shape_version": shape_version,
        "test_season": int(test_season),
        "train_seasons": (
            [min(int(row["season"]) for row in train), max(int(row["season"]) for row in train)]
            if train else None
        ),
        "training_rows": len(train),
        "test_rows": len(test),
        "cluster_grid": list(cluster_grid),
        "neighbor_grid": list(neighbor_grid),
        "similarity_grid": similarity_grid,
        "interaction_grid": interaction_grid,
        "stability": {
            "similarity": {
                outcome: stability(
                    similarity_grid, outcome, "mae_delta_vs_baseline")
                for outcome in OUTCOMES
            },
            "interactions": {
                outcome: stability(
                    interaction_grid, outcome, "mae_delta_vs_own_shape")
                for outcome in OUTCOMES
            },
        },
        "decision_rule": (
            "Treat a shape signal as robust only if held-out MAE improves across most reasonable "
            "neighbor/cluster choices, rather than only at a single tuned configuration."
        ),
    }


def full_chain_ablation(repository: CFBRepository, *, test_season: int,
                        shape_version: str = SHAPE_VERSION, clusters: int = 8,
                        min_prior_games: int = 3, min_pair_rows: int = 25,
                        backtest_version: str = "game-projection-backtest-v1") -> dict[str, Any]:
    """Test shape interaction as an incremental layer on validated xDrives/xPlays.

    Volume always comes from cfb_projection_backtest's production historical
    projection. Shape is allowed to adjust PPD and pass rate only.
    """
    initialize(repository)
    with repository._reader() as connection:
        shape_rows = [dict(row) for row in connection.execute(
            """SELECT * FROM cfb_team_shape_backtest
               WHERE shape_version=? AND prior_games>=?
               ORDER BY season,week,kickoff,game_id,side""",
            (shape_version, int(min_prior_games)),
        )]
        projection_rows = {
            (int(row["game_id"]), str(row["team"])): dict(row)
            for row in connection.execute(
                """SELECT game_id,team,season,projected_drives,projected_plays,
                          projected_dropbacks,projected_rush_attempts,
                          projected_points_per_drive,projected_offensive_points,
                          actual_drives,actual_plays,actual_dropbacks,actual_rush_attempts,
                          actual_points_per_drive,actual_offensive_points
                   FROM cfb_projection_backtest
                   WHERE backtest_version=?""",
                (backtest_version,),
            )
        }

    train = [row for row in shape_rows if int(row["season"]) < int(test_season)]
    test = [row for row in shape_rows if int(row["season"]) == int(test_season)]
    complete_train = [row for row in train if all(row.get(key) is not None for key in FEATURES)]
    scaler = _fit_scaler(complete_train)
    train_vectors = [_vector(row, scaler) for row in complete_train]
    train_vectors = [vec for vec in train_vectors if vec is not None]
    centroids = _kmeans(train_vectors, k=int(clusters))

    train_lookup = {(int(row["game_id"]), str(row["team"])): row for row in train}
    test_lookup = {(int(row["game_id"]), str(row["team"])): row for row in test}

    ppd_own: dict[int, list[float]] = defaultdict(list)
    ppd_pair: dict[tuple[int, int], list[float]] = defaultdict(list)
    pass_own: dict[int, list[float]] = defaultdict(list)
    pass_pair: dict[tuple[int, int], list[float]] = defaultdict(list)

    for row in train:
        vec = _vector(row, scaler)
        opponent = train_lookup.get((int(row["game_id"]), str(row["opponent"])))
        opp_vec = _vector(opponent, scaler) if opponent else None
        if vec is None or opp_vec is None:
            continue
        own = _cluster(vec, centroids)
        opp = _cluster(opp_vec, centroids)
        if row.get("actual_points_per_drive") is not None:
            value = float(row["actual_points_per_drive"])
            ppd_own[own].append(value)
            ppd_pair[(own, opp)].append(value)
        if row.get("actual_pass_rate") is not None:
            value = float(row["actual_pass_rate"])
            pass_own[own].append(value)
            pass_pair[(own, opp)].append(value)

    baseline_ppd = []
    adjusted_ppd = []
    baseline_points = []
    adjusted_points = []
    baseline_pass_rate = []
    adjusted_pass_rate = []
    baseline_dropbacks = []
    adjusted_dropbacks = []
    baseline_rushes = []
    adjusted_rushes = []
    applied_ppd = 0
    applied_pass = 0
    evaluated = 0

    for row in test:
        projection = projection_rows.get((int(row["game_id"]), str(row["team"])))
        opponent = test_lookup.get((int(row["game_id"]), str(row["opponent"])))
        vec = _vector(row, scaler)
        opp_vec = _vector(opponent, scaler) if opponent else None
        if not projection or vec is None or opp_vec is None:
            continue
        own = _cluster(vec, centroids)
        opp = _cluster(opp_vec, centroids)
        evaluated += 1

        projected_drives = projection.get("projected_drives")
        projected_plays = projection.get("projected_plays")
        projected_ppd = projection.get("projected_points_per_drive")
        actual_ppd_value = projection.get("actual_points_per_drive")
        actual_points_value = projection.get("actual_offensive_points")

        ppd_effect = 0.0
        pair_values = ppd_pair.get((own, opp), [])
        own_values = ppd_own.get(own, [])
        if len(pair_values) >= int(min_pair_rows) and own_values:
            ppd_effect = (
                sum(pair_values) / len(pair_values)
                - sum(own_values) / len(own_values)
            )
            applied_ppd += 1

        if projected_ppd is not None and actual_ppd_value is not None:
            base = float(projected_ppd)
            adj = max(0.0, base + ppd_effect)
            baseline_ppd.append((base, float(actual_ppd_value)))
            adjusted_ppd.append((adj, float(actual_ppd_value)))
            if projected_drives is not None and actual_points_value is not None:
                baseline_points.append(
                    (float(projected_drives) * base, float(actual_points_value)))
                adjusted_points.append(
                    (float(projected_drives) * adj, float(actual_points_value)))

        if projected_plays not in (None, 0) and projection.get("projected_dropbacks") is not None:
            base_pass = float(projection["projected_dropbacks"]) / float(projected_plays)
            pass_effect = 0.0
            pair_values = pass_pair.get((own, opp), [])
            own_values = pass_own.get(own, [])
            if len(pair_values) >= int(min_pair_rows) and own_values:
                pass_effect = (
                    sum(pair_values) / len(pair_values)
                    - sum(own_values) / len(own_values)
                )
                applied_pass += 1
            adj_pass = min(0.85, max(0.15, base_pass + pass_effect))
            if row.get("actual_pass_rate") is not None:
                actual_rate = float(row["actual_pass_rate"])
                baseline_pass_rate.append((base_pass, actual_rate))
                adjusted_pass_rate.append((adj_pass, actual_rate))
            actual_dropbacks = projection.get("actual_dropbacks")
            actual_rushes = projection.get("actual_rush_attempts")
            if actual_dropbacks is not None:
                baseline_dropbacks.append(
                    (float(projected_plays) * base_pass, float(actual_dropbacks)))
                adjusted_dropbacks.append(
                    (float(projected_plays) * adj_pass, float(actual_dropbacks)))
            if actual_rushes is not None:
                baseline_rushes.append(
                    (float(projected_plays) * (1.0 - base_pass), float(actual_rushes)))
                adjusted_rushes.append(
                    (float(projected_plays) * (1.0 - adj_pass), float(actual_rushes)))

    def comparison(base_pairs, adjusted_pairs):
        base = _metrics(base_pairs)
        adjusted = _metrics(adjusted_pairs)
        return {
            "production_baseline": base,
            "shape_adjusted": adjusted,
            "mae_delta_vs_production": (
                round(adjusted["mae"] - base["mae"], 4)
                if adjusted["mae"] is not None and base["mae"] is not None else None
            ),
            "rmse_delta_vs_production": (
                round(adjusted["rmse"] - base["rmse"], 4)
                if adjusted["rmse"] is not None and base["rmse"] is not None else None
            ),
        }

    return {
        "test_season": int(test_season),
        "train_seasons": (
            [min(int(row["season"]) for row in train), max(int(row["season"]) for row in train)]
            if train else None
        ),
        "clusters": int(clusters),
        "min_pair_rows": int(min_pair_rows),
        "evaluated_rows": evaluated,
        "ppd_interaction_applied_rows": applied_ppd,
        "pass_rate_interaction_applied_rows": applied_pass,
        "volume_source": (
            "Validated production historical projections from cfb_projection_backtest: "
            "projected_drives and projected_plays are never replaced by shape similarity."
        ),
        "points_per_drive": comparison(baseline_ppd, adjusted_ppd),
        "offensive_points": comparison(baseline_points, adjusted_points),
        "pass_rate": comparison(baseline_pass_rate, adjusted_pass_rate),
        "dropbacks": comparison(baseline_dropbacks, adjusted_dropbacks),
        "rush_attempts": comparison(baseline_rushes, adjusted_rushes),
        "notes": [
            "Shape interaction is an additive residual relative to the team's archetype mean.",
            "PPD effects are trained only on seasons before the held-out test season.",
            "Pass-rate effects change mix only; projected play volume remains fixed.",
            "QB is intentionally excluded from this ablation.",
        ],
    }



def _residual_adjustment(raw_effect: float, n: int, *, shrink_k: float,
                         threshold: float) -> float:
    if abs(float(raw_effect)) < float(threshold):
        return 0.0
    weight = float(n) / (float(n) + float(shrink_k)) if shrink_k > 0 else 1.0
    return float(raw_effect) * weight


def residual_chain_ablation(repository: CFBRepository, *, test_season: int,
                            shape_version: str = SHAPE_VERSION, clusters: int = 8,
                            min_prior_games: int = 3, min_pair_rows: int = 25,
                            backtest_version: str = "game-projection-backtest-v1") -> dict[str, Any]:
    """Walk-forward residualized shape interaction on top of production PPD."""
    initialize(repository)
    validation_season = int(test_season) - 1
    with repository._reader() as connection:
        shape_rows = [dict(row) for row in connection.execute(
            """SELECT * FROM cfb_team_shape_backtest
               WHERE shape_version=? AND prior_games>=?
               ORDER BY season,week,kickoff,game_id,side""",
            (shape_version, int(min_prior_games)),
        )]
        projection_rows = {
            (int(row["game_id"]), str(row["team"])): dict(row)
            for row in connection.execute(
                """SELECT game_id,team,season,projected_drives,
                          projected_points_per_drive,projected_offensive_points,
                          actual_points_per_drive,actual_offensive_points
                   FROM cfb_projection_backtest
                   WHERE backtest_version=?""",
                (backtest_version,),
            )
        }

    def fit_model(train_rows: list[dict[str, Any]]) -> dict[str, Any]:
        complete = [row for row in train_rows if all(row.get(key) is not None for key in FEATURES)]
        scaler = _fit_scaler(complete)
        vectors = [_vector(row, scaler) for row in complete]
        vectors = [vector for vector in vectors if vector is not None]
        centroids = _kmeans(vectors, k=int(clusters))
        lookup = {(int(row["game_id"]), str(row["team"])): row for row in train_rows}
        own_residuals: dict[int, list[float]] = defaultdict(list)
        pair_residuals: dict[tuple[int, int], list[float]] = defaultdict(list)
        for row in train_rows:
            projection = projection_rows.get((int(row["game_id"]), str(row["team"])))
            opponent = lookup.get((int(row["game_id"]), str(row["opponent"])))
            vec = _vector(row, scaler)
            opp_vec = _vector(opponent, scaler) if opponent else None
            if not projection or vec is None or opp_vec is None:
                continue
            projected = projection.get("projected_points_per_drive")
            actual = projection.get("actual_points_per_drive")
            if projected is None or actual is None:
                continue
            own = _cluster(vec, centroids)
            opp = _cluster(opp_vec, centroids)
            residual = float(actual) - float(projected)
            own_residuals[own].append(residual)
            pair_residuals[(own, opp)].append(residual)
        cells = {}
        for key, values in pair_residuals.items():
            own_values = own_residuals.get(key[0], [])
            if len(values) < int(min_pair_rows) or not own_values:
                continue
            pair_mean = sum(values) / len(values)
            own_mean = sum(own_values) / len(own_values)
            cells[key] = {
                "n": len(values),
                "pair_residual_mean": pair_mean,
                "own_shape_residual_mean": own_mean,
                "interaction_residual": pair_mean - own_mean,
            }
        return {"scaler": scaler, "centroids": centroids, "cells": cells}

    def evaluate(rows: list[dict[str, Any]], model: dict[str, Any],
                 *, shrink_k: float, threshold: float) -> dict[str, Any]:
        lookup = {(int(row["game_id"]), str(row["team"])): row for row in rows}
        base_ppd, adjusted_ppd = [], []
        base_points, adjusted_points = [], []
        applied = 0
        effects = []
        for row in rows:
            projection = projection_rows.get((int(row["game_id"]), str(row["team"])))
            opponent = lookup.get((int(row["game_id"]), str(row["opponent"])))
            vec = _vector(row, model["scaler"])
            opp_vec = _vector(opponent, model["scaler"]) if opponent else None
            if not projection or vec is None or opp_vec is None:
                continue
            projected_ppd = projection.get("projected_points_per_drive")
            actual_ppd = projection.get("actual_points_per_drive")
            if projected_ppd is None or actual_ppd is None:
                continue
            own = _cluster(vec, model["centroids"])
            opp = _cluster(opp_vec, model["centroids"])
            cell = model["cells"].get((own, opp))
            adjustment = 0.0
            if cell:
                adjustment = _residual_adjustment(
                    cell["interaction_residual"], cell["n"],
                    shrink_k=float(shrink_k), threshold=float(threshold))
                if adjustment != 0.0:
                    applied += 1
                    effects.append(adjustment)
            base = float(projected_ppd)
            adj = max(0.0, base + adjustment)
            actual = float(actual_ppd)
            base_ppd.append((base, actual))
            adjusted_ppd.append((adj, actual))

            actual_points = projection.get("actual_offensive_points")
            projected_points = projection.get("projected_offensive_points")
            drives = projection.get("projected_drives")
            if actual_points is not None and projected_points is not None and drives is not None:
                base_points.append((float(projected_points), float(actual_points)))
                adjusted_points.append((
                    float(projected_points) + float(drives) * adjustment,
                    float(actual_points),
                ))
        ppd_base = _metrics(base_ppd)
        ppd_adj = _metrics(adjusted_ppd)
        points_base = _metrics(base_points)
        points_adj = _metrics(adjusted_points)
        return {
            "rows": ppd_base["n"],
            "adjusted_rows": applied,
            "adjusted_rate": round(applied / ppd_base["n"], 4) if ppd_base["n"] else 0.0,
            "mean_applied_adjustment": (
                round(sum(effects) / len(effects), 4) if effects else 0.0
            ),
            "points_per_drive": {
                "production_baseline": ppd_base,
                "residual_shape_adjusted": ppd_adj,
                "mae_delta_vs_production": round(ppd_adj["mae"] - ppd_base["mae"], 4),
                "rmse_delta_vs_production": round(ppd_adj["rmse"] - ppd_base["rmse"], 4),
            },
            "offensive_points": {
                "production_baseline": points_base,
                "residual_shape_adjusted": points_adj,
                "mae_delta_vs_production": round(points_adj["mae"] - points_base["mae"], 4),
                "rmse_delta_vs_production": round(points_adj["rmse"] - points_base["rmse"], 4),
            },
        }

    policy_train = [row for row in shape_rows if int(row["season"]) < validation_season]
    validation = [row for row in shape_rows if int(row["season"]) == validation_season]
    policy_model = fit_model(policy_train)
    candidates = []
    for threshold in (0.0, 0.05, 0.10, 0.15):
        for shrink_k in (0.0, 25.0, 50.0, 100.0):
            result = evaluate(
                validation, policy_model, shrink_k=shrink_k, threshold=threshold)
            candidates.append({
                "threshold": threshold,
                "shrink_k": shrink_k,
                "validation": result,
            })
    selected = min(
        candidates,
        key=lambda item: (
            item["validation"]["points_per_drive"]["residual_shape_adjusted"]["mae"],
            item["validation"]["points_per_drive"]["residual_shape_adjusted"]["rmse"],
        ),
    )

    final_train = [row for row in shape_rows if int(row["season"]) < int(test_season)]
    test = [row for row in shape_rows if int(row["season"]) == int(test_season)]
    final_model = fit_model(final_train)
    test_result = evaluate(
        test, final_model,
        shrink_k=float(selected["shrink_k"]),
        threshold=float(selected["threshold"]),
    )
    strongest = sorted(
        (
            {
                "own_shape": own,
                "opponent_shape": opp,
                "training_rows": cell["n"],
                "pair_residual_mean": round(cell["pair_residual_mean"], 4),
                "own_shape_residual_mean": round(cell["own_shape_residual_mean"], 4),
                "interaction_residual": round(cell["interaction_residual"], 4),
                "shrunk_adjustment": round(_residual_adjustment(
                    cell["interaction_residual"], cell["n"],
                    shrink_k=float(selected["shrink_k"]),
                    threshold=float(selected["threshold"])), 4),
            }
            for (own, opp), cell in final_model["cells"].items()
        ),
        key=lambda item: abs(item["shrunk_adjustment"]),
        reverse=True,
    )[:15]

    return {
        "test_season": int(test_season),
        "validation_season": validation_season,
        "clusters": int(clusters),
        "min_pair_rows": int(min_pair_rows),
        "policy_fit_seasons": (
            [min(int(row["season"]) for row in policy_train),
             max(int(row["season"]) for row in policy_train)]
            if policy_train else None
        ),
        "final_fit_seasons": (
            [min(int(row["season"]) for row in final_train),
             max(int(row["season"]) for row in final_train)]
            if final_train else None
        ),
        "selected_policy": {
            "threshold": selected["threshold"],
            "shrink_k": selected["shrink_k"],
            "validation": selected["validation"],
        },
        "candidate_count": len(candidates),
        "test": test_result,
        "strongest_final_residual_interactions": strongest,
        "volume_source": (
            "Production xDrives/projected_drives is fixed. Shape only attempts "
            "to explain residual PPD error left after the production model."
        ),
        "notes": [
            "Residual target is actual PPD minus production projected PPD.",
            "Interaction residual subtracts the own-shape mean residual before adjustment.",
            "Threshold/shrinkage policy is selected on the prior validation season, not on the test season.",
            "Final residual cells are refit through the season before the held-out test.",
            "QB remains excluded.",
        ],
    }


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
