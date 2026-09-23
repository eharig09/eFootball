"""Streaming feature-based win probability model (wp-v2).

WP-v2 keeps the auditable, low-memory philosophy of wp-v1 but replaces coarse
state buckets with a logistic model over continuous game state plus pregame
strength. Training uses deterministic, streaming Newton/IRLS updates: only the
small gradient/Hessian matrices are retained in memory, so the result is not
sensitive to database row order like the original online-SGD prototype was.

Features are all pre-play / pregame information:
- home score margin
- game time remaining
- score-margin x late-game interaction
- possession
- field position oriented to the home team
- possession-oriented down and distance pressure
- pregame Elo differential when both Elo values are available
- neutral-site indicator

The model writes predictions into the same cfb_play_win_probability table used
by wp-v1, so existing validation and turning-point tooling can compare versions.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import math
from typing import Any

from sports_aggregator.cfb.repository import schema_once

MODEL_VERSION = "wp-v2"
WRITE_BATCH = 1000
FEATURE_VERSION = "wp-v2-features-2"
FEATURE_COUNT = 10


@schema_once("win_probability_v2")
def initialize(repository) -> None:
    from sports_aggregator.cfb.win_probability import initialize as initialize_wp
    initialize_wp(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_win_probability_logistic_model (
          model_version TEXT PRIMARY KEY,
          feature_version TEXT NOT NULL,
          coefficients_json TEXT NOT NULL,
          epochs INTEGER NOT NULL,
          learning_rate REAL NOT NULL,
          l2 REAL NOT NULL,
          samples INTEGER NOT NULL,
          fitted_at TEXT NOT NULL,
          from_season INTEGER,
          to_season INTEGER
        );
        """)
        connection.commit()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _home_state(row: Any) -> tuple[float, bool, float]:
    offense_is_home = str(row["offense"]) == str(row["home_team"])
    offense_score = _num(row["offense_score"])
    defense_score = _num(row["defense_score"])
    home_margin = offense_score - defense_score if offense_is_home else defense_score - offense_score

    yards_to_goal = max(1.0, min(100.0, _num(row["yards_to_goal"], 50.0)))
    # Convert the current spot into distance from the HOME attacking goal.
    # If away has the ball, a large away yards-to-goal means they are backed up
    # near their own end zone, which is favorable field position for home.
    home_ytg = yards_to_goal if offense_is_home else 100.0 - yards_to_goal
    return home_margin, offense_is_home, home_ytg


def _game_remaining(row: Any) -> float:
    try:
        period = int(row["period"])
        clock = int(row["clock_minutes"] or 0) * 60 + int(row["clock_seconds"] or 0)
    except (TypeError, ValueError):
        return 1800.0
    if period <= 4:
        return float(max(0, (4 - period) * 900 + clock))
    return 0.0


def _elo_diff(row: Any) -> tuple[float, bool]:
    home_elo = _optional_num(row["home_pregame_elo"])
    away_elo = _optional_num(row["away_pregame_elo"])
    if home_elo is None or away_elo is None:
        return 0.0, False
    return home_elo - away_elo, True


def features(row: Any) -> list[float]:
    margin, home_possession, home_ytg = _home_state(row)
    remaining = _game_remaining(row)
    elapsed = 1.0 - min(1.0, max(0.0, remaining / 3600.0))
    down = max(1.0, min(4.0, _num(row["down"], 1.0)))
    distance = max(0.0, min(25.0, _num(row["distance"], 10.0)))
    elo_diff, _ = _elo_diff(row)
    neutral = 1.0 if int(row["neutral_site"] or 0) else 0.0

    margin_scaled = max(-3.0, min(3.0, margin / 21.0))
    field_scaled = (50.0 - home_ytg) / 50.0
    time_scaled = remaining / 3600.0
    possession = 1.0 if home_possession else -1.0

    # Down/distance describe the OFFENSE'S burden. Orient them to home so a
    # difficult situation hurts home when home possesses and helps home when
    # away possesses.
    down_pressure = (down - 1.0) / 3.0
    distance_pressure = distance / 25.0
    down_advantage = -possession * down_pressure
    distance_advantage = -possession * distance_pressure

    return [
        1.0,
        margin_scaled,
        time_scaled,
        margin_scaled * elapsed,
        possession,
        field_scaled,
        down_advantage,
        distance_advantage,
        max(-3.0, min(3.0, elo_diff / 400.0)),
        neutral,
    ]


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 35.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -35.0))
    return z / (1.0 + z)


