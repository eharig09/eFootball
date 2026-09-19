"""Walk-forward backtest for the production CFB matchup projection.

The central rule is simple: historical evaluation must call the same
`game_projection.project_matchup()` adapter the live site calls, with the
historical game's own kickoff as `as_of_date`. That keeps feature construction,
fallbacks and fitted-model use identical between backtest and production.

Rows are stored one team-game at a time so the report layer can evaluate both
team outputs (points, yards, drives, red-zone opportunities) and game outputs
(total and margin). Expected points are offense-only by construction; both
offensive points and final scoreboard points are retained so that distinction
is measurable instead of hidden.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import math
from typing import Any, Iterable

from sports_aggregator.cfb.game_projection import project_matchup
from sports_aggregator.cfb.repository import CFBRepository, schema_once
from sports_aggregator.cfb.team_game_drive_outcomes import (
    METRIC_VERSION as OUTCOME_VERSION,
    initialize as initialize_outcomes,
)
from sports_aggregator.cfb.team_game_pace import (
    METRIC_VERSION as PACE_VERSION,
    initialize as initialize_pace,
)
from sports_aggregator.cfb.team_game_scoring import (
    METRIC_VERSION as SCORING_VERSION,
    initialize as initialize_scoring,
)

BACKTEST_VERSION = "game-projection-backtest-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfb_projection_backtest (
  game_id INTEGER NOT NULL,
  team TEXT NOT NULL,
  opponent TEXT NOT NULL,
  side TEXT NOT NULL,
  backtest_version TEXT NOT NULL,
  season INTEGER NOT NULL,
  week INTEGER,
  kickoff TEXT NOT NULL,

  projection_model_version TEXT,
  prior_games INTEGER NOT NULL,
  quality_edge REAL,
  quality_sources INTEGER,

  projected_drives REAL,
  projected_plays REAL,
  projected_dropbacks REAL,
  projected_rush_attempts REAL,
  projected_pass_yards REAL,
  projected_rush_yards REAL,
  projected_total_yards REAL,
  projected_points_per_drive REAL,
  projected_offensive_points REAL,
  projected_giveaways REAL,
  projected_red_zone_trips REAL,
  projected_red_zone_touchdowns REAL,

  actual_drives REAL,
  actual_plays REAL,
  actual_dropbacks REAL,
  actual_rush_attempts REAL,
  actual_pass_yards REAL,
  actual_rush_yards REAL,
  actual_total_yards REAL,
  actual_points_per_drive REAL,
  actual_offensive_points REAL,
  actual_score_points REAL,
  actual_giveaways REAL,
  actual_red_zone_trips REAL,
  actual_red_zone_touchdowns REAL,

  market_spread REAL,
  market_total REAL,
  market_implied_points REAL,

  built_at TEXT NOT NULL,
  PRIMARY KEY(game_id, team, backtest_version),
  FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cfb_projection_backtest_season
  ON cfb_projection_backtest(backtest_version, season, week);
"""


@schema_once("projection_backtest")
def initialize(repository: CFBRepository) -> None:
    repository.initialize()
    initialize_pace(repository)
    initialize_scoring(repository)
    initialize_outcomes(repository)
    with closing(repository._connect()) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


def _float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _market_by_game(repository: CFBRepository, game_ids: Iterable[int]) -> dict[int, dict[str, float | None]]:
    ids = sorted({int(game_id) for game_id in game_ids})
    if not ids:
        return {}
    with repository._reader() as connection:
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "game_lines" not in tables:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""SELECT game_id,AVG(spread) AS spread,AVG(over_under) AS total
                FROM game_lines WHERE game_id IN ({placeholders})
                GROUP BY game_id""", ids).fetchall()
    return {
        int(row["game_id"]): {
            "spread": _float(row["spread"]),
            "total": _float(row["total"]),
        }
        for row in rows
    }


def _temporal_points_model(repository: CFBRepository, *, train_from: int,
                           train_to: int, min_prior_games: int = 3) -> dict[str, Any] | None:
    """Fit an in-memory xPoints model without mutating the production model table."""
    if train_to < train_from:
        return None
    from sports_aggregator.cfb import xpoints
    xpoints.initialize(repository)
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT * FROM cfb_xpoints_dataset
               WHERE dataset_version=? AND season BETWEEN ? AND ?""",
            (xpoints.DATASET_VERSION, int(train_from), int(train_to)),
        )]
    eligible = [
        row for row in rows
        if int(row["team_prior_games"] or 0) >= int(min_prior_games)
        and all(row.get(key) is not None for key in xpoints.ADVANCED_FEATURES)
    ]
    if not eligible:
        return None
    coefficients, means, scales = xpoints._fit_standardized(
        eligible, xpoints.ADVANCED_FEATURES, l2=2.0)
    if coefficients is None:
        return None
    return {
        "model_version": f"backtest-xpoints-{train_from}-{train_to}",
        "features": tuple(xpoints.ADVANCED_FEATURES),
        "coefficients": coefficients,
        "means": means,
        "scales": scales,
        "training_rows": len(eligible),
        "from_season": int(train_from),
        "to_season": int(train_to),
    }


