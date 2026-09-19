"""Leak-safe drive outcome and points-per-drive projections (Milestone 11).

The opponent adjustment is residual based: an offense is credited for scoring
above what its prior opponents normally allowed, and a defense is charged for
allowing more than its prior opponents normally scored.  Recent score margin
is kept separately so short-term form can be tested without conflating it with
schedule strength.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
import math
import json
from typing import Any

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.team_game_drive_outcomes import (
    METRIC_VERSION as OUTCOME_VERSION,
    initialize as initialize_outcomes,
)
from sports_aggregator.cfb.xdrives import RECENCY_LAMBDA, TRAILING_WINDOW_GAMES

DATASET_VERSION = "xpoints-dataset-v1"
MODEL_VERSION = "xpoints-ridge-v1"
ADVANCED_FEATURES = (
    "team_prior_points_per_drive", "opponent_prior_points_per_drive_allowed",
    "team_opponent_adjusted_residual", "opponent_defense_adjusted_residual",
    "team_recent_margin", "opponent_recent_margin",
    "opponent_quality_blend",
)


@schema_once("xpoints")
def initialize(repository) -> None:
    initialize_outcomes(repository)
    from sports_aggregator.cfb.lines import initialize as initialize_lines
    initialize_lines(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_xpoints_dataset (
          game_id INTEGER NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
          season INTEGER NOT NULL, week INTEGER, home_away TEXT NOT NULL,
          dataset_version TEXT NOT NULL,
          actual_drives INTEGER NOT NULL, actual_touchdown_rate REAL NOT NULL,
          actual_field_goal_rate REAL NOT NULL, actual_turnover_rate REAL NOT NULL,
          actual_punt_rate REAL NOT NULL, actual_points_per_drive REAL NOT NULL,
          team_prior_games INTEGER NOT NULL, team_prior_points_per_drive REAL,
          team_prior_touchdown_rate REAL, team_prior_field_goal_rate REAL,
          opponent_prior_games INTEGER NOT NULL,
          opponent_prior_points_per_drive_allowed REAL,
          opponent_prior_touchdown_rate_allowed REAL,
          opponent_prior_field_goal_rate_allowed REAL,
          league_prior_points_per_drive REAL,
          league_prior_touchdown_rate REAL, league_prior_field_goal_rate REAL,
          team_opponent_adjusted_residual REAL,
          opponent_defense_adjusted_residual REAL,
          opponent_adjusted_points_per_drive REAL,
          team_recent_margin REAL, opponent_recent_margin REAL,
          team_elo REAL, opponent_elo REAL, elo_difference REAL,
          market_spread REAL, market_total REAL, market_implied_points REAL,
          fpi_margin REAL, core_margin REAL, vegas_margin REAL,
          opponent_quality_blend REAL, quality_source_count INTEGER NOT NULL DEFAULT 0,
          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,dataset_version)
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_xpoints_team
          ON cfb_xpoints_dataset(team,dataset_version,season,week);
        CREATE TABLE IF NOT EXISTS cfb_xpoints_model (
          model_version TEXT PRIMARY KEY, feature_json TEXT NOT NULL,
          coefficients_json TEXT NOT NULL, means_json TEXT NOT NULL,
          scales_json TEXT NOT NULL, l2 REAL NOT NULL,
          from_season INTEGER, to_season INTEGER,
          training_rows INTEGER NOT NULL, fitted_at TEXT NOT NULL
        );
        """)
        existing = {str(row[1]) for row in connection.execute(
            "PRAGMA table_info(cfb_xpoints_dataset)")}
        for column, declaration in (
            ("fpi_margin", "REAL"), ("core_margin", "REAL"),
            ("vegas_margin", "REAL"), ("opponent_quality_blend", "REAL"),
            ("quality_source_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE cfb_xpoints_dataset ADD COLUMN {column} {declaration}")
        connection.commit()


def _weights(count: int) -> list[float]:
    return [math.exp(-RECENCY_LAMBDA * (count - 1 - index)) for index in range(count)]


def _mean(rows, key: str) -> float | None:
    pairs = [(weight, row.get(key)) for weight, row in zip(_weights(len(rows)), rows)
             if row.get(key) is not None]
    denominator = sum(weight for weight, _ in pairs)
    return (sum(weight * float(value) for weight, value in pairs) / denominator
            if denominator else None)


def _ratio(rows, numerator: str, denominator: str) -> float | None:
    top = bottom = 0.0
    for weight, row in zip(_weights(len(rows)), rows):
        if row.get(numerator) is None or row.get(denominator) is None:
            continue
        top += weight * float(row[numerator]); bottom += weight * float(row[denominator])
    return top / bottom if bottom else None


def _league_rate(totals: dict[str, float], numerator: str) -> float | None:
    return totals[numerator] / totals["drives"] if totals["drives"] else None


def build_dataset(repository, *, from_season: int | None = None,
                  to_season: int | None = None,
                  dataset_version: str = DATASET_VERSION) -> dict[str, Any]:
    """Build chronological pregame features, updating histories after both game rows."""
    initialize(repository)
    clauses = ["o.metric_version=?"]
    params: list[Any] = [OUTCOME_VERSION]
    if from_season is not None:
        clauses.append("g.season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("g.season<=?"); params.append(int(to_season))
    with repository._reader() as connection:
        actuals = [dict(row) for row in connection.execute(f"""
          SELECT o.*,g.season,g.week,g.start_date,g.home_team,g.away_team,
                 g.home_team_id,g.away_team_id,
                 g.home_points,g.away_points,g.home_pregame_elo,g.away_pregame_elo
          FROM cfb_team_game_drive_outcomes o JOIN games g USING(game_id)
          WHERE {' AND '.join(clauses)} ORDER BY g.start_date,g.game_id,o.team
        """, params)]
        game_ids = sorted({int(row["game_id"]) for row in actuals})
        lines = {}
        if game_ids:
            marks = ",".join("?" for _ in game_ids)
            for row in connection.execute(f"""SELECT game_id,AVG(spread) spread,
                AVG(over_under) total FROM game_lines WHERE game_id IN ({marks}) GROUP BY game_id""",
                                          game_ids):
                lines[int(row["game_id"])] = dict(row)
        table_names = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        fpi = {}
        if "fpi_game_projections" in table_names and game_ids:
            marks = ",".join("?" for _ in game_ids)
            fpi = {(int(row["game_id"]), int(row["team_id"])): row["pred_point_diff"]
                   for row in connection.execute(f"""SELECT game_id,team_id,pred_point_diff
                     FROM fpi_game_projections WHERE game_id IN ({marks})""", game_ids)}
        core: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute("SELECT season,through_week,team,overall FROM core_ratings"):
            core[(int(row["season"]), str(row["team"]))].append(dict(row))

    own: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    allowed: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    offense_residual: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    defense_residual: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    margins: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    league = defaultdict(float)
    by_game: dict[int, list[dict[str, Any]]] = defaultdict(list)
    order = []
    for row in actuals:
        game_id = int(row["game_id"])
        if game_id not in by_game:
            order.append(game_id)
        by_game[game_id].append(row)

    now = datetime.now(timezone.utc).isoformat(); output = []
    for game_id in order:
        rows = by_game[game_id]
        pregame = {}
        for row in rows:
            team, opponent = str(row["team"]), str(row["opponent"])
            team_history, defense_history = own[team], allowed[opponent]
            league_ppd = _league_rate(league, "points")
            off_resid = _mean(offense_residual[team], "value")
            def_resid = _mean(defense_residual[opponent], "value")
            adjusted = (league_ppd + (off_resid or 0.0) + (def_resid or 0.0)
                        if league_ppd is not None else None)
            is_home = team == row["home_team"]
            team_elo = row["home_pregame_elo"] if is_home else row["away_pregame_elo"]
            opponent_elo = row["away_pregame_elo"] if is_home else row["home_pregame_elo"]
            line = lines.get(game_id, {}); spread = line.get("spread"); total = line.get("total")
            team_spread = (spread if is_home else -spread) if spread is not None else None
            implied = (total / 2 - team_spread / 2
                       if total is not None and team_spread is not None else None)
            team_id = row["home_team_id"] if is_home else row["away_team_id"]
            fpi_margin = fpi.get((game_id, int(team_id)))
            def prior_core(name: str) -> float | None:
                candidates = [item for item in core.get((int(row["season"]), name), [])
                              if int(item["through_week"]) < int(row["week"])]
                return (float(max(candidates, key=lambda item: int(item["through_week"]))["overall"])
                        if candidates else None)
            team_core, opponent_core = prior_core(team), prior_core(opponent)
            core_margin = (team_core - opponent_core
                           if team_core is not None and opponent_core is not None else None)
            vegas_margin = -float(team_spread) if team_spread is not None else None
            # Put all four lenses onto an approximate point-margin scale before
            # averaging. Elo's conventional CFB conversion is about 25 points
            # of rating per scoreboard point; FPI, CORE and Vegas are already
            # expressed on point-like scales.
            quality_components = [value for value in (
                ((float(team_elo) - float(opponent_elo)) / 25.0
                 if team_elo is not None and opponent_elo is not None else None),
                float(fpi_margin) if fpi_margin is not None else None,
                core_margin, vegas_margin,
            ) if value is not None]
            quality_blend = (sum(quality_components) / len(quality_components)
                             if quality_components else None)
            opponent_allowed_ppd = _ratio(defense_history, "offensive_points", "meaningful_drives")
            team_ppd = _ratio(team_history, "offensive_points", "meaningful_drives")
            pregame[team] = {"team_ppd": team_ppd, "opponent_allowed_ppd": opponent_allowed_ppd}
            output.append((
                game_id, team, opponent, row["season"], row["week"],
                "home" if is_home else "away", dataset_version,
                row["meaningful_drives"], row["touchdowns_per_drive"],
                row["field_goals_per_drive"], row["turnovers_per_drive"],
                row["punts_per_drive"], row["points_per_drive"],
                len(team_history), team_ppd,
                _ratio(team_history, "touchdowns", "meaningful_drives"),
                _ratio(team_history, "field_goals", "meaningful_drives"),
                len(defense_history), opponent_allowed_ppd,
                _ratio(defense_history, "touchdowns", "meaningful_drives"),
                _ratio(defense_history, "field_goals", "meaningful_drives"),
                league_ppd, _league_rate(league, "touchdowns"),
                _league_rate(league, "field_goals"), off_resid, def_resid,
                max(0.0, adjusted) if adjusted is not None else None,
                _mean(margins[team], "value"), _mean(margins[opponent], "value"),
                team_elo, opponent_elo,
                (float(team_elo) - float(opponent_elo)
                 if team_elo is not None and opponent_elo is not None else None),
                team_spread, total, implied, fpi_margin, core_margin, vegas_margin,
                quality_blend, len(quality_components), now,
            ))
        # Do not let one side of a game enter history before the other side is featured.
        for row in rows:
            team, opponent = str(row["team"]), str(row["opponent"])
            actual_ppd = float(row["points_per_drive"])
            reference_defense = pregame[team]["opponent_allowed_ppd"]
            reference_offense = pregame.get(opponent, {}).get("team_ppd")
            if reference_defense is not None:
                offense_residual[team].append({"value": actual_ppd - reference_defense})
            if reference_offense is not None:
                defense_residual[opponent].append({"value": actual_ppd - reference_offense})
            own[team].append(row); allowed[opponent].append(row)
            points_for = row["home_points"] if team == row["home_team"] else row["away_points"]
            points_against = row["away_points"] if team == row["home_team"] else row["home_points"]
            if points_for is not None and points_against is not None:
                margins[team].append({"value": float(points_for) - float(points_against)})
            league["drives"] += float(row["meaningful_drives"])
            league["points"] += float(row["offensive_points"])
            league["touchdowns"] += float(row["touchdowns"])
            league["field_goals"] += float(row["field_goals"])

    with closing(repository._connect()) as connection:
        if from_season is not None or to_season is not None:
            filters, values = [], []
            if from_season is not None:
                filters.append("season>=?"); values.append(int(from_season))
            if to_season is not None:
                filters.append("season<=?"); values.append(int(to_season))
            connection.execute(f"""DELETE FROM cfb_xpoints_dataset WHERE dataset_version=?
              AND game_id IN (SELECT game_id FROM games WHERE {' AND '.join(filters)})""",
                               (dataset_version, *values))
        else:
            connection.execute("DELETE FROM cfb_xpoints_dataset WHERE dataset_version=?",
                               (dataset_version,))
        columns = """game_id,team,opponent,season,week,home_away,dataset_version,
          actual_drives,actual_touchdown_rate,actual_field_goal_rate,actual_turnover_rate,
          actual_punt_rate,actual_points_per_drive,team_prior_games,
          team_prior_points_per_drive,team_prior_touchdown_rate,team_prior_field_goal_rate,
          opponent_prior_games,opponent_prior_points_per_drive_allowed,
          opponent_prior_touchdown_rate_allowed,opponent_prior_field_goal_rate_allowed,
          league_prior_points_per_drive,league_prior_touchdown_rate,
          league_prior_field_goal_rate,team_opponent_adjusted_residual,
          opponent_defense_adjusted_residual,opponent_adjusted_points_per_drive,
          team_recent_margin,opponent_recent_margin,team_elo,opponent_elo,elo_difference,
          market_spread,market_total,market_implied_points,fpi_margin,core_margin,
          vegas_margin,opponent_quality_blend,quality_source_count,built_at"""
        placeholders = ",".join("?" for _ in range(41))
        connection.executemany(
            f"INSERT INTO cfb_xpoints_dataset({columns}) VALUES({placeholders})", output)
        connection.commit()
    return {"dataset_version": dataset_version, "rows": len(output),
            "from_season": from_season, "to_season": to_season}


def _finish(pairs: list[tuple[float, float]]) -> dict[str, Any]:
    if not pairs:
        return {"games": 0, "mae": None, "rmse": None, "bias": None}
    errors = [predicted - actual for predicted, actual in pairs]
    return {"games": len(pairs),
            "mae": round(sum(abs(value) for value in errors) / len(errors), 5),
            "rmse": round(math.sqrt(sum(value * value for value in errors) / len(errors)), 5),
            "bias": round(sum(errors) / len(errors), 5)}


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector); augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        for row in range(col + 1, n):
            factor = augmented[row][col] / augmented[col][col]
            for index in range(col, n + 1):
                augmented[row][index] -= factor * augmented[col][index]
    answer = [0.0] * n
    for row in range(n - 1, -1, -1):
        answer[row] = (augmented[row][n] - sum(augmented[row][index] * answer[index]
                       for index in range(row + 1, n))) / augmented[row][row]
    return answer


def _fit_standardized(rows: list[dict[str, Any]], features: tuple[str, ...], l2: float = 2.0):
    means = {key: sum(float(row[key]) for row in rows) / len(rows) for key in features}
    scales = {key: math.sqrt(sum((float(row[key]) - means[key]) ** 2 for row in rows) / len(rows)) or 1.0
              for key in features}
    size = len(features) + 1; xtx = [[0.0] * size for _ in range(size)]; xty = [0.0] * size
    for row in rows:
        x = [1.0] + [(float(row[key]) - means[key]) / scales[key] for key in features]
        y = float(row["actual_points_per_drive"])
        for i in range(size):
            xty[i] += x[i] * y
            for j in range(size): xtx[i][j] += x[i] * x[j]
    for i in range(1, size): xtx[i][i] += l2
    return _solve(xtx, xty), means, scales


def fit_model(repository, *, from_season: int, to_season: int,
              dataset_version: str = DATASET_VERSION,
              model_version: str = MODEL_VERSION, min_prior_games: int = 3,
              l2: float = 2.0) -> dict[str, Any]:
    """Fit and persist the holdout-qualified standardized ridge model."""
    initialize(repository)
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute("""SELECT * FROM cfb_xpoints_dataset
          WHERE dataset_version=? AND season BETWEEN ? AND ?""",
            (dataset_version, from_season, to_season))]
    eligible = [row for row in rows if row["team_prior_games"] >= min_prior_games
                and all(row[key] is not None for key in ADVANCED_FEATURES)]
    if not eligible:
        raise ValueError("No eligible xPoints training rows")
    coefficients, means, scales = _fit_standardized(eligible, ADVANCED_FEATURES, l2=l2)
    if coefficients is None:
        raise ValueError("xPoints model matrix is singular")
    fitted_at = datetime.now(timezone.utc).isoformat()
    with closing(repository._connect()) as connection:
        connection.execute("""INSERT INTO cfb_xpoints_model VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(model_version) DO UPDATE SET feature_json=excluded.feature_json,
          coefficients_json=excluded.coefficients_json,means_json=excluded.means_json,
          scales_json=excluded.scales_json,l2=excluded.l2,
          from_season=excluded.from_season,to_season=excluded.to_season,
          training_rows=excluded.training_rows,fitted_at=excluded.fitted_at""",
          (model_version, json.dumps(ADVANCED_FEATURES), json.dumps(coefficients),
           json.dumps(means), json.dumps(scales), l2, from_season, to_season,
           len(eligible), fitted_at))
        connection.commit()
    return {"model_version": model_version, "features": list(ADVANCED_FEATURES),
            "training_rows": len(eligible), "from_season": from_season,
            "to_season": to_season, "l2": l2}


