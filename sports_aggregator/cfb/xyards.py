"""Leak-safe expected yardage: Expected Passing/Rushing Yards (Milestone/spec section 20).

`ExpectedPassYards = ExpectedDropbacks x ExpectedYardsPerDropback` and
`ExpectedRushYards = ExpectedRushAttempts x ExpectedYardsPerRush` -- the
fourth layer on top of xDrives, xPlaysPerDrive and xVolume. Efficiency is
explicitly an offense x defense interaction, not a team average alone: a
team's own trailing yards/dropback is blended with what its *opponent's
defense* has been allowing per dropback, the same "allowed" mirror pattern
`xdrives.py`/`xplays.py`/`xvolume.py` all use, applied separately to the
pass and rush games since a team's efficiency identity in each is its own
signal (spec section 20's own example treats
`f(PassOffense, PassDefense, ...)` and `f(RushOffense, RunDefense, ...)`
as two different functions, not one).

Built the same leak-safe, recency-weighted way as the three layers below it,
reusing xDrives' tuned half-life. Scope note: same as `xplays.py`/
`xvolume.py`, this ships with in-sample Baselines A-D only -- a genuine
train/test holdout should be added before this feeds a served projection,
and no environment/shrinkage-style lever is added preemptively (see
`docs/CFB_XDRIVES.md` section 7 and `docs/CFB_XVOLUME.md` for why those did
not earn their complexity when actually tested).
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Sequence

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.xdrives import RECENCY_LAMBDA, TRAILING_WINDOW_GAMES

DATASET_VERSION = "xyards-dataset-v1"

_OWN_FIELDS = ("yards_per_dropback", "yards_per_rush", "success_rate", "explosive_rate")


@schema_once("xyards")
def initialize(repository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_xyards_dataset (
          game_id INTEGER NOT NULL,
          team TEXT NOT NULL,
          opponent TEXT NOT NULL,
          dataset_version TEXT NOT NULL,
          season INTEGER NOT NULL,
          week INTEGER NOT NULL,
          home_away TEXT NOT NULL,

          actual_yards_per_dropback REAL,
          actual_yards_per_rush REAL,
          actual_pass_yards INTEGER,
          actual_rush_yards INTEGER,
          actual_pass_attempts INTEGER,
          actual_rush_attempts INTEGER,

          team_prior_games INTEGER NOT NULL,
          team_prior_yards_per_dropback REAL,
          team_prior_yards_per_dropback_allowed REAL,
          team_prior_yards_per_rush REAL,
          team_prior_yards_per_rush_allowed REAL,
          team_prior_success_rate REAL,
          team_prior_explosive_rate REAL,

          opponent_prior_games INTEGER NOT NULL,
          opponent_prior_yards_per_dropback REAL,
          opponent_prior_yards_per_dropback_allowed REAL,
          opponent_prior_yards_per_rush REAL,
          opponent_prior_yards_per_rush_allowed REAL,
          opponent_prior_success_rate REAL,
          opponent_prior_explosive_rate REAL,

          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,dataset_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_xyards_dataset_team
          ON cfb_xyards_dataset(team,dataset_version,season,week);
        """)
        connection.commit()


def _decay_weights(count: int, lam: float) -> list[float]:
    return [math.exp(-lam * (count - 1 - i)) for i in range(count)]


def _weighted_mean(weights: Sequence[float], values: Iterable[Any]) -> float | None:
    pairs = [(w, float(v)) for w, v in zip(weights, values) if v is not None]
    total_weight = sum(w for w, _ in pairs)
    if not pairs or total_weight <= 0:
        return None
    return sum(w * v for w, v in pairs) / total_weight


def _trailing_summary(own_window: deque, pass_allowed_window: deque, rush_allowed_window: deque,
                      *, lam: float) -> dict[str, Any]:
    own_weights = _decay_weights(len(own_window), lam)
    summary = {"games": len(own_window)}
    for field in _OWN_FIELDS:
        summary[field] = _weighted_mean(own_weights, (row[field] for row in own_window))
    summary["yards_per_dropback_allowed"] = _weighted_mean(
        _decay_weights(len(pass_allowed_window), lam), pass_allowed_window)
    summary["yards_per_rush_allowed"] = _weighted_mean(
        _decay_weights(len(rush_allowed_window), lam), rush_allowed_window)
    return summary