def _predict(weights: list[float], x: list[float]) -> float:
    return _sigmoid(sum(w * value for w, value in zip(weights, x)))


def _training_sql(from_season: int | None, to_season: int | None) -> tuple[str, list[Any]]:
    clauses = ["g.completed=1", "g.home_points IS NOT NULL", "g.away_points IS NOT NULL"]
    params: list[Any] = []
    if from_season is not None:
        clauses.append("p.season>=?")
        params.append(int(from_season))
    if to_season is not None:
        clauses.append("p.season<=?")
        params.append(int(to_season))
    sql = f"""
      SELECT p.offense,p.home_team,p.offense_score,p.defense_score,p.period,
             p.clock_minutes,p.clock_seconds,p.yards_to_goal,p.down,p.distance,
             g.home_points,g.away_points,g.home_pregame_elo,g.away_pregame_elo,g.neutral_site
      FROM cfb_plays p JOIN games g USING(game_id)
      WHERE {' AND '.join(clauses)}
    """
    return sql, params


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a tiny dense system with pivoted Gaussian elimination."""
    n = len(vector)
    augmented = [list(matrix[i]) + [float(vector[i])] for i in range(n)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-10:
            augmented[pivot][column] = 1e-10
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for j in range(column, n + 1):
            augmented[column][j] /= divisor
        for row in range(n):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0:
                continue
            for j in range(column, n + 1):
                augmented[row][j] -= factor * augmented[column][j]
    return [augmented[i][n] for i in range(n)]


def fit_model(repository, *, from_season: int | None = None,
              to_season: int | None = None, model_version: str = MODEL_VERSION,
              epochs: int = 8, learning_rate: float = 1.0,
              l2: float = 0.0005) -> dict[str, Any]:
    """Fit logistic WP with streaming damped Newton/IRLS updates.

    `learning_rate` is a damping multiplier on the Newton step (0 < rate <= 1
    is normally appropriate), not a per-row SGD rate.
    """
    initialize(repository)
    epochs = max(1, int(epochs))
    learning_rate = max(0.01, min(1.0, float(learning_rate)))
    l2 = max(0.0, float(l2))
    weights = [0.0] * FEATURE_COUNT
    sql, params = _training_sql(from_season, to_season)
    samples = 0
    final_log_loss = 0.0
    elo_available = 0
    neutral_samples = 0

    for epoch in range(epochs):
        gradient = [0.0] * FEATURE_COUNT
        hessian = [[0.0] * FEATURE_COUNT for _ in range(FEATURE_COUNT)]
        epoch_samples = 0
        epoch_loss = 0.0
        epoch_elo_available = 0
        epoch_neutral = 0
        with closing(repository._connect()) as connection:
            for row in connection.execute(sql, params):
                x = features(row)
                y = 1.0 if float(row["home_points"]) > float(row["away_points"]) else 0.0
                p = _predict(weights, x)
                residual = y - p
                curvature = max(1e-6, p * (1.0 - p))
                for i in range(FEATURE_COUNT):
                    gradient[i] += residual * x[i]
                    for j in range(i, FEATURE_COUNT):
                        hessian[i][j] += curvature * x[i] * x[j]
                clipped = min(.999999, max(.000001, p))
                epoch_loss += -(y * math.log(clipped) + (1.0 - y) * math.log(1.0 - clipped))
                epoch_samples += 1
                if _elo_diff(row)[1]:
                    epoch_elo_available += 1
                if int(row["neutral_site"] or 0):
                    epoch_neutral += 1

        if not epoch_samples:
            break
        for i in range(FEATURE_COUNT):
            for j in range(i):
                hessian[i][j] = hessian[j][i]
            if i != 0:
                # Scale ridge penalty by sample count so its meaning is stable.
                hessian[i][i] += l2 * epoch_samples
                gradient[i] -= l2 * epoch_samples * weights[i]
            else:
                hessian[i][i] += 1e-8

        step = _solve(hessian, gradient)
        # Clamp very large Newton jumps as an additional guard against a nearly
        # singular historical feature matrix.
        max_abs = max((abs(value) for value in step), default=0.0)
        shrink = min(1.0, 5.0 / max_abs) if max_abs > 0 else 1.0
        for i in range(FEATURE_COUNT):
            weights[i] += learning_rate * shrink * step[i]

        samples = epoch_samples
        final_log_loss = epoch_loss / epoch_samples
        elo_available = epoch_elo_available
        neutral_samples = epoch_neutral
        if max(abs(learning_rate * shrink * value) for value in step) < 1e-6:
            break

    # Evaluate once at the FINAL coefficients so the reported training loss is
    # directly comparable with holdout log loss; the old prototype reported the
    # loss encountered while weights were still changing within an epoch.
    evaluation_loss = 0.0
    evaluation_samples = 0
    mean_prediction = 0.0
    actual_home_wins = 0.0
    with closing(repository._connect()) as connection:
        for row in connection.execute(sql, params):
            p = _predict(weights, features(row))
            y = 1.0 if float(row["home_points"]) > float(row["away_points"]) else 0.0
            clipped = min(.999999, max(.000001, p))
            evaluation_loss += -(y * math.log(clipped) + (1.0 - y) * math.log(1.0 - clipped))
            mean_prediction += p
            actual_home_wins += y
            evaluation_samples += 1
    if evaluation_samples:
        final_log_loss = evaluation_loss / evaluation_samples

    fitted_at = datetime.now(timezone.utc).isoformat()
    with closing(repository._connect()) as connection:
        connection.execute("""INSERT OR REPLACE INTO cfb_win_probability_logistic_model
          (model_version,feature_version,coefficients_json,epochs,learning_rate,l2,samples,
           fitted_at,from_season,to_season) VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (model_version, FEATURE_VERSION, json.dumps(weights), epochs, learning_rate, l2,
           samples, fitted_at, from_season, to_season))
        connection.commit()
    return {
        "model_version": model_version,
        "feature_version": FEATURE_VERSION,
        "plays": samples,
        "epochs_requested": epochs,
        "training_log_loss": round(final_log_loss, 5),
        "training_mean_prediction": round(mean_prediction / evaluation_samples, 4) if evaluation_samples else None,
        "training_home_win_rate": round(actual_home_wins / evaluation_samples, 4) if evaluation_samples else None,
        "elo_coverage": round(elo_available / samples, 4) if samples else 0.0,
        "neutral_site_share": round(neutral_samples / samples, 4) if samples else 0.0,
        "coefficients": [round(value, 6) for value in weights],
        "from_season": from_season,
        "to_season": to_season,
    }


