"""Leak-safe expected run/pass volume and Expected Dropbacks/Rush Attempts (Milestone 6).

xDrives answers "how many possessions", xPlaysPerDrive "how many plays per
possession"; this answers "how many of those plays are dropbacks versus
rush attempts" -- spec section 19 -- so all three combine into Expected
Dropbacks and Expected Rush Attempts without ever multiplying a team's
season-long pass percentage straight through a play count.

Spec section 19 is explicit that game state should shift the expectation,
not just the two teams' identities: "Do not simply multiply overall pass
percentage by projected plays." This module tests that directly rather than
assuming it -- Baseline D includes each team's own market-implied game
script (a signed point spread) alongside its neutral-situation pass rate,
and whether that spread term earns its keep is reported, not asserted.

Built the same leak-safe, recency-weighted way `xdrives.py` and `xplays.py`
walk `cfb_team_game_pace`, reusing xDrives' already-tuned half-life rather
than deriving a new one (same reasoning as `xplays.py`).

Scope note: after xDrives' environment term and shrinkage each failed a
genuine holdout check (`docs/CFB_XDRIVES.md` section 7), and `xplays.py`
deliberately did not re-run that experiment without evidence it was needed,
this module ships with in-sample baselines only (mirroring `xplays.py`'s own
scope decision) -- a genuine train/test holdout should be added, the same
way `xdrives.evaluate_advanced_model` does it, before any of this feeds a
served projection.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Sequence

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.xdrives import RECENCY_LAMBDA, TRAILING_WINDOW_GAMES

DATASET_VERSION = "xvolume-dataset-v1"

_TRAILING_FIELDS = ("pass_rate", "neutral_pass_rate")


@schema_once("xvolume")
def initialize(repository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_xvolume_dataset (
          game_id INTEGER NOT NULL,
          team TEXT NOT NULL,
          opponent TEXT NOT NULL,
          dataset_version TEXT NOT NULL,
          season INTEGER NOT NULL,
          week INTEGER NOT NULL,
          home_away TEXT NOT NULL,

          actual_pass_rate REAL,
          actual_pass_attempts INTEGER,
          actual_rush_attempts INTEGER,
          actual_scrimmage_plays INTEGER,

          team_prior_games INTEGER NOT NULL,
          team_prior_pass_rate REAL,
          team_prior_neutral_pass_rate REAL,
          team_prior_pass_rate_allowed REAL,

          opponent_prior_games INTEGER NOT NULL,
          opponent_prior_pass_rate REAL,
          opponent_prior_neutral_pass_rate REAL,
          opponent_prior_pass_rate_allowed REAL,

          team_elo INTEGER,
          opponent_elo INTEGER,
          market_spread REAL,

          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,dataset_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_xvolume_dataset_team
          ON cfb_xvolume_dataset(team,dataset_version,season,week);
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
    summary["pass_rate_allowed"] = _weighted_mean(allowed_weights, allowed_window)
    return summary


def build_dataset(repository, *, from_season: int | None = None, to_season: int | None = None,
                  dataset_version: str = DATASET_VERSION,
                  window: int = TRAILING_WINDOW_GAMES,
                  half_life_games: float | None = None) -> dict[str, Any]:
    """Rebuild the leak-safe run/pass volume dataset for [from_season, to_season].

    Same construction as `xplays.build_dataset`: trailing features read a
    team's entire stored history regardless of this range, but only rows
    whose own game falls in it are written.
    """
    from sports_aggregator.cfb.lines import initialize as initialize_lines
    from sports_aggregator.cfb.team_game_pace import METRIC_VERSION as PACE_VERSION
    from sports_aggregator.cfb.team_game_pace import initialize as initialize_pace

    lam = RECENCY_LAMBDA if half_life_games is None else (
        0.0 if half_life_games in (math.inf,) else math.log(2.0) / half_life_games)

    initialize(repository)
    initialize_pace(repository)
    initialize_lines(repository)

    with closing(repository._connect()) as connection:
        pace_rows = [dict(row) for row in connection.execute("""
          SELECT a.game_id, a.team, a.opponent, a.pass_rate, a.neutral_pass_rate,
                 a.pass_plays, a.rush_plays, a.scrimmage_plays,
                 g.season, g.week, g.start_date, g.home_team, g.away_team,
                 g.home_pregame_elo, g.away_pregame_elo
          FROM cfb_team_game_pace a JOIN games g ON g.game_id=a.game_id
          WHERE a.metric_version=?
          ORDER BY a.team, g.start_date
        """, (PACE_VERSION,)).fetchall()]

        game_ids = {row["game_id"] for row in pace_rows}
        lines_by_game: dict[int, float | None] = {}
        if game_ids:
            placeholders = ",".join("?" for _ in game_ids)
            for row in connection.execute(
                f"SELECT game_id, AVG(spread) AS spread FROM game_lines WHERE game_id IN ({placeholders}) "
                f"GROUP BY game_id", list(game_ids),
            ):
                lines_by_game[int(row["game_id"])] = row["spread"]

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
                allowed_window.append(opponent_row["pass_rate"])

    now = datetime.now(timezone.utc).isoformat()
    output = []
    for row in pace_rows:
        if from_season is not None and row["season"] < from_season:
            continue
        if to_season is not None and row["season"] > to_season:
            continue
        team, opponent = row["team"], row["opponent"]
        empty_prior = {"games": 0, "pass_rate_allowed": None, **{f: None for f in _TRAILING_FIELDS}}
        team_prior = prior_by_key.get((row["game_id"], team)) or empty_prior
        opponent_prior = prior_by_key.get((row["game_id"], opponent)) or empty_prior
        is_home = row["team"] == row["home_team"]
        team_elo = row["home_pregame_elo"] if is_home else row["away_pregame_elo"]
        opponent_elo = row["away_pregame_elo"] if is_home else row["home_pregame_elo"]

        spread = lines_by_game.get(row["game_id"])
        # Home-relative -> this team's own signed line, same convention as
        # xdrives.py: negative means this team is favored.
        team_spread = (spread if is_home else -spread) if spread is not None else None

        output.append((
            row["game_id"], team, opponent, dataset_version, row["season"], row["week"],
            "home" if is_home else "away",
            row["pass_rate"], row["pass_plays"], row["rush_plays"], row["scrimmage_plays"],
            team_prior["games"], team_prior["pass_rate"], team_prior["neutral_pass_rate"],
            team_prior["pass_rate_allowed"],
            opponent_prior["games"], opponent_prior["pass_rate"], opponent_prior["neutral_pass_rate"],
            opponent_prior["pass_rate_allowed"],
            team_elo, opponent_elo, team_spread,
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
                f"DELETE FROM cfb_xvolume_dataset WHERE dataset_version=? AND {' AND '.join(clauses)}", params)
        else:
            connection.execute("DELETE FROM cfb_xvolume_dataset WHERE dataset_version=?", (dataset_version,))
        connection.executemany("""INSERT INTO cfb_xvolume_dataset(
          game_id,team,opponent,dataset_version,season,week,home_away,
          actual_pass_rate,actual_pass_attempts,actual_rush_attempts,actual_scrimmage_plays,
          team_prior_games,team_prior_pass_rate,team_prior_neutral_pass_rate,team_prior_pass_rate_allowed,
          opponent_prior_games,opponent_prior_pass_rate,opponent_prior_neutral_pass_rate,
          opponent_prior_pass_rate_allowed,
          team_elo,opponent_elo,market_spread,built_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)
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
            f"SELECT * FROM cfb_xvolume_dataset WHERE {' AND '.join(clauses)}", params)]


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