def build_dataset(repository, *, from_season: int | None = None, to_season: int | None = None,
                  dataset_version: str = DATASET_VERSION,
                  window: int = TRAILING_WINDOW_GAMES,
                  half_life_games: float | None = None) -> dict[str, Any]:
    """Rebuild the leak-safe expected-yardage dataset for [from_season, to_season].

    Same construction as `xvolume.build_dataset`: trailing features read a
    team's entire stored history regardless of this range, but only rows
    whose own game falls in it are written.
    """
    from sports_aggregator.cfb.team_game_pace import METRIC_VERSION as PACE_VERSION
    from sports_aggregator.cfb.team_game_pace import initialize as initialize_pace

    lam = RECENCY_LAMBDA if half_life_games is None else (
        0.0 if half_life_games in (math.inf,) else math.log(2.0) / half_life_games)

    initialize(repository)
    initialize_pace(repository)

    with closing(repository._connect()) as connection:
        pace_rows = [dict(row) for row in connection.execute("""
          SELECT a.game_id, a.team, a.opponent, a.yards_per_dropback, a.yards_per_rush,
                 a.pass_yards, a.rush_yards, a.pass_plays, a.rush_plays,
                 a.success_rate, a.explosive_rate,
                 g.season, g.week, g.start_date, g.home_team, g.away_team
          FROM cfb_team_game_pace a JOIN games g ON g.game_id=a.game_id
          WHERE a.metric_version=?
          ORDER BY a.team, g.start_date
        """, (PACE_VERSION,)).fetchall()]

    pace_by_key = {(row["game_id"], row["team"]): row for row in pace_rows}
    by_team: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pace_rows:
        by_team[row["team"]].append(row)

    prior_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for team, games_for_team in by_team.items():
        own_window: deque = deque(maxlen=window)
        pass_allowed_window: deque = deque(maxlen=window)
        rush_allowed_window: deque = deque(maxlen=window)
        for row in games_for_team:
            prior_by_key[(row["game_id"], team)] = _trailing_summary(
                own_window, pass_allowed_window, rush_allowed_window, lam=lam)
            own_window.append(row)
            opponent_row = pace_by_key.get((row["game_id"], row["opponent"]))
            if opponent_row is not None:
                pass_allowed_window.append(opponent_row["yards_per_dropback"])
                rush_allowed_window.append(opponent_row["yards_per_rush"])

    now = datetime.now(timezone.utc).isoformat()
    output = []
    for row in pace_rows:
        if from_season is not None and row["season"] < from_season:
            continue
        if to_season is not None and row["season"] > to_season:
            continue
        team, opponent = row["team"], row["opponent"]
        empty_prior = {"games": 0, "yards_per_dropback_allowed": None, "yards_per_rush_allowed": None,
                      **{f: None for f in _OWN_FIELDS}}
        team_prior = prior_by_key.get((row["game_id"], team)) or empty_prior
        opponent_prior = prior_by_key.get((row["game_id"], opponent)) or empty_prior
        is_home = row["team"] == row["home_team"]

        output.append((
            row["game_id"], team, opponent, dataset_version, row["season"], row["week"],
            "home" if is_home else "away",
            row["yards_per_dropback"], row["yards_per_rush"], row["pass_yards"], row["rush_yards"],
            row["pass_plays"], row["rush_plays"],
            team_prior["games"], team_prior["yards_per_dropback"], team_prior["yards_per_dropback_allowed"],
            team_prior["yards_per_rush"], team_prior["yards_per_rush_allowed"],
            team_prior["success_rate"], team_prior["explosive_rate"],
            opponent_prior["games"], opponent_prior["yards_per_dropback"], opponent_prior["yards_per_dropback_allowed"],
            opponent_prior["yards_per_rush"], opponent_prior["yards_per_rush_allowed"],
            opponent_prior["success_rate"], opponent_prior["explosive_rate"],
            now,
        ))

    with closing(repository._connect()) as connection:
        if from_season is not None or to_season is not None:
            clauses = []
            params: list[Any] = [dataset_version]
            if from_season is not None:
                clauses.append("season>=?"); params.append(int(from_season))
            if to_season is not None:
                clauses.append("season<=?"); params.append(int(to_season))
            connection.execute(
                f"DELETE FROM cfb_xyards_dataset WHERE dataset_version=? AND {' AND '.join(clauses)}", params)
        else:
            connection.execute("DELETE FROM cfb_xyards_dataset WHERE dataset_version=?", (dataset_version,))
        connection.executemany("""INSERT INTO cfb_xyards_dataset(
          game_id,team,opponent,dataset_version,season,week,home_away,
          actual_yards_per_dropback,actual_yards_per_rush,actual_pass_yards,actual_rush_yards,
          actual_pass_attempts,actual_rush_attempts,
          team_prior_games,team_prior_yards_per_dropback,team_prior_yards_per_dropback_allowed,
          team_prior_yards_per_rush,team_prior_yards_per_rush_allowed,
          team_prior_success_rate,team_prior_explosive_rate,
          opponent_prior_games,opponent_prior_yards_per_dropback,opponent_prior_yards_per_dropback_allowed,
          opponent_prior_yards_per_rush,opponent_prior_yards_per_rush_allowed,
          opponent_prior_success_rate,opponent_prior_explosive_rate,
          built_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)
        connection.commit()

    return {
        "dataset_version": dataset_version,
        "from_season": from_season,
        "to_season": to_season,
        "rows": len(output),
    }


def _read_dataset_rows(repository, *, from_season: int | None, to_season: int | None,
                       dataset_version: str) -> list[dict[str, Any]]:
    clauses = ["dataset_version=?"]
    params: list[Any] = [dataset_version]
    if from_season is not None:
        clauses.append("season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("season<=?"); params.append(int(to_season))
    with closing(repository._connect()) as connection:
        return [dict(row) for row in connection.execute(
            f"SELECT * FROM cfb_xyards_dataset WHERE {' AND '.join(clauses)}", params)]


def _mean(values: Iterable[Any]) -> float | None:
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    augmented = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        for row in range(col + 1, n):
            factor = augmented[row][col] / augmented[col][col]
            for k in range(col, n + 1):
                augmented[row][k] -= factor * augmented[col][k]
    solution = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = augmented[row][n] - sum(augmented[row][k] * solution[k] for k in range(row + 1, n))
        solution[row] = total / augmented[row][row]
    return solution


def _fit_ols(rows: list[dict[str, Any]], feature_keys: tuple[str, ...], target_key: str, *,
            l2: float = 0.0) -> list[float] | None:
    n_features = len(feature_keys) + 1
    xtx = [[0.0] * n_features for _ in range(n_features)]
    xty = [0.0] * n_features
    for row in rows:
        x = [1.0] + [float(row[key]) for key in feature_keys]
        y = float(row[target_key])
        for i in range(n_features):
            xty[i] += x[i] * y
            for j in range(n_features):
                xtx[i][j] += x[i] * x[j]
    if l2:
        for i in range(1, n_features):
            xtx[i][i] += l2
    return _solve(xtx, xty)


def _accumulator() -> dict[str, float]:
    return {"n": 0.0, "abs_error": 0.0, "sq_error": 0.0, "sum_pred": 0.0, "sum_actual": 0.0}


def _add(acc: dict[str, float], predicted: float, actual: float) -> None:
    error = predicted - actual
    acc["n"] += 1
    acc["abs_error"] += abs(error)
    acc["sq_error"] += error * error
    acc["sum_pred"] += predicted
    acc["sum_actual"] += actual


def _finish(acc: dict[str, float]) -> dict[str, Any]:
    n = int(acc["n"])
    if not n:
        return {"games": 0, "mae": None, "rmse": None, "bias": None}
    return {
        "games": n,
        "mae": round(acc["abs_error"] / n, 4),
        "rmse": round(math.sqrt(acc["sq_error"] / n), 4),
        "bias": round((acc["sum_pred"] - acc["sum_actual"]) / n, 4),
    }


def _evaluate_metric(rows: list[dict[str, Any]], *, target: str, team_key: str, allowed_key: str,
                     regression_features: tuple[str, ...], min_prior_games: int) -> dict[str, Any]:
    """Baselines A-D for one metric (pass or rush), shared by both halves of `evaluate_baselines`."""
    cold_start, scoped = [], []
    for row in rows:
        if row["team_prior_games"] < min_prior_games or row[target] is None:
            cold_start.append(row)
        else:
            scoped.append(row)
    league_mean = _mean(r[target] for r in scoped)

    ols_rows = [r for r in scoped if all(r[key] is not None for key in regression_features)]
    coefficients = _fit_ols(ols_rows, regression_features, target, l2=1.0) if ols_rows else None

    def _score(scope_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        accumulators = {label: _accumulator() for label in "ABCD"}
        for row in scope_rows:
            actual = float(row[target])
            if league_mean is not None:
                _add(accumulators["A"], league_mean, actual)
            if row[team_key] is not None:
                _add(accumulators["B"], row[team_key], actual)
            if row[team_key] is not None and row[allowed_key] is not None:
                predicted_c = (row[team_key] + row[allowed_key]) / 2
                _add(accumulators["C"], predicted_c, actual)
            if coefficients is not None and all(row[key] is not None for key in regression_features):
                predicted_d = coefficients[0] + sum(
                    c * row[key] for c, key in zip(coefficients[1:], regression_features))
                _add(accumulators["D"], predicted_d, actual)
        return {
            "A_league_average": _finish(accumulators["A"]),
            "B_team_average": _finish(accumulators["B"]),
            "C_team_and_opponent_allowed_blend": _finish(accumulators["C"]),
            "D_linear_regression": _finish(accumulators["D"]),
        }

    by_season_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in scoped:
        by_season_rows[int(row["season"])].append(row)

    return {
        "rows_evaluated": len(scoped),
        "rows_dropped_cold_start": len(cold_start),
        "league_average": round(league_mean, 4) if league_mean is not None else None,
        "regression_features": list(regression_features),
        "regression_coefficients": coefficients,
        "overall": _score(scoped),
        "by_season": {str(season): _score(season_rows) for season, season_rows in sorted(by_season_rows.items())},
    }


def evaluate_baselines(repository, *, from_season: int | None = None, to_season: int | None = None,
                       dataset_version: str = DATASET_VERSION,
                       min_prior_games: int = 1) -> dict[str, Any]:
    """MAE/RMSE/bias for passing- and rushing-yardage Baselines A-D.

    A and D are in-sample diagnostics, same disclosure as every other
    xdrives/xplays/xvolume baseline evaluator. B and C use only
    strictly-prior trailing data and are leak-safe by construction; C is the
    explicit offense x defense interaction spec section 20 asks for --
    a team's own trailing efficiency blended with what its opponent's
    defense has been allowing.
    """
    initialize(repository)
    rows = _read_dataset_rows(repository, from_season=from_season, to_season=to_season,
                              dataset_version=dataset_version)

    passing = _evaluate_metric(
        rows, target="actual_yards_per_dropback",
        team_key="team_prior_yards_per_dropback", allowed_key="opponent_prior_yards_per_dropback_allowed",
        regression_features=("team_prior_yards_per_dropback", "opponent_prior_yards_per_dropback_allowed",
                             "team_prior_success_rate", "team_prior_explosive_rate"),
        min_prior_games=min_prior_games)
    rushing = _evaluate_metric(
        rows, target="actual_yards_per_rush",
        team_key="team_prior_yards_per_rush", allowed_key="opponent_prior_yards_per_rush_allowed",
        regression_features=("team_prior_yards_per_rush", "opponent_prior_yards_per_rush_allowed",
                             "team_prior_success_rate", "team_prior_explosive_rate"),
        min_prior_games=min_prior_games)

    return {
        "dataset_version": dataset_version,
        "from_season": from_season,
        "to_season": to_season,
        "passing": passing,
        "rushing": rushing,
        "evaluation_note": (
            "A (league mean) and D (regression) are both fit on these same rows -- in-sample "
            "diagnostics, not out-of-sample performance. B and C use only strictly-prior trailing "
            "data and are leak-safe by construction."
        ),
    }


def evaluate_expected_yardage(repository, *, from_season: int | None = None, to_season: int | None = None,
                              xvolume_dataset_version: str | None = None,
                              xyards_dataset_version: str = DATASET_VERSION,
                              min_prior_games: int = 1) -> dict[str, Any]:
    """Backtest Expected Passing/Rushing Yards against actual yardage.

    `ExpectedPassYards = ExpectedDropbacks x ExpectedYardsPerDropback`,
    `ExpectedRushYards = ExpectedRushAttempts x ExpectedYardsPerRush` --
    chains xVolume's own Baseline C (predicted attempts) with this module's
    own Baseline C (predicted yards/attempt). In-sample only, same
    disclosure as `xplays.evaluate_expected_plays` and
    `xvolume.evaluate_expected_volume`.
    """
    from sports_aggregator.cfb.xvolume import DATASET_VERSION as XVOLUME_DATASET_VERSION
    from sports_aggregator.cfb.xvolume import initialize as initialize_xvolume

    initialize(repository)
    initialize_xvolume(repository)
    xvolume_version = xvolume_dataset_version or XVOLUME_DATASET_VERSION

    clauses = ["dataset_version=?"]
    params: list[Any] = [xvolume_version]
    if from_season is not None:
        clauses.append("season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("season<=?"); params.append(int(to_season))
    with closing(repository._connect()) as connection:
        volume_rows = {
            (row["game_id"], row["team"]): dict(row)
            for row in connection.execute(
                f"SELECT game_id,team,team_prior_pass_rate,opponent_prior_pass_rate_allowed "
                f"FROM cfb_xvolume_dataset WHERE {' AND '.join(clauses)}", params)
        }

    # xVolume itself needs xDrives x xPlaysPerDrive for attempt counts; reuse
    # its own combination logic rather than re-deriving predicted attempts
    # here, so this stays "chain the layer below's own Baseline C" all the
    # way down instead of picking a different rule partway through.
    from sports_aggregator.cfb.xdrives import DATASET_VERSION as XDRIVES_DATASET_VERSION
    from sports_aggregator.cfb.xdrives import initialize as initialize_xdrives
    from sports_aggregator.cfb.xplays import DATASET_VERSION as XPLAYS_DATASET_VERSION
    from sports_aggregator.cfb.xplays import initialize as initialize_xplays
    initialize_xdrives(repository)
    initialize_xplays(repository)

    def _joined(table: str, version: str, columns: str) -> dict[tuple[int, str], dict[str, Any]]:
        table_clauses = ["dataset_version=?"]
        table_params: list[Any] = [version]
        if from_season is not None:
            table_clauses.append("season>=?"); table_params.append(int(from_season))
        if to_season is not None:
            table_clauses.append("season<=?"); table_params.append(int(to_season))
        with closing(repository._connect()) as connection:
            return {
                (row["game_id"], row["team"]): dict(row)
                for row in connection.execute(
                    f"SELECT game_id,team,{columns} FROM {table} WHERE {' AND '.join(table_clauses)}", table_params)
            }

    drive_rows = _joined("cfb_xdrives_dataset", XDRIVES_DATASET_VERSION,
                        "team_prior_drives,opponent_prior_drives_allowed")
    play_rows = _joined("cfb_xplays_dataset", XPLAYS_DATASET_VERSION,
                       "team_prior_plays_per_drive,opponent_prior_plays_per_drive_allowed")
    yards_rows = _read_dataset_rows(repository, from_season=from_season, to_season=to_season,
                                    dataset_version=xyards_dataset_version)

    eligible = [row for row in yards_rows if row["team_prior_games"] >= min_prior_games
               and row["actual_pass_yards"] is not None and row["actual_rush_yards"] is not None]

    league_drives = _mean(drive_rows.get((r["game_id"], r["team"]), {}).get("team_prior_drives") for r in eligible)
    league_ppd = _mean(
        play_rows.get((r["game_id"], r["team"]), {}).get("team_prior_plays_per_drive") for r in eligible)
    league_pass_rate = _mean(
        volume_rows.get((r["game_id"], r["team"]), {}).get("team_prior_pass_rate") for r in eligible)
    league_ypd = _mean(r["team_prior_yards_per_dropback"] for r in eligible)
    league_ypr = _mean(r["team_prior_yards_per_rush"] for r in eligible)
    naive_ready = None not in (league_drives, league_ppd, league_pass_rate, league_ypd, league_ypr)
    if naive_ready:
        naive_plays = league_drives * league_ppd
        naive_pass_yards_constant = naive_plays * league_pass_rate * league_ypd
        naive_rush_yards_constant = naive_plays * (1 - league_pass_rate) * league_ypr

    naive_pass_acc, naive_rush_acc = _accumulator(), _accumulator()
    pass_yards_acc, rush_yards_acc = _accumulator(), _accumulator()
    matched = 0
    for row in eligible:
        if naive_ready:
            _add(naive_pass_acc, naive_pass_yards_constant, float(row["actual_pass_yards"]))
            _add(naive_rush_acc, naive_rush_yards_constant, float(row["actual_rush_yards"]))

        key = (row["game_id"], row["team"])
        drives_row, plays_row, volume_row = drive_rows.get(key), play_rows.get(key), volume_rows.get(key)
        if drives_row is None or plays_row is None or volume_row is None:
            continue
        needed = (
            drives_row.get("team_prior_drives"), drives_row.get("opponent_prior_drives_allowed"),
            plays_row.get("team_prior_plays_per_drive"), plays_row.get("opponent_prior_plays_per_drive_allowed"),
            volume_row.get("team_prior_pass_rate"), volume_row.get("opponent_prior_pass_rate_allowed"),
            row["team_prior_yards_per_dropback"], row["opponent_prior_yards_per_dropback_allowed"],
            row["team_prior_yards_per_rush"], row["opponent_prior_yards_per_rush_allowed"],
        )
        if None in needed:
            continue
        predicted_drives = (drives_row["team_prior_drives"] + drives_row["opponent_prior_drives_allowed"]) / 2
        predicted_ppd = (plays_row["team_prior_plays_per_drive"]
                        + plays_row["opponent_prior_plays_per_drive_allowed"]) / 2
        predicted_pass_rate = (volume_row["team_prior_pass_rate"]
                              + volume_row["opponent_prior_pass_rate_allowed"]) / 2
        predicted_plays = predicted_drives * predicted_ppd
        predicted_dropbacks = predicted_plays * predicted_pass_rate
        predicted_rushes = predicted_plays * (1 - predicted_pass_rate)

        predicted_ypd = (row["team_prior_yards_per_dropback"] + row["opponent_prior_yards_per_dropback_allowed"]) / 2
        predicted_ypr = (row["team_prior_yards_per_rush"] + row["opponent_prior_yards_per_rush_allowed"]) / 2

        _add(pass_yards_acc, predicted_dropbacks * predicted_ypd, float(row["actual_pass_yards"]))
        _add(rush_yards_acc, predicted_rushes * predicted_ypr, float(row["actual_rush_yards"]))
        matched += 1

    return {
        "from_season": from_season,
        "to_season": to_season,
        "rows_matched": matched,
        "naive_constant": {
            "expected_pass_yards": _finish(naive_pass_acc),
            "expected_rush_yards": _finish(naive_rush_acc),
        },
        "combined_matchup_blend": {
            "expected_pass_yards": _finish(pass_yards_acc),
            "expected_rush_yards": _finish(rush_yards_acc),
        },
        "evaluation_note": (
            "'naive_constant' applies the same league-wide drives x plays-per-drive x pass-rate x "
            "yards-per-attempt product to every team-game; 'combined_matchup_blend' chains each "
            "layer's own opponent-adjusted Baseline C, all the way down from xDrives. In-sample "
            "only (no train/test season split) -- this checks whether the four-layer chain is "
            "coherent, not final predictive performance."
        ),
    }