def _actuals(repository: CFBRepository, game_ids: Iterable[int]) -> dict[tuple[int, str], dict[str, Any]]:
    ids = sorted({int(game_id) for game_id in game_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with repository._reader() as connection:
        pace = {
            (int(row["game_id"]), str(row["team"])): dict(row)
            for row in connection.execute(
                f"""SELECT game_id,team,meaningful_drives,scrimmage_plays,pass_plays,rush_plays,
                           pass_yards,rush_yards
                    FROM cfb_team_game_pace
                    WHERE metric_version=? AND game_id IN ({placeholders})""",
                (PACE_VERSION, *ids))
        }
        scoring = {
            (int(row["game_id"]), str(row["team"])): dict(row)
            for row in connection.execute(
                f"""SELECT game_id,team,giveaways,red_zone_trips,red_zone_touchdowns
                    FROM cfb_team_game_scoring
                    WHERE metric_version=? AND game_id IN ({placeholders})""",
                (SCORING_VERSION, *ids))
        }
        outcomes = {
            (int(row["game_id"]), str(row["team"])): dict(row)
            for row in connection.execute(
                f"""SELECT game_id,team,offensive_points,points_per_drive
                    FROM cfb_team_game_drive_outcomes
                    WHERE metric_version=? AND game_id IN ({placeholders})""",
                (OUTCOME_VERSION, *ids))
        }
    output: dict[tuple[int, str], dict[str, Any]] = {}
    for key in set(pace) | set(scoring) | set(outcomes):
        p = pace.get(key, {})
        s = scoring.get(key, {})
        o = outcomes.get(key, {})
        pass_yards = p.get("pass_yards")
        rush_yards = p.get("rush_yards")
        output[key] = {
            "drives": p.get("meaningful_drives"),
            "plays": p.get("scrimmage_plays"),
            "dropbacks": p.get("pass_plays"),
            "rush_attempts": p.get("rush_plays"),
            "pass_yards": pass_yards,
            "rush_yards": rush_yards,
            "total_yards": (
                float(pass_yards) + float(rush_yards)
                if pass_yards is not None and rush_yards is not None else None
            ),
            "points_per_drive": o.get("points_per_drive"),
            "offensive_points": o.get("offensive_points"),
            "giveaways": s.get("giveaways"),
            "red_zone_trips": s.get("red_zone_trips"),
            "red_zone_touchdowns": s.get("red_zone_touchdowns"),
        }
    return output


def build(repository: CFBRepository, *, from_season: int, to_season: int,
          backtest_version: str = BACKTEST_VERSION,
          min_prior_games: int = 1,
          points_train_from_season: int = 2022) -> dict[str, Any]:
    """Rebuild walk-forward predictions for completed games in a season range."""
    if from_season > to_season:
        raise ValueError("from_season must not be after to_season")
    initialize(repository)

    with repository._reader() as connection:
        games = [dict(row) for row in connection.execute(
            """SELECT game_id,season,week,start_date,home_team,away_team,
                      home_points,away_points
               FROM games
               WHERE completed=1 AND season BETWEEN ? AND ?
                 AND start_date IS NOT NULL
                 AND home_points IS NOT NULL AND away_points IS NOT NULL
               ORDER BY start_date,game_id""",
            (int(from_season), int(to_season))
        )]

    actuals = _actuals(repository, (game["game_id"] for game in games))
    markets = _market_by_game(repository, (game["game_id"] for game in games))
    # One frozen model per test season. A 2025 game, for example, may use a
    # model trained through 2024 but never the persisted 2022-25 production
    # model. The underlying xPoints rows are themselves strictly pregame.
    temporal_models = {
        season: _temporal_points_model(
            repository,
            train_from=int(points_train_from_season),
            train_to=int(season) - 1,
        )
        for season in sorted({int(game["season"]) for game in games})
    }
    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple[Any, ...]] = []
    games_projected = 0
    games_skipped = 0

    for game in games:
        projection = project_matchup(
            repository,
            str(game["home_team"]),
            str(game["away_team"]),
            as_of_date=str(game["start_date"]),
            game_id=int(game["game_id"]),
            points_model_override=temporal_models.get(int(game["season"])),
        )
        market = markets.get(int(game["game_id"]), {})
        spread = market.get("spread")
        total = market.get("total")
        model_version = (
            (projection.get("points_model") or {}).get("model_version")
            or "matchup-fallback"
        )
        game_rows_before = len(rows)
        for side in ("away", "home"):
            team = str(game[f"{side}_team"])
            other = "home" if side == "away" else "away"
            opponent = str(game[f"{other}_team"])
            predicted = projection[side]
            snapshot = projection[f"{side}_snapshot"]
            prior_games = int(snapshot.get("games") or 0)
            if prior_games < min_prior_games:
                continue

            actual = actuals.get((int(game["game_id"]), team), {})
            score_points = (
                game["home_points"] if side == "home" else game["away_points"]
            )
            team_spread = spread if side == "home" else (-spread if spread is not None else None)
            implied = (
                total / 2.0 - team_spread / 2.0
                if total is not None and team_spread is not None else None
            )
            rows.append((
                int(game["game_id"]), team, opponent, side, backtest_version,
                int(game["season"]), game.get("week"), str(game["start_date"]),
                str(model_version), prior_games,
                predicted.get("opponent_quality_edge"),
                predicted.get("opponent_quality_sources"),
                predicted.get("drives"), predicted.get("plays"),
                predicted.get("dropbacks"), predicted.get("rush_attempts"),
                predicted.get("pass_yards"), predicted.get("rush_yards"),
                predicted.get("total_yards"), predicted.get("points_per_drive"),
                predicted.get("expected_points"), predicted.get("giveaways"),
                predicted.get("red_zone_trips"), predicted.get("red_zone_touchdowns"),
                actual.get("drives"), actual.get("plays"),
                actual.get("dropbacks"), actual.get("rush_attempts"),
                actual.get("pass_yards"), actual.get("rush_yards"),
                actual.get("total_yards"), actual.get("points_per_drive"),
                actual.get("offensive_points"), score_points,
                actual.get("giveaways"), actual.get("red_zone_trips"),
                actual.get("red_zone_touchdowns"),
                spread, total, implied, now,
            ))
        if len(rows) > game_rows_before:
            games_projected += 1
        else:
            games_skipped += 1

    with repository.transaction() as connection:
        connection.execute(
            """DELETE FROM cfb_projection_backtest
               WHERE backtest_version=? AND season BETWEEN ? AND ?""",
            (backtest_version, int(from_season), int(to_season)),
        )
        connection.executemany(
            """INSERT INTO cfb_projection_backtest VALUES(
               ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    return {
        "backtest_version": backtest_version,
        "from_season": int(from_season),
        "to_season": int(to_season),
        "completed_games": len(games),
        "games_projected": games_projected,
        "games_skipped_no_history": games_skipped,
        "team_game_rows": len(rows),
        "points_train_from_season": int(points_train_from_season),
        "temporal_points_models": {
            str(season): (
                {
                    "model_version": model["model_version"],
                    "training_rows": model["training_rows"],
                    "from_season": model["from_season"],
                    "to_season": model["to_season"],
                }
                if model else None
            )
            for season, model in temporal_models.items()
        },
    }


def _metrics(pairs: Iterable[tuple[float | None, float | None]]) -> dict[str, Any]:
    values = [(float(p), float(a)) for p, a in pairs if p is not None and a is not None]
    if not values:
        return {"n": 0, "mae": None, "rmse": None, "bias": None}
    errors = [predicted - actual for predicted, actual in values]
    return {
        "n": len(values),
        "mae": round(sum(abs(error) for error in errors) / len(errors), 4),
        "rmse": round(math.sqrt(sum(error * error for error in errors) / len(errors)), 4),
        "bias": round(sum(errors) / len(errors), 4),
    }


def _bucket_prior_games(games: int) -> str:
    if games <= 2:
        return "1-2"
    if games <= 4:
        return "3-4"
    if games <= 7:
        return "5-7"
    if games <= 11:
        return "8-11"
    return "12+"


def _bucket_week(week: int | None) -> str:
    if week is None:
        return "unknown"
    if week <= 3:
        return "0-3"
    if week <= 6:
        return "4-6"
    if week <= 10:
        return "7-10"
    return "11+"


def _bucket_quality(edge: float | None) -> str:
    if edge is None:
        return "missing"
    magnitude = abs(float(edge))
    if magnitude < 3:
        return "0-3"
    if magnitude < 7:
        return "3-7"
    return "7+"


_TEAM_METRICS = {
    "offensive_points": ("projected_offensive_points", "actual_offensive_points"),
    "score_points": ("projected_offensive_points", "actual_score_points"),
    "total_yards": ("projected_total_yards", "actual_total_yards"),
    "pass_yards": ("projected_pass_yards", "actual_pass_yards"),
    "rush_yards": ("projected_rush_yards", "actual_rush_yards"),
    "drives": ("projected_drives", "actual_drives"),
    "plays": ("projected_plays", "actual_plays"),
    "dropbacks": ("projected_dropbacks", "actual_dropbacks"),
    "rush_attempts": ("projected_rush_attempts", "actual_rush_attempts"),
    "points_per_drive": ("projected_points_per_drive", "actual_points_per_drive"),
    "giveaways": ("projected_giveaways", "actual_giveaways"),
    "red_zone_trips": ("projected_red_zone_trips", "actual_red_zone_trips"),
    "red_zone_touchdowns": ("projected_red_zone_touchdowns", "actual_red_zone_touchdowns"),
}


def _score_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        name: _metrics((row[predicted], row[actual]) for row in rows)
        for name, (predicted, actual) in _TEAM_METRICS.items()
    }


def _group_scores(rows: list[dict[str, Any]], key_fn) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(key_fn(row))].append(row)
    return {
        key: {"rows": len(group), **_score_rows(group)}
        for key, group in sorted(grouped.items())
    }


def _game_scores(rows: list[dict[str, Any]]) -> dict[str, Any]:
    games: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        games[int(row["game_id"])][str(row["side"])] = row

    total_pairs = []
    margin_pairs = []
    offensive_total_pairs = []
    market_total_pairs = []
    market_margin_pairs = []
    for sides in games.values():
        home = sides.get("home")
        away = sides.get("away")
        if not home or not away:
            continue
        hp, ap = home["projected_offensive_points"], away["projected_offensive_points"]
        hs, ass = home["actual_score_points"], away["actual_score_points"]
        ho, ao = home["actual_offensive_points"], away["actual_offensive_points"]
        if None not in (hp, ap, hs, ass):
            total_pairs.append((float(hp) + float(ap), float(hs) + float(ass)))
            margin_pairs.append((float(hp) - float(ap), float(hs) - float(ass)))
        if None not in (hp, ap, ho, ao):
            offensive_total_pairs.append((float(hp) + float(ap), float(ho) + float(ao)))

        market_total = home.get("market_total")
        spread = home.get("market_spread")
        if market_total is not None and hs is not None and ass is not None:
            market_total_pairs.append((float(market_total), float(hs) + float(ass)))
        if spread is not None and hs is not None and ass is not None:
            # Stored spread is home-relative; negative means home favored.
            market_margin_pairs.append((-float(spread), float(hs) - float(ass)))

    return {
        "games_with_two_sides": sum(1 for sides in games.values() if "home" in sides and "away" in sides),
        "projected_score_total": _metrics(total_pairs),
        "projected_score_margin": _metrics(margin_pairs),
        "projected_offensive_total": _metrics(offensive_total_pairs),
        "market_total": _metrics(market_total_pairs),
        "market_margin": _metrics(market_margin_pairs),
    }


def report(repository: CFBRepository, *, from_season: int | None = None,
           to_season: int | None = None,
           backtest_version: str = BACKTEST_VERSION,
           min_prior_games: int = 1) -> dict[str, Any]:
    initialize(repository)
    clauses = ["backtest_version=?", "prior_games>=?"]
    params: list[Any] = [backtest_version, int(min_prior_games)]
    if from_season is not None:
        clauses.append("season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("season<=?"); params.append(int(to_season))
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            f"""SELECT * FROM cfb_projection_backtest
                WHERE {' AND '.join(clauses)}
                ORDER BY season,week,kickoff,game_id,side""", params)]

    market_points = _metrics(
        (row["market_implied_points"], row["actual_score_points"]) for row in rows
    )
    by_season = _group_scores(rows, lambda row: row["season"])
    by_week_band = _group_scores(rows, lambda row: _bucket_week(row.get("week")))
    by_prior_games = _group_scores(rows, lambda row: _bucket_prior_games(int(row["prior_games"])))
    by_quality_edge = _group_scores(rows, lambda row: _bucket_quality(row.get("quality_edge")))
    by_quality_sources = _group_scores(rows, lambda row: row.get("quality_sources") or 0)

    return {
        "backtest_version": backtest_version,
        "from_season": from_season,
        "to_season": to_season,
        "min_prior_games": int(min_prior_games),
        "team_game_rows": len(rows),
        "overall": _score_rows(rows),
        "game_level": _game_scores(rows),
        "market_implied_points": market_points,
        "by_season": by_season,
        "by_week_band": by_week_band,
        "by_prior_games": by_prior_games,
        "by_quality_edge": by_quality_edge,
        "by_quality_sources": by_quality_sources,
        "notes": [
            "Every prediction is generated by production game_projection.project_matchup() at the historical kickoff timestamp.",
            "projected_offensive_points is compared both with drive-derived offensive points (model fidelity) and final scoreboard points (user-facing diagnostic).",
            "Market benchmarks use stored game_lines consensus values and are descriptive comparisons, not model inputs reconstructed from postgame results.",
        ],
    }