#: Baseline D's features: opponent-adjusted identity plus this team's own
#: signed game script. The whole point of Baseline D is to check whether
#: `market_spread` earns a place here -- see `baseline_d_coefficients` and
#: the module docstring.
_BASELINE_D_FEATURES = ("team_prior_neutral_pass_rate", "opponent_prior_pass_rate_allowed", "market_spread")


def evaluate_baselines(repository, *, from_season: int | None = None, to_season: int | None = None,
                       dataset_version: str = DATASET_VERSION,
                       min_prior_games: int = 1) -> dict[str, Any]:
    """MAE/RMSE/bias for pass-rate Baselines A-D, overall and by season.

    A (league mean) and D (regression) are in-sample diagnostics, same
    disclosure as `xdrives.evaluate_baselines` and `xplays.evaluate_baselines`.
    B and C use only strictly-prior trailing data and are leak-safe by
    construction.
    """
    initialize(repository)
    all_rows = _read_dataset_rows(repository, from_season=from_season, to_season=to_season,
                                  dataset_version=dataset_version)

    cold_start, rows = [], []
    for row in all_rows:
        if row["team_prior_games"] < min_prior_games or row["actual_pass_rate"] is None:
            cold_start.append(row)
        else:
            rows.append(row)
    league_mean = _mean(r["actual_pass_rate"] for r in rows)

    ols_rows = [r for r in rows if all(r[key] is not None for key in _BASELINE_D_FEATURES)]
    coefficients = _fit_ols(ols_rows, _BASELINE_D_FEATURES, "actual_pass_rate", l2=1.0) if ols_rows else None

    def _score(scope_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        accumulators = {label: _accumulator() for label in "ABCD"}
        for row in scope_rows:
            actual = float(row["actual_pass_rate"])
            if league_mean is not None:
                _add(accumulators["A"], league_mean, actual)
            if row["team_prior_pass_rate"] is not None:
                _add(accumulators["B"], row["team_prior_pass_rate"], actual)
            if row["team_prior_pass_rate"] is not None and row["opponent_prior_pass_rate_allowed"] is not None:
                predicted_c = (row["team_prior_pass_rate"] + row["opponent_prior_pass_rate_allowed"]) / 2
                _add(accumulators["C"], predicted_c, actual)
            if coefficients is not None and all(row[key] is not None for key in _BASELINE_D_FEATURES):
                predicted_d = coefficients[0] + sum(
                    c * row[key] for c, key in zip(coefficients[1:], _BASELINE_D_FEATURES))
                _add(accumulators["D"], predicted_d, actual)
        return {
            "A_league_average": _finish(accumulators["A"]),
            "B_team_average": _finish(accumulators["B"]),
            "C_team_and_opponent_allowed_blend": _finish(accumulators["C"]),
            "D_linear_regression_with_game_script": _finish(accumulators["D"]),
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
        "league_average_pass_rate": round(league_mean, 4) if league_mean is not None else None,
        "baseline_d_features": list(_BASELINE_D_FEATURES),
        "baseline_d_coefficients": coefficients,
        "overall": _score(rows),
        "by_season": {str(season): _score(season_rows) for season, season_rows in sorted(by_season_rows.items())},
        "evaluation_note": (
            "Baseline A's league mean and Baseline D's regression are both fit on these same "
            "rows, so both are in-sample diagnostics, not out-of-sample model performance. "
            "Baselines B and C use only strictly-prior trailing data per row and are already "
            "leak-safe by construction. Baseline D's market_spread coefficient sign/magnitude is "
            "the direct check on spec section 19's claim that game state should shift pass rate: "
            "a negative coefficient (favorites -- negative signed spread -- pass less) is the "
            "expected direction if the effect is real."
        ),
    }


def evaluate_expected_volume(repository, *, from_season: int | None = None, to_season: int | None = None,
                             xdrives_dataset_version: str | None = None,
                             xplays_dataset_version: str | None = None,
                             xvolume_dataset_version: str = DATASET_VERSION,
                             min_prior_games: int = 1) -> dict[str, Any]:
    """Backtest Expected Dropbacks/Rush Attempts against actual attempts.

    Chains all three layers' own Baseline C: `xDrives x xPlaysPerDrive x
    xPassRate` predicts total plays and splits them by predicted pass rate,
    compared against actual pass/rush attempts. In-sample only, same
    disclosure as `xplays.evaluate_expected_plays`.
    """
    from sports_aggregator.cfb.xdrives import DATASET_VERSION as XDRIVES_DATASET_VERSION
    from sports_aggregator.cfb.xdrives import initialize as initialize_xdrives
    from sports_aggregator.cfb.xplays import DATASET_VERSION as XPLAYS_DATASET_VERSION
    from sports_aggregator.cfb.xplays import initialize as initialize_xplays

    initialize(repository)
    initialize_xdrives(repository)
    initialize_xplays(repository)
    xdrives_version = xdrives_dataset_version or XDRIVES_DATASET_VERSION
    xplays_version = xplays_dataset_version or XPLAYS_DATASET_VERSION

    def _joined(table: str, version: str, columns: str) -> dict[tuple[int, str], dict[str, Any]]:
        clauses = ["dataset_version=?"]
        params: list[Any] = [version]
        if from_season is not None:
            clauses.append("season>=?"); params.append(int(from_season))
        if to_season is not None:
            clauses.append("season<=?"); params.append(int(to_season))
        with closing(repository._connect()) as connection:
            return {
                (row["game_id"], row["team"]): dict(row)
                for row in connection.execute(
                    f"SELECT game_id,team,{columns} FROM {table} WHERE {' AND '.join(clauses)}", params)
            }

    drive_rows = _joined("cfb_xdrives_dataset", xdrives_version,
                        "team_prior_drives,opponent_prior_drives_allowed")
    play_rows = _joined("cfb_xplays_dataset", xplays_version,
                       "team_prior_plays_per_drive,opponent_prior_plays_per_drive_allowed")
    volume_rows = _read_dataset_rows(repository, from_season=from_season, to_season=to_season,
                                     dataset_version=xvolume_dataset_version)

    eligible = [row for row in volume_rows if row["team_prior_games"] >= min_prior_games
               and row["actual_pass_attempts"] is not None and row["actual_rush_attempts"] is not None]
    league_pass_rate = _mean(r["actual_pass_rate"] for r in eligible)
    league_drives = _mean(drive_rows.get((r["game_id"], r["team"]), {}).get("team_prior_drives") for r in eligible)
    league_plays_per_drive = _mean(
        play_rows.get((r["game_id"], r["team"]), {}).get("team_prior_plays_per_drive") for r in eligible)

    naive_pass, naive_rush = _accumulator(), _accumulator()
    combined_pass, combined_rush = _accumulator(), _accumulator()
    matched = 0
    for row in eligible:
        key = (row["game_id"], row["team"])
        actual_pass, actual_rush = float(row["actual_pass_attempts"]), float(row["actual_rush_attempts"])

        if None not in (league_drives, league_plays_per_drive, league_pass_rate):
            naive_plays = league_drives * league_plays_per_drive
            _add(naive_pass, naive_plays * league_pass_rate, actual_pass)
            _add(naive_rush, naive_plays * (1 - league_pass_rate), actual_rush)

        drives_row, plays_row = drive_rows.get(key), play_rows.get(key)
        if drives_row is None or plays_row is None:
            continue
        team_drives, opp_drives_allowed = drives_row.get("team_prior_drives"), drives_row.get("opponent_prior_drives_allowed")
        team_ppd, opp_ppd_allowed = plays_row.get("team_prior_plays_per_drive"), plays_row.get("opponent_prior_plays_per_drive_allowed")
        team_pr, opp_pr_allowed = row["team_prior_pass_rate"], row["opponent_prior_pass_rate_allowed"]
        if None in (team_drives, opp_drives_allowed, team_ppd, opp_ppd_allowed, team_pr, opp_pr_allowed):
            continue
        predicted_drives = (team_drives + opp_drives_allowed) / 2
        predicted_ppd = (team_ppd + opp_ppd_allowed) / 2
        predicted_pass_rate = (team_pr + opp_pr_allowed) / 2
        predicted_plays = predicted_drives * predicted_ppd
        _add(combined_pass, predicted_plays * predicted_pass_rate, actual_pass)
        _add(combined_rush, predicted_plays * (1 - predicted_pass_rate), actual_rush)
        matched += 1

    return {
        "from_season": from_season,
        "to_season": to_season,
        "rows_matched": matched,
        "naive_constant": {
            "expected_dropbacks": _finish(naive_pass),
            "expected_rush_attempts": _finish(naive_rush),
        },
        "combined_matchup_blend": {
            "expected_dropbacks": _finish(combined_pass),
            "expected_rush_attempts": _finish(combined_rush),
        },
        "evaluation_note": (
            "'naive_constant' applies the same league-wide drives x plays-per-drive x pass-rate "
            "product to every team-game; 'combined_matchup_blend' chains each layer's own "
            "opponent-adjusted Baseline C. In-sample only (no train/test season split) -- this "
            "checks whether the three-layer chain is coherent, not final predictive performance."
        ),
    }