def load_model(repository, *, model_version: str = MODEL_VERSION) -> dict[str, Any] | None:
    initialize(repository)
    with repository._reader() as connection:
        row = connection.execute("SELECT * FROM cfb_xpoints_model WHERE model_version=?",
                                 (model_version,)).fetchone()
    if not row:
        return None
    row = dict(row)
    return {**row, "features": tuple(json.loads(row["feature_json"])),
            "coefficients": json.loads(row["coefficients_json"]),
            "means": json.loads(row["means_json"]),
            "scales": json.loads(row["scales_json"])}


def predict(model: dict[str, Any], features: dict[str, Any]) -> float | None:
    keys = model["features"]
    if any(features.get(key) is None for key in keys):
        return None
    value = float(model["coefficients"][0])
    for index, key in enumerate(keys):
        value += (float(model["coefficients"][index + 1])
                  * (float(features[key]) - float(model["means"][key]))
                  / float(model["scales"][key]))
    return max(0.0, value)


def evaluate(repository, *, train_from: int, train_to: int, test_from: int, test_to: int,
             dataset_version: str = DATASET_VERSION, min_prior_games: int = 3) -> dict[str, Any]:
    """Temporal holdout for simple, opponent-adjusted, recent-form and market baselines."""
    initialize(repository)
    with repository._reader() as connection:
        train = [dict(row) for row in connection.execute("""SELECT * FROM cfb_xpoints_dataset
          WHERE dataset_version=? AND season BETWEEN ? AND ?""",
            (dataset_version, train_from, train_to))]
        test = [dict(row) for row in connection.execute("""SELECT * FROM cfb_xpoints_dataset
          WHERE dataset_version=? AND season BETWEEN ? AND ?""",
            (dataset_version, test_from, test_to))]
    eligible_train = [row for row in train if row["team_prior_games"] >= min_prior_games
                      and all(row[key] is not None for key in ADVANCED_FEATURES)]
    coefficients = means = scales = None
    if eligible_train:
        coefficients, means, scales = _fit_standardized(eligible_train, ADVANCED_FEATURES)
    scores = {key: [] for key in ("A_league", "B_team", "C_matchup_blend",
                                   "D_opponent_adjusted", "E_adjusted_plus_recent",
                                   "M_market_implied")}
    evaluated = 0
    for row in test:
        if row["team_prior_games"] < min_prior_games:
            continue
        actual = float(row["actual_points_per_drive"]); evaluated += 1
        candidates = {
            "A_league": row["league_prior_points_per_drive"],
            "B_team": row["team_prior_points_per_drive"],
            "C_matchup_blend": ((row["team_prior_points_per_drive"] + row["opponent_prior_points_per_drive_allowed"]) / 2
                                if row["team_prior_points_per_drive"] is not None and row["opponent_prior_points_per_drive_allowed"] is not None else None),
            "D_opponent_adjusted": row["opponent_adjusted_points_per_drive"],
            "M_market_implied": (row["market_implied_points"] / row["actual_drives"]
                                 if row["market_implied_points"] is not None and row["actual_drives"] else None),
        }
        if coefficients is not None and all(row[key] is not None for key in ADVANCED_FEATURES):
            candidates["E_adjusted_plus_recent"] = max(0.0, coefficients[0] + sum(
                coefficients[index + 1] * (float(row[key]) - means[key]) / scales[key]
                for index, key in enumerate(ADVANCED_FEATURES)))
        for label, predicted in candidates.items():
            if predicted is not None: scores[label].append((float(predicted), actual))
    base_features = ADVANCED_FEATURES[:-1]
    variants = {
        "recent_and_residuals_only": base_features,
        "plus_elo": base_features + ("elo_difference",),
        "plus_fpi": base_features + ("fpi_margin",),
        "plus_core": base_features + ("core_margin",),
        "plus_vegas": base_features + ("vegas_margin",),
        "plus_equal_quality_blend": ADVANCED_FEATURES,
    }
    ablation = {}
    variant_coverage = {}
    matched_deltas = {}
    for label, feature_keys in variants.items():
        # Evaluate each quality lens on the rows where *that lens* is
        # available. Requiring Elo, FPI, CORE and Vegas simultaneously made
        # the intersection empty even though individual sources have coverage.
        matched_train = [
            row for row in train
            if row["team_prior_games"] >= min_prior_games
            and all(row[key] is not None for key in feature_keys)
        ]
        matched_test = [
            row for row in test
            if row["team_prior_games"] >= min_prior_games
            and all(row[key] is not None for key in feature_keys)
        ]
        variant_coefficients, variant_means, variant_scales = _fit_standardized(
            matched_train, feature_keys) if matched_train else (None, None, None)

        # Fit the no-quality baseline on the *same* rows. This makes the delta
        # an apples-to-apples estimate of whether the added lens helps rather
        # than a comparison contaminated by different source coverage.
        base_train = [
            row for row in matched_train
            if all(row[key] is not None for key in base_features)
        ]
        base_test = [
            row for row in matched_test
            if all(row[key] is not None for key in base_features)
        ]
        base_coefficients, base_means, base_scales = _fit_standardized(
            base_train, base_features) if base_train else (None, None, None)

        pairs = []
        base_pairs = []
        if variant_coefficients is not None:
            for row in matched_test:
                predicted = variant_coefficients[0] + sum(
                    variant_coefficients[index + 1]
                    * (float(row[key]) - variant_means[key]) / variant_scales[key]
                    for index, key in enumerate(feature_keys))
                pairs.append((max(0.0, predicted), float(row["actual_points_per_drive"])))
        if base_coefficients is not None:
            for row in base_test:
                predicted = base_coefficients[0] + sum(
                    base_coefficients[index + 1]
                    * (float(row[key]) - base_means[key]) / base_scales[key]
                    for index, key in enumerate(base_features))
                base_pairs.append((max(0.0, predicted), float(row["actual_points_per_drive"])))

        variant_metrics = _finish(pairs)
        base_metrics = _finish(base_pairs)
        ablation[label] = variant_metrics
        variant_coverage[label] = {
            "training_rows": len(matched_train),
            "test_rows": len(matched_test),
        }
        matched_deltas[label] = {
            "same_sample_base": base_metrics,
            "variant": variant_metrics,
            "mae_delta_vs_base": (
                round(variant_metrics["mae"] - base_metrics["mae"], 5)
                if variant_metrics["mae"] is not None and base_metrics["mae"] is not None
                else None
            ),
            "rmse_delta_vs_base": (
                round(variant_metrics["rmse"] - base_metrics["rmse"], 5)
                if variant_metrics["rmse"] is not None and base_metrics["rmse"] is not None
                else None
            ),
        }

    source_fields = {
        "elo": "elo_difference",
        "fpi": "fpi_margin",
        "core": "core_margin",
        "vegas": "vegas_margin",
        "quality_blend": "opponent_quality_blend",
    }
    quality_source_coverage = {
        name: {
            "training_rows": sum(
                1 for row in train
                if row["team_prior_games"] >= min_prior_games and row.get(field) is not None
            ),
            "test_rows": sum(
                1 for row in test
                if row["team_prior_games"] >= min_prior_games and row.get(field) is not None
            ),
        }
        for name, field in source_fields.items()
    }
    return {"dataset_version": dataset_version, "train_seasons": [train_from, train_to],
            "test_seasons": [test_from, test_to], "training_rows": len(eligible_train),
            "test_rows": evaluated, "features": list(ADVANCED_FEATURES),
            "standardized_coefficients": coefficients,
            "points_per_drive": {label: _finish(values) for label, values in scores.items()},
            "quality_ablation": {
                "coverage": variant_coverage,
                "source_coverage": quality_source_coverage,
                "matched_deltas": matched_deltas,
                **ablation,
            },
            "note": "All features are strictly pregame. Market uses actual drive count only as a diagnostic and is not deployable."}