def game_win_probability_series(repository, game_id: int, *,
                                model_version: str = MODEL_VERSION) -> list[dict[str, Any]]:
    """One game's play-by-play home win probability, in chronological order.

    Chronological is drive_number/play_number, not clock time -- overtime
    periods run the clock backward from CFBD's own zero, so ordering by clock
    would put OT before the fourth quarter ends.
    """
    initialize(repository)
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT p.drive_number, p.play_number, p.period, p.clock_minutes,
                      p.clock_seconds, p.offense_score, p.defense_score, p.offense,
                      p.home_team, p.down, p.distance, p.play_text, p.play_type,
                      w.home_win_probability
               FROM cfb_plays p JOIN cfb_play_win_probability w
                 ON w.play_id = p.play_id AND w.model_version = ?
               WHERE p.game_id = ?
               ORDER BY p.drive_number, p.play_number""",
            (model_version, int(game_id))).fetchall()
    return [dict(row) for row in rows if row["home_win_probability"] is not None]


def team_win_probability_values(repository, game_id: int, team_id: int, *,
                                model_version: str = MODEL_VERSION) -> list[float]:
    """One team's own win probability (0-100) through one game, home or away.

    A team that was away needs its share flipped from the stored home
    probability; which side that team was on for this specific game_id is
    read straight from games rather than trusted from a caller's own
    home/away framing, so a mismatched team_id/game_id pair fails loudly
    (empty series) rather than silently reporting the wrong team's numbers.
    """
    initialize(repository)
    with repository._reader() as connection:
        game_row = connection.execute(
            "SELECT home_team_id, away_team_id FROM games WHERE game_id=?",
            (int(game_id),)).fetchone()
    if game_row is None or int(team_id) not in (game_row["home_team_id"], game_row["away_team_id"]):
        return []
    team_is_home = game_row["home_team_id"] == int(team_id)
    series = game_win_probability_series(repository, game_id, model_version=model_version)
    return [
        round((row["home_win_probability"] if team_is_home
              else 1 - row["home_win_probability"]) * 100, 1)
        for row in series
    ]


def _load_weights(repository, model_version: str) -> tuple[list[float] | None, str | None]:
    initialize(repository)
    with closing(repository._connect()) as connection:
        row = connection.execute(
            "SELECT coefficients_json,feature_version FROM cfb_win_probability_logistic_model WHERE model_version=?",
            (model_version,)).fetchone()
    if not row:
        return None, None
    values = json.loads(str(row[0]))
    return [float(value) for value in values], str(row[1])


def score_plays(repository, *, from_season: int | None = None,
                to_season: int | None = None, model_version: str = MODEL_VERSION) -> dict[str, Any]:
    """Score stored plays in streaming order and attach leverage to the causing play."""
    weights, feature_version = _load_weights(repository, model_version)
    if not weights:
        return {"model_version": model_version, "scored": 0, "reason": "model_not_fitted"}
    if feature_version != FEATURE_VERSION:
        return {
            "model_version": model_version,
            "scored": 0,
            "reason": "feature_version_mismatch",
            "stored_feature_version": feature_version,
            "required_feature_version": FEATURE_VERSION,
        }

    clauses: list[str] = []
    params: list[Any] = []
    if from_season is not None:
        clauses.append("p.season>=?")
        params.append(int(from_season))
    if to_season is not None:
        clauses.append("p.season<=?")
        params.append(int(to_season))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with closing(repository._connect()) as connection:
        delete_clauses = []
        delete_params: list[Any] = [model_version]
        if from_season is not None:
            delete_clauses.append("season>=?")
            delete_params.append(int(from_season))
        if to_season is not None:
            delete_clauses.append("season<=?")
            delete_params.append(int(to_season))
        if delete_clauses:
            connection.execute(f"""DELETE FROM cfb_play_win_probability
              WHERE model_version=? AND play_id IN (
                SELECT play_id FROM cfb_plays WHERE {' AND '.join(delete_clauses)}
              )""", delete_params)
        else:
            connection.execute("DELETE FROM cfb_play_win_probability WHERE model_version=?", (model_version,))
        connection.commit()

    now = datetime.now(timezone.utc).isoformat()
    batch: list[tuple[Any, ...]] = []
    scored = 0
    pending: tuple[str, float] | None = None
    current_game: int | None = None
    elo_available = 0
    total_rows = 0

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        with closing(repository._connect()) as writer:
            writer.executemany(
                "INSERT OR REPLACE INTO cfb_play_win_probability VALUES(?,?,?,?,?)", batch)
            writer.commit()
        batch = []

    def emit(play_id: str, probability: float, next_probability: float | None) -> None:
        nonlocal scored, batch
        leverage = abs(next_probability - probability) if next_probability is not None else None
        batch.append((play_id, model_version, probability, leverage, now))
        scored += 1
        if len(batch) >= WRITE_BATCH:
            flush()

    with closing(repository._connect()) as reader:
        cursor = reader.execute(f"""
          SELECT p.play_id,p.game_id,p.offense,p.home_team,p.offense_score,p.defense_score,
                 p.period,p.clock_minutes,p.clock_seconds,p.yards_to_goal,p.down,p.distance,
                 p.drive_number,p.play_number,g.home_pregame_elo,g.away_pregame_elo,g.neutral_site
          FROM cfb_plays p JOIN games g USING(game_id)
          {where}
          ORDER BY p.game_id,p.period,p.clock_minutes DESC,p.clock_seconds DESC,p.drive_number,p.play_number
        """, params)
        for row in cursor:
            game_id = int(row["game_id"])
            probability = _predict(weights, features(row))
            total_rows += 1
            if _elo_diff(row)[1]:
                elo_available += 1
            if current_game is not None and game_id != current_game:
                if pending is not None:
                    emit(pending[0], pending[1], None)
                pending = None
            if pending is not None:
                emit(pending[0], pending[1], probability)
            pending = (str(row["play_id"]), probability)
            current_game = game_id
        if pending is not None:
            emit(pending[0], pending[1], None)
    flush()
    return {
        "model_version": model_version,
        "feature_version": feature_version,
        "scored": scored,
        "elo_coverage": round(elo_available / total_rows, 4) if total_rows else 0.0,
    }
