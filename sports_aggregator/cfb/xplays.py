"""Leak-safe expected-plays-per-drive dataset and Expected Plays (Milestone 5).

xDrives (`xdrives.py`) answers "how many possessions will this team get".
This answers "how many plays will each of those possessions take" -- spec
section 18's `xPlaysPerDrive` -- so together (`xDrives x xPlaysPerDrive`)
they produce Expected Plays without ever jumping straight from team
averages to a play count.

Built the same leak-safe, recency-weighted way `xdrives.py` walks
`cfb_team_game_pace`: a team's own trailing plays/drive and the efficiency
drivers spec section 18 names (success rate, explosiveness, first-down rate,
three-and-out rate), plus what its *opponents* have needed per drive against
it -- the defensive mirror, exactly like xDrives' `drives_allowed`. Reuses
xDrives' already-tuned recency window (`RECENCY_LAMBDA`,
`TRAILING_WINDOW_GAMES`) rather than re-deriving one, since there is no
evidence a team's plays-per-drive identity decays at a different rate than
its drive count does.

Scope note: after xDrives found that an environment term, shrinkage and a
10-feature regression each failed to beat a simple opponent-adjusted blend
out of sample (see `docs/CFB_XDRIVES.md` section 7), this module ships with
only the direct analogs of Baselines A-D and does not re-run that same
experiment here. If a genuine holdout check later shows the simple blend
underfitting plays/drive specifically, add the extra levers then -- not
before, per spec section 37.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
import math
from typing import Any, Callable, Iterable, Sequence

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.xdrives import RECENCY_LAMBDA, TRAILING_WINDOW_GAMES

DATASET_VERSION = "xplays-dataset-v1"

#: The efficiency drivers spec section 18 names as inputs to plays/drive,
#: beyond plays/drive itself. Penalties, sack rate and turnover rate are not
#: yet columns on `cfb_team_game_pace` (turnovers are Milestone 9's own
#: model) -- included here only once they exist there, to keep this file's
#: claims matched to what is actually measured.
_TRAILING_FIELDS = (
    "plays_per_meaningful_drive", "success_rate", "explosive_rate",
    "first_down_rate", "three_and_out_rate",
)


@schema_once("xplays")
def initialize(repository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_xplays_dataset (
          game_id INTEGER NOT NULL,
          team TEXT NOT NULL,
          opponent TEXT NOT NULL,
          dataset_version TEXT NOT NULL,
          season INTEGER NOT NULL,
          week INTEGER NOT NULL,
          home_away TEXT NOT NULL,

          actual_plays_per_drive REAL,
          actual_scrimmage_plays INTEGER,
          actual_meaningful_drives INTEGER,

          team_prior_games INTEGER NOT NULL,
          team_prior_plays_per_drive REAL,
          team_prior_plays_per_drive_allowed REAL,
          team_prior_success_rate REAL,
          team_prior_explosive_rate REAL,
          team_prior_first_down_rate REAL,
          team_prior_three_and_out_rate REAL,

          opponent_prior_games INTEGER NOT NULL,
          opponent_prior_plays_per_drive REAL,
          opponent_prior_plays_per_drive_allowed REAL,
          opponent_prior_success_rate REAL,
          opponent_prior_explosive_rate REAL,
          opponent_prior_first_down_rate REAL,
          opponent_prior_three_and_out_rate REAL,

          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,dataset_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_xplays_dataset_team
          ON cfb_xplays_dataset(team,dataset_version,season,week);
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


def _trailing_summary(own_window: deque, allowed_window: deque, *, lam: float) -> dict[str, Any]:
    own_weights = _decay_weights(len(own_window), lam)
    allowed_weights = _decay_weights(len(allowed_window), lam)
    summary = {"games": len(own_window)}
    for field in _TRAILING_FIELDS:
        summary[field] = _weighted_mean(own_weights, (row[field] for row in own_window))
    summary["plays_per_drive_allowed"] = _weighted_mean(allowed_weights, allowed_window)
    return summary


def build_dataset(repository, *, from_season: int | None = None, to_season: int | None = None,
                  dataset_version: str = DATASET_VERSION,
                  window: int = TRAILING_WINDOW_GAMES,
                  half_life_games: float | None = None) -> dict[str, Any]:
    """Rebuild the leak-safe plays-per-drive dataset for [from_season, to_season].

    Trailing features are computed from a team's entire stored pace history
    regardless of this range, same reasoning as `xdrives.build_dataset`.
    `half_life_games=None` reuses xDrives' own tuned half-life
    (`RECENCY_LAMBDA`); pass `math.inf` for a flat, unweighted mean instead.
    """
    from sports_aggregator.cfb.team_game_pace import METRIC_VERSION as PACE_VERSION
    from sports_aggregator.cfb.team_game_pace import initialize as initialize_pace

    lam = RECENCY_LAMBDA if half_life_games is None else (
        0.0 if half_life_games in (math.inf,) else math.log(2.0) / half_life_games)

    initialize(repository)
    initialize_pace(repository)

    with closing(repository._connect()) as connection:
        pace_rows = [dict(row) for row in connection.execute("""
          SELECT a.game_id, a.team, a.opponent, a.meaningful_drives, a.scrimmage_plays,
                 a.plays_per_meaningful_drive, a.success_rate, a.explosive_rate,
                 a.first_down_rate, a.three_and_out_rate,
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
        allowed_window: deque = deque(maxlen=window)
        for row in games_for_team:
            prior_by_key[(row["game_id"], team)] = _trailing_summary(own_window, allowed_window, lam=lam)
            own_window.append(row)
            opponent_row = pace_by_key.get((row["game_id"], row["opponent"]))
            if opponent_row is not None:
                allowed_window.append(opponent_row["plays_per_meaningful_drive"])

    now = datetime.now(timezone.utc).isoformat()
    output = []
    for row in pace_rows:
        if from_season is not None and row["season"] < from_season:
            continue
        if to_season is not None and row["season"] > to_season:
            continue
        team, opponent = row["team"], row["opponent"]
        empty_prior = {"games": 0, "plays_per_drive_allowed": None, **{f: None for f in _TRAILING_FIELDS}}
        team_prior = prior_by_key.get((row["game_id"], team)) or empty_prior
        opponent_prior = prior_by_key.get((row["game_id"], opponent)) or empty_prior
        is_home = row["team"] == row["home_team"]

        output.append((
            row["game_id"], team, opponent, dataset_version, row["season"], row["week"],
            "home" if is_home else "away",
            row["plays_per_meaningful_drive"], row["scrimmage_plays"], row["meaningful_drives"],
            team_prior["games"], team_prior["plays_per_meaningful_drive"], team_prior["plays_per_drive_allowed"],
            team_prior["success_rate"], team_prior["explosive_rate"],
            team_prior["first_down_rate"], team_prior["three_and_out_rate"],
            opponent_prior["games"], opponent_prior["plays_per_meaningful_drive"],
            opponent_prior["plays_per_drive_allowed"],
            opponent_prior["success_rate"], opponent_prior["explosive_rate"],
            opponent_prior["first_down_rate"], opponent_prior["three_and_out_rate"],
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
                f"DELETE FROM cfb_xplays_dataset WHERE dataset_version=? AND {' AND '.join(clauses)}", params)
        else:
            connection.execute("DELETE FROM cfb_xplays_dataset WHERE dataset_version=?", (dataset_version,))
        connection.executemany("""INSERT INTO cfb_xplays_dataset(
          game_id,team,opponent,dataset_version,season,week,home_away,
          actual_plays_per_drive,actual_scrimmage_plays,actual_meaningful_drives,
          team_prior_games,team_prior_plays_per_drive,team_prior_plays_per_drive_allowed,
          team_prior_success_rate,team_prior_explosive_rate,
          team_prior_first_down_rate,team_prior_three_and_out_rate,
          opponent_prior_games,opponent_prior_plays_per_drive,opponent_prior_plays_per_drive_allowed,
          opponent_prior_success_rate,opponent_prior_explosive_rate,
          opponent_prior_first_down_rate,opponent_prior_three_and_out_rate,
          built_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)
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
            f"SELECT * FROM cfb_xplays_dataset WHERE {' AND '.join(clauses)}", params)]


def _mean(values: Iterable[Any]) -> float | None:
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; duplicated from xdrives.py
    deliberately (see model_validation.py's per-model accumulator precedent)
    rather than sharing a private helper across sibling modules."""
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


def evaluate_baselines(repository, *, from_season: int | None = None, to_season: int | None = None,
                       dataset_version: str = DATASET_VERSION,
                       min_prior_games: int = 1) -> dict[str, Any]:
    """MAE/RMSE/bias for plays-per-drive Baselines PA-PD, overall and by season.

    Mirrors `xdrives.evaluate_baselines`' structure and its disclosure: PA
    (league mean) and PD (regression) are fit on these same rows and are
    in-sample diagnostics, not predictive-performance claims. PB and PC use
    only strictly-prior trailing data and are leak-safe by construction.
    """
    initialize(repository)
    all_rows = _read_dataset_rows(repository, from_season=from_season, to_season=to_season,
                                  dataset_version=dataset_version)

    cold_start, rows = [], []
    for row in all_rows:
        if row["team_prior_games"] < min_prior_games or row["actual_plays_per_drive"] is None:
            cold_start.append(row)
        else:
            rows.append(row)
    league_mean = _mean(r["actual_plays_per_drive"] for r in rows)

    feature_keys = ("team_prior_plays_per_drive", "opponent_prior_plays_per_drive_allowed",
                    "team_prior_success_rate", "opponent_prior_three_and_out_rate")
    ols_rows = [r for r in rows if all(r[key] is not None for key in feature_keys)]
    coefficients = _fit_ols(ols_rows, feature_keys, "actual_plays_per_drive", l2=1.0) if ols_rows else None

    def _score(scope_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        accumulators = {label: _accumulator() for label in "ABCD"}
        for row in scope_rows:
            actual = float(row["actual_plays_per_drive"])
            if league_mean is not None:
                _add(accumulators["A"], league_mean, actual)
            if row["team_prior_plays_per_drive"] is not None:
                _add(accumulators["B"], row["team_prior_plays_per_drive"], actual)
            if (row["team_prior_plays_per_drive"] is not None
                    and row["opponent_prior_plays_per_drive_allowed"] is not None):
                predicted_c = (row["team_prior_plays_per_drive"]
                              + row["opponent_prior_plays_per_drive_allowed"]) / 2
                _add(accumulators["C"], predicted_c, actual)
            if coefficients is not None and all(row[key] is not None for key in feature_keys):
                predicted_d = coefficients[0] + sum(
                    c * row[key] for c, key in zip(coefficients[1:], feature_keys))
                _add(accumulators["D"], predicted_d, actual)
        return {
            "A_league_average": _finish(accumulators["A"]),
            "B_team_average": _finish(accumulators["B"]),
            "C_team_and_opponent_allowed_blend": _finish(accumulators["C"]),
            "D_linear_regression": _finish(accumulators["D"]),
        }

    by_season_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_season_rows[int(row["season"])].append(row)

    return {
        "dataset_version": dataset_version,
        "from_season": from_season,
        "to_season": to_season,
        "rows_evaluated": len(rows),
        "rows_dropped_cold_start": len(cold_start),
        "league_average_plays_per_drive": round(league_mean, 4) if league_mean is not None else None,
        "baseline_d_features": list(feature_keys),
        "baseline_d_coefficients": coefficients,
        "overall": _score(rows),
        "by_season": {str(season): _score(season_rows) for season, season_rows in sorted(by_season_rows.items())},
        "evaluation_note": (
            "Baseline A's league mean and Baseline D's regression are both fit on these same "
            "rows, so both are in-sample diagnostics, not out-of-sample model performance. "
            "Baselines B and C use only strictly-prior trailing data per row and are already "
            "leak-safe by construction."
        ),
    }


def evaluate_expected_plays(repository, *, from_season: int | None = None, to_season: int | None = None,
                            xdrives_dataset_version: str | None = None,
                            xplays_dataset_version: str = DATASET_VERSION,
                            min_prior_games: int = 1) -> dict[str, Any]:
    """Backtest `xDrives x xPlaysPerDrive` (spec section 18) against actual scrimmage plays.

    Joins `cfb_xdrives_dataset` and `cfb_xplays_dataset` on (game_id, team).
    Both sides use their own Baseline C (team-and-opponent-allowed blend) --
    the one baseline each dataset's own holdout testing found competitive
    with more complex alternatives -- rather than introducing a third,
    untested combination rule.
    """
    from sports_aggregator.cfb.xdrives import DATASET_VERSION as XDRIVES_DATASET_VERSION
    from sports_aggregator.cfb.xdrives import initialize as initialize_xdrives

    initialize(repository)
    initialize_xdrives(repository)
    xdrives_version = xdrives_dataset_version or XDRIVES_DATASET_VERSION
    xplays_version = xplays_dataset_version

    drive_clauses = ["dataset_version=?"]
    drive_params: list[Any] = [xdrives_version]
    if from_season is not None:
        drive_clauses.append("season>=?"); drive_params.append(int(from_season))
    if to_season is not None:
        drive_clauses.append("season<=?"); drive_params.append(int(to_season))
    with closing(repository._connect()) as connection:
        drive_rows = {
            (row["game_id"], row["team"]): dict(row)
            for row in connection.execute(
                f"SELECT game_id,team,team_prior_drives,opponent_prior_drives_allowed "
                f"FROM cfb_xdrives_dataset WHERE {' AND '.join(drive_clauses)}", drive_params)
        }

    play_rows = _read_dataset_rows(repository, from_season=from_season, to_season=to_season,
                                   dataset_version=xplays_version)

    league_plays_per_drive = _mean(
        r["actual_plays_per_drive"] for r in play_rows
        if r["team_prior_games"] >= min_prior_games and r["actual_plays_per_drive"] is not None)
    league_drives = _mean(
        drive_rows.get((r["game_id"], r["team"]), {}).get("team_prior_drives")
        for r in play_rows if r["team_prior_games"] >= min_prior_games
    )

    naive = _accumulator()
    combined = _accumulator()
    matched = 0
    dropped_no_drives_row = 0
    for row in play_rows:
        if row["team_prior_games"] < min_prior_games or row["actual_scrimmage_plays"] is None:
            continue
        drives_row = drive_rows.get((row["game_id"], row["team"]))
        if drives_row is None:
            dropped_no_drives_row += 1
            continue
        actual = float(row["actual_scrimmage_plays"])

        if league_plays_per_drive is not None and league_drives is not None:
            _add(naive, league_drives * league_plays_per_drive, actual)

        team_drives, opp_drives_allowed = drives_row["team_prior_drives"], drives_row["opponent_prior_drives_allowed"]
        team_ppd, opp_ppd_allowed = row["team_prior_plays_per_drive"], row["opponent_prior_plays_per_drive_allowed"]
        if None in (team_drives, opp_drives_allowed, team_ppd, opp_ppd_allowed):
            continue
        predicted_drives = (team_drives + opp_drives_allowed) / 2
        predicted_plays_per_drive = (team_ppd + opp_ppd_allowed) / 2
        _add(combined, predicted_drives * predicted_plays_per_drive, actual)
        matched += 1

    return {
        "from_season": from_season,
        "to_season": to_season,
        "xdrives_dataset_version": xdrives_version,
        "xplays_dataset_version": xplays_version,
        "rows_matched": matched,
        "rows_dropped_no_xdrives_row": dropped_no_drives_row,
        "naive_constant": {
            "league_drives": round(league_drives, 4) if league_drives is not None else None,
            "league_plays_per_drive": round(league_plays_per_drive, 4) if league_plays_per_drive is not None else None,
            **_finish(naive),
        },
        "combined_matchup_blend": _finish(combined),
        "evaluation_note": (
            "'naive_constant' predicts every team-game with the same league-wide drives x "
            "plays-per-drive product; 'combined_matchup_blend' uses each side's own Baseline-C "
            "style opponent-adjusted blend, multiplied. Both are in-sample here (no train/test "
            "season split) -- this checks whether the combination is more than the sum of "
            "coincidence, not final predictive performance; see xdrives.evaluate_advanced_model "
            "for how to add a genuine holdout if this combination is promoted further."
        ),
    }
