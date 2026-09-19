"""Leak-safe team-game drive dataset, and baselines for how hard xDrives is.

`team_game_pace.py` stores what actually happened. This module builds what a
model would have been allowed to know beforehand: for every team-game, each
trailing feature is the mean of that team's own `cfb_team_game_pace` rows
strictly before this game, walked in chronological order by
`games.start_date`. Nothing about the game being featured, or any game after
it, ever enters its own average -- see spec section 29 ("data leakage") in
the project brief this module implements. The actual outcome is stored
alongside the leak-safe features so the two can be compared, not blended.

Two rolling series are kept per team: its own trailing offense (how many
drives *it* gets, how it plays), and its trailing "allowed" figure (how many
drives its *opponents* have gotten against it) -- the second is what
Baseline C needs and is a defensive pace signal in its own right.

The trailing window (last `TRAILING_WINDOW_GAMES` games) is exponentially
recency-weighted (spec section 27): a game `n` games back carries weight
`exp(-lambda * n)`, with `lambda` set from `RECENCY_HALF_LIFE_GAMES` so a
game that far back counts for half of yesterday's. This exists because an
earlier out-of-sample check (training on 2022-2024, testing on 2025) found
every baseline under-predicted 2025 drives by a consistent margin -- a level
shift a flat multi-year average cannot see coming, which decay at least
partially tracks. Full Bayesian shrinkage toward a preseason prior (spec
section 28) is still not implemented here; a team's most recent prior-season
games already count toward week 1 of a new season (just lightly, at that
much decay), so cold starts are rare, not absolute -- `team_prior_games`
records exactly how much history backed each row so `evaluate_baselines`
can exclude true cold starts and report how many it dropped.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from typing import Any, Callable, Iterable, Sequence

from sports_aggregator.cfb.repository import schema_once

#: v2 recency-weights the trailing window (see module docstring); v1's flat
#: mean is left in place under its own version so the two can be compared
#: directly by passing `dataset_version="xdrives-dataset-v1"` to any reader.
DATASET_VERSION = "xdrives-dataset-v2"
TRAILING_WINDOW_GAMES = 12
#: A game this many appearances back in the trailing window counts half as
#: much as the most recent one. Chosen, not fit: roughly a third of a season,
#: short enough to react to an early-season identity change, long enough that
#: a single game is never more than a fifth or so of the average.
RECENCY_HALF_LIFE_GAMES = 8.0
RECENCY_LAMBDA = math.log(2.0) / RECENCY_HALF_LIFE_GAMES

#: The leaguewide "current era" pace level (spec section 6), separate from
#: any one team's own trailing history: an out-of-sample check (train
#: 2022-2024, test 2025) found every team-level baseline under-predicting
#: 2025 drives by a consistent margin even after recency-weighting each
#: team's own window -- a level shift every team was behind on at once,
#: which no team-specific window can see coming. This walks every stored
#: team-game observation across the whole league in calendar order instead
#: of one team's games, so it reacts to a leaguewide trend a team's own
#: schedule (one game a week) is too sparse to reveal quickly.
#: About two weeks of the full FBS slate (~130 teams x 1 game x 2 sides per
#: week) -- long enough that a single day's results do not swing it, short
#: enough to move within a season rather than only across ones.
LEAGUE_WINDOW_GAMES = 1500
LEAGUE_RECENCY_HALF_LIFE_GAMES = 400.0
LEAGUE_LAMBDA = math.log(2.0) / LEAGUE_RECENCY_HALF_LIFE_GAMES
#: Chosen, not fit: how much a hand-picked (non-regression) environment
#: baseline leans on the team-matchup blend versus the current league level.
ENV_BLEND_TEAM_WEIGHT = 0.7

#: Bayesian shrinkage toward the league level (spec section 28), applied to
#: the two trailing drive-count fields Baseline C blends. Standard
#: empirical-Bayes form: weight = games / (games + pseudo_games), so a team
#: with zero trailing games gets the league prior outright (a well-defined
#: prediction for a true week-1 cold start, instead of nothing), a team with
#: `SHRINKAGE_PSEUDO_GAMES` worth of history is trusted half as much as the
#: league prior, and trust keeps climbing but never fully reaches 1 -- a
#: persistently fast or slow team's own identity is real, not noise, so it
#: should not be shrunk away entirely even with a full trailing window.
#: Chosen, not fit, to avoid tuning a hyperparameter against the same
#: holdouts it gets judged on.
SHRINKAGE_PSEUDO_GAMES = 4.0

ADVANCED_MODEL_VERSION = "xdrives-advanced-v1"

#: Pace, opponent quality, market context and the league environment
#: together, per Milestone 4 of the project brief -- deliberately not every
#: stored trailing feature, to keep an 11-feature OLS numerically
#: well-behaved without numpy. `elo_diff` and `is_home` are derived at
#: fit/predict time by `_feature_value` rather than stored as their own
#: dataset columns; `league_prior_drives` is a stored column.
ADVANCED_FEATURES = (
    "team_prior_drives",
    "opponent_prior_drives_allowed",
    "league_prior_drives",
    "team_prior_seconds_per_play",
    "opponent_prior_seconds_per_play",
    "team_prior_pass_rate",
    "team_prior_success_rate",
    "opponent_prior_success_rate",
    "elo_diff",
    "market_total",
    "is_home",
)
#: Ridge penalty on the advanced model's non-intercept coefficients. Several
#: of these features move together (a fast team's own tempo correlates with
#: its opponent's), and a handful of seasons of games is not a lot of rows
#: for a 10-feature fit -- a small L2 keeps coefficients from blowing up on
#: that collinearity instead of needing to drop features by hand.
ADVANCED_MODEL_L2 = 1.0


@schema_once("xdrives")
def initialize(repository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_xdrives_dataset (
          game_id INTEGER NOT NULL,
          team TEXT NOT NULL,
          opponent TEXT NOT NULL,
          dataset_version TEXT NOT NULL,
          season INTEGER NOT NULL,
          week INTEGER NOT NULL,
          home_away TEXT NOT NULL,

          actual_meaningful_drives INTEGER,
          actual_game_total_drives INTEGER,

          team_prior_games INTEGER NOT NULL,
          team_prior_drives REAL,
          team_prior_drives_allowed REAL,
          team_prior_seconds_per_play REAL,
          team_prior_neutral_seconds_per_play REAL,
          team_prior_pass_rate REAL,
          team_prior_neutral_pass_rate REAL,
          team_prior_success_rate REAL,
          team_prior_explosive_rate REAL,
          team_prior_first_down_rate REAL,
          team_prior_three_and_out_rate REAL,

          opponent_prior_games INTEGER NOT NULL,
          opponent_prior_drives REAL,
          opponent_prior_drives_allowed REAL,
          opponent_prior_seconds_per_play REAL,
          opponent_prior_neutral_seconds_per_play REAL,
          opponent_prior_pass_rate REAL,
          opponent_prior_neutral_pass_rate REAL,
          opponent_prior_success_rate REAL,
          opponent_prior_explosive_rate REAL,
          opponent_prior_first_down_rate REAL,
          opponent_prior_three_and_out_rate REAL,

          team_elo INTEGER,
          opponent_elo INTEGER,
          market_spread REAL,
          market_total REAL,
          team_implied_points REAL,
          opponent_implied_points REAL,

          league_prior_games INTEGER,
          league_prior_drives REAL,

          team_prior_drives_shrunk REAL,
          team_prior_drives_allowed_shrunk REAL,
          opponent_prior_drives_shrunk REAL,
          opponent_prior_drives_allowed_shrunk REAL,

          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,dataset_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_xdrives_dataset_team
          ON cfb_xdrives_dataset(team,dataset_version,season,week);

        CREATE TABLE IF NOT EXISTS cfb_xdrives_model (
          model_version TEXT PRIMARY KEY,
          feature_json TEXT NOT NULL,
          coefficients_json TEXT,
          l2 REAL NOT NULL,
          from_season INTEGER,
          to_season INTEGER,
          training_rows INTEGER NOT NULL,
          fitted_at TEXT NOT NULL
        );
        """)
        # `cfb_xdrives_dataset` may already exist from before the league
        # environment term (spec section 6) was added -- CREATE TABLE IF NOT
        # EXISTS is a no-op against it, so the new columns need an explicit
        # migration the same way `play_detail.py` backfills its own additions.
        existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(cfb_xdrives_dataset)")}
        for column, sql_type in (
            ("league_prior_games", "INTEGER"), ("league_prior_drives", "REAL"),
            ("team_prior_drives_shrunk", "REAL"), ("team_prior_drives_allowed_shrunk", "REAL"),
            ("opponent_prior_drives_shrunk", "REAL"), ("opponent_prior_drives_allowed_shrunk", "REAL"),
        ):
            if column not in existing:
                connection.execute(f"ALTER TABLE cfb_xdrives_dataset ADD COLUMN {column} {sql_type}")
        connection.commit()


def _mean(values: Iterable[Any]) -> float | None:
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _decay_weights(count: int, lam: float) -> list[float]:
    """Weight for each slot in an oldest-to-newest window of `count` games.

    Index 0 (oldest) is farthest back; the last index (most recent prior
    game) gets weight 1.0, and each step further back is discounted by
    `exp(-lam)` per game -- `weight = exp(-lambda * gamesAgo)` from spec
    section 27, with `gamesAgo` counted from the most recent game.
    """
    return [math.exp(-lam * (count - 1 - i)) for i in range(count)]


def _weighted_mean(weights: Sequence[float], values: Iterable[Any]) -> float | None:
    pairs = [(w, float(v)) for w, v in zip(weights, values) if v is not None]
    total_weight = sum(w for w, _ in pairs)
    if not pairs or total_weight <= 0:
        return None
    return sum(w * v for w, v in pairs) / total_weight


def _shrink(trailing_value: float | None, trailing_games: int, prior_value: float | None, *,
           pseudo_games: float = SHRINKAGE_PSEUDO_GAMES) -> float | None:
    """Empirical-Bayes blend of a team's own trailing value toward a prior.

    `trailing_games` is the sample size *behind* `trailing_value`, not a
    row's overall history -- a team's own drives and its own drives-allowed
    windows fill at the same rate, so one games count covers both.
    """
    if prior_value is None:
        return trailing_value
    if trailing_games <= 0 or trailing_value is None:
        return prior_value
    weight = trailing_games / (trailing_games + pseudo_games)
    return weight * trailing_value + (1 - weight) * prior_value


_TRAILING_RATE_FIELDS = (
    "seconds_per_play", "neutral_seconds_per_play", "pass_rate", "neutral_pass_rate",
    "success_rate", "explosive_rate", "first_down_rate", "three_and_out_rate",
)


def _trailing_summary(own_window: deque, allowed_window: deque, *,
                      lam: float = RECENCY_LAMBDA) -> dict[str, Any]:
    own_weights = _decay_weights(len(own_window), lam)
    allowed_weights = _decay_weights(len(allowed_window), lam)
    summary = {
        "games": len(own_window),
        "drives": _weighted_mean(own_weights, (row["meaningful_drives"] for row in own_window)),
        "drives_allowed": _weighted_mean(allowed_weights, allowed_window),
    }
    for field in _TRAILING_RATE_FIELDS:
        summary[field] = _weighted_mean(own_weights, (row[field] for row in own_window))
    return summary


def build_dataset(repository, *, from_season: int | None = None, to_season: int | None = None,
                  dataset_version: str = DATASET_VERSION,
                  window: int = TRAILING_WINDOW_GAMES,
                  half_life_games: float = RECENCY_HALF_LIFE_GAMES) -> dict[str, Any]:
    """Rebuild the leak-safe dataset for games in [from_season, to_season].

    Trailing features are computed from a team's entire stored pace history
    regardless of this range -- a week 1 game in `from_season` still needs
    last season's tail to avoid a needless cold start -- but only rows whose
    own game falls in range are written. `half_life_games` controls how fast
    the trailing window's exponential decay forgets older games; pass
    `math.inf` to recover a flat, unweighted mean.
    """
    lam = math.log(2.0) / half_life_games if half_life_games not in (None, math.inf) else 0.0
    from sports_aggregator.cfb.lines import initialize as initialize_lines
    from sports_aggregator.cfb.team_game_pace import METRIC_VERSION as PACE_VERSION
    from sports_aggregator.cfb.team_game_pace import initialize as initialize_pace

    initialize(repository)
    initialize_pace(repository)
    initialize_lines(repository)

    with closing(repository._connect()) as connection:
        pace_rows = [dict(row) for row in connection.execute("""
          SELECT a.game_id, a.team, a.opponent, a.meaningful_drives, a.seconds_per_play,
                 a.neutral_seconds_per_play, a.pass_rate, a.neutral_pass_rate, a.success_rate,
                 a.explosive_rate, a.first_down_rate, a.three_and_out_rate,
                 g.season, g.week, g.start_date, g.home_team, g.away_team,
                 g.home_pregame_elo, g.away_pregame_elo
          FROM cfb_team_game_pace a JOIN games g ON g.game_id=a.game_id
          WHERE a.metric_version=?
          ORDER BY a.team, g.start_date
        """, (PACE_VERSION,)).fetchall()]

        game_ids = {row["game_id"] for row in pace_rows}
        lines_by_game: dict[int, dict[str, float | None]] = {}
        if game_ids:
            placeholders = ",".join("?" for _ in game_ids)
            for row in connection.execute(
                f"""SELECT game_id, AVG(spread) AS spread, AVG(over_under) AS total
                    FROM game_lines WHERE game_id IN ({placeholders}) GROUP BY game_id""",
                list(game_ids),
            ):
                lines_by_game[int(row["game_id"])] = {"spread": row["spread"], "total": row["total"]}

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
                allowed_window.append(opponent_row["meaningful_drives"])

    # Leaguewide current-era level (spec section 6), walked once across every
    # team-game observation in calendar order rather than per team. Games on
    # the same date are processed as a batch: every row from that date reads
    # the window as it stood at the end of the *previous* date, and only
    # after the whole date's rows have read it does that date's own results
    # get folded in -- so simultaneous Saturday games never inform each other.
    # (`_decay_weights` still assigns those same-date games slightly
    # different positions once they enter the window, since it weights by
    # position, not date -- a harmless imprecision given how many games share
    # a date versus `LEAGUE_WINDOW_GAMES`, and never a leak: it only affects
    # how much a past date's games are weighted for a *later* one.)
    league_window: deque = deque(maxlen=LEAGUE_WINDOW_GAMES)
    league_prior_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pace_rows:
        by_date[row["start_date"]].append(row)
    for date in sorted(by_date):
        date_rows = by_date[date]
        summary = {
            "games": len(league_window),
            "drives": _weighted_mean(_decay_weights(len(league_window), LEAGUE_LAMBDA), league_window),
        }
        for row in date_rows:
            league_prior_by_key[(row["game_id"], row["team"])] = summary
        league_window.extend(row["meaningful_drives"] for row in date_rows)

    now = datetime.now(timezone.utc).isoformat()
    output = []
    for row in pace_rows:
        if from_season is not None and row["season"] < from_season:
            continue
        if to_season is not None and row["season"] > to_season:
            continue
        team, opponent = row["team"], row["opponent"]
        team_prior = prior_by_key.get((row["game_id"], team)) or {"games": 0, "drives": None,
            "drives_allowed": None, **{f: None for f in _TRAILING_RATE_FIELDS}}
        opponent_prior = prior_by_key.get((row["game_id"], opponent)) or {"games": 0, "drives": None,
            "drives_allowed": None, **{f: None for f in _TRAILING_RATE_FIELDS}}
        opponent_pace_row = pace_by_key.get((row["game_id"], opponent))
        league_prior = league_prior_by_key.get((row["game_id"], team)) or {"games": 0, "drives": None}
        league_drives = league_prior["drives"]

        team_drives_shrunk = _shrink(team_prior["drives"], team_prior["games"], league_drives)
        team_allowed_shrunk = _shrink(team_prior["drives_allowed"], team_prior["games"], league_drives)
        opponent_drives_shrunk = _shrink(opponent_prior["drives"], opponent_prior["games"], league_drives)
        opponent_allowed_shrunk = _shrink(opponent_prior["drives_allowed"], opponent_prior["games"], league_drives)

        is_home = row["team"] == row["home_team"]
        team_elo = row["home_pregame_elo"] if is_home else row["away_pregame_elo"]
        opponent_elo = row["away_pregame_elo"] if is_home else row["home_pregame_elo"]

        lines = lines_by_game.get(row["game_id"], {})
        spread, total = lines.get("spread"), lines.get("total")
        if spread is not None and total is not None:
            # `spread` is home-relative (negative = home favored); flip the
            # sign when this team is the away side so it is always this
            # team's own line.
            team_spread = spread if is_home else -spread
            team_implied = total / 2 - team_spread / 2
            opponent_implied = total - team_implied
        else:
            team_implied = opponent_implied = None

        output.append((
            row["game_id"], team, opponent, dataset_version, row["season"], row["week"],
            "home" if is_home else "away",
            row["meaningful_drives"],
            (row["meaningful_drives"] + opponent_pace_row["meaningful_drives"]) if opponent_pace_row else None,
            team_prior["games"], team_prior["drives"], team_prior["drives_allowed"],
            team_prior["seconds_per_play"], team_prior["neutral_seconds_per_play"],
            team_prior["pass_rate"], team_prior["neutral_pass_rate"], team_prior["success_rate"],
            team_prior["explosive_rate"], team_prior["first_down_rate"], team_prior["three_and_out_rate"],
            opponent_prior["games"], opponent_prior["drives"], opponent_prior["drives_allowed"],
            opponent_prior["seconds_per_play"], opponent_prior["neutral_seconds_per_play"],
            opponent_prior["pass_rate"], opponent_prior["neutral_pass_rate"], opponent_prior["success_rate"],
            opponent_prior["explosive_rate"], opponent_prior["first_down_rate"], opponent_prior["three_and_out_rate"],
            team_elo, opponent_elo, spread, total, team_implied, opponent_implied,
            league_prior["games"], league_prior["drives"],
            team_drives_shrunk, team_allowed_shrunk, opponent_drives_shrunk, opponent_allowed_shrunk,
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
                f"DELETE FROM cfb_xdrives_dataset WHERE dataset_version=? AND {' AND '.join(clauses)}", params)
        else:
            connection.execute("DELETE FROM cfb_xdrives_dataset WHERE dataset_version=?", (dataset_version,))
        # Named columns, not positional VALUES(...): `league_prior_games`/
        # `league_prior_drives` were added to an already-existing production
        # table by ALTER TABLE, which always appends new columns at the end
        # -- after `built_at`, not before it as this file's own CREATE TABLE
        # text would suggest. A bare VALUES(...) silently maps by physical
        # column position and would misalign every value on that table.
        connection.executemany("""INSERT INTO cfb_xdrives_dataset(
          game_id,team,opponent,dataset_version,season,week,home_away,
          actual_meaningful_drives,actual_game_total_drives,
          team_prior_games,team_prior_drives,team_prior_drives_allowed,
          team_prior_seconds_per_play,team_prior_neutral_seconds_per_play,
          team_prior_pass_rate,team_prior_neutral_pass_rate,team_prior_success_rate,
          team_prior_explosive_rate,team_prior_first_down_rate,team_prior_three_and_out_rate,
          opponent_prior_games,opponent_prior_drives,opponent_prior_drives_allowed,
          opponent_prior_seconds_per_play,opponent_prior_neutral_seconds_per_play,
          opponent_prior_pass_rate,opponent_prior_neutral_pass_rate,opponent_prior_success_rate,
          opponent_prior_explosive_rate,opponent_prior_first_down_rate,opponent_prior_three_and_out_rate,
          team_elo,opponent_elo,market_spread,market_total,team_implied_points,opponent_implied_points,
          league_prior_games,league_prior_drives,
          team_prior_drives_shrunk,team_prior_drives_allowed_shrunk,
          opponent_prior_drives_shrunk,opponent_prior_drives_allowed_shrunk,
          built_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)
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
            f"SELECT * FROM cfb_xdrives_dataset WHERE {' AND '.join(clauses)}", params)]


def _feature_value(row: dict[str, Any], key: str) -> float | None:
    """A feature's value for OLS, including ones not stored as their own column."""
    if key == "elo_diff":
        if row.get("team_elo") is None or row.get("opponent_elo") is None:
            return None
        return float(row["team_elo"]) - float(row["opponent_elo"])
    if key == "is_home":
        return 1.0 if row.get("home_away") == "home" else 0.0
    value = row.get(key)
    return float(value) if value is not None else None


def _eligible_rows(rows: list[dict[str, Any]], feature_keys: tuple[str, ...], *,
                   min_prior_games: int = 1) -> list[dict[str, Any]]:
    eligible = []
    for row in rows:
        if row["team_prior_games"] < min_prior_games or row["actual_meaningful_drives"] is None:
            continue
        if any(_feature_value(row, key) is None for key in feature_keys):
            continue
        eligible.append(row)
    return eligible


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a small dense system."""
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
            l2: float = 0.0,
            value_fn: Callable[[dict[str, Any], str], float] = lambda row, key: float(row[key]),
            ) -> list[float] | None:
    """Closed-form (ridge) least squares via normal equations; no numpy at runtime.

    `l2` adds a penalty to every coefficient except the intercept -- plain
    ridge regression -- which keeps the fit stable when features are
    correlated (Baseline D's two features barely are; the advanced model's
    ten include some that move together, like a team's own tempo and its
    opponent's). `l2=0` recovers ordinary least squares exactly.
    """
    n_features = len(feature_keys) + 1
    xtx = [[0.0] * n_features for _ in range(n_features)]
    xty = [0.0] * n_features
    for row in rows:
        x = [1.0] + [value_fn(row, key) for key in feature_keys]
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


def _env_blend_prediction(row: dict[str, Any], *, team_weight: float = ENV_BLEND_TEAM_WEIGHT) -> float | None:
    """A hand-picked (not fit) blend of the team matchup and the current league level.

    Unlike Baselines B/C/D, which only ever see one team's own history,
    `league_prior_drives` is the same leaguewide signal for every row on a
    given date -- this is the simplest possible way to let a team's
    prediction react to a leaguewide pace shift its own trailing window is
    too slow (or, for a team on a bye, too stale) to reflect yet.
    """
    if (row["team_prior_drives"] is None or row["opponent_prior_drives_allowed"] is None
            or row["league_prior_drives"] is None):
        return None
    matchup = (row["team_prior_drives"] + row["opponent_prior_drives_allowed"]) / 2
    return team_weight * matchup + (1 - team_weight) * row["league_prior_drives"]


def _shrunk_blend_prediction(row: dict[str, Any]) -> float | None:
    """Baseline C's formula, but on the shrinkage-adjusted trailing fields.

    Defined even at zero trailing games (`_shrink` falls back to the league
    prior there), so this is the one baseline that can score a true
    week-1 cold start rather than needing to skip it.
    """
    if row["team_prior_drives_shrunk"] is None or row["opponent_prior_drives_allowed_shrunk"] is None:
        return None
    return (row["team_prior_drives_shrunk"] + row["opponent_prior_drives_allowed_shrunk"]) / 2


def evaluate_baselines(repository, *, from_season: int | None = None, to_season: int | None = None,
                       dataset_version: str = DATASET_VERSION,
                       min_prior_games: int = 1) -> dict[str, Any]:
    """MAE/RMSE/bias for baselines A-G, overall and by season.

    Rows with fewer than `min_prior_games` of trailing history (true cold
    starts -- typically a team's first stored game) are excluded from every
    baseline so an undefined trailing average does not have to be silently
    imputed; how many were dropped is reported rather than hidden. G (the
    shrinkage-adjusted blend) is scored on those same non-cold-start rows for
    a fair A-G comparison, and *separately* on the cold-start rows it alone
    can produce a prediction for -- see `cold_start_recovery`.
    """
    initialize(repository)
    all_rows = _read_dataset_rows(repository, from_season=from_season, to_season=to_season,
                                  dataset_version=dataset_version)

    cold_start, rows = [], []
    for row in all_rows:
        if row["team_prior_games"] < min_prior_games or row["actual_meaningful_drives"] is None:
            cold_start.append(row)
        else:
            rows.append(row)
    league_mean = _mean(r["actual_meaningful_drives"] for r in rows)

    ols_rows = [r for r in rows if r["opponent_prior_drives_allowed"] is not None]
    coefficients = _fit_ols(ols_rows, ("team_prior_drives", "opponent_prior_drives_allowed"),
                            "actual_meaningful_drives") if ols_rows else None

    def _score(scope_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        accumulators = {label: _accumulator() for label in "ABCDEG"}
        for row in scope_rows:
            actual = float(row["actual_meaningful_drives"])
            if league_mean is not None:
                _add(accumulators["A"], league_mean, actual)
            if row["team_prior_drives"] is not None:
                _add(accumulators["B"], row["team_prior_drives"], actual)
            if row["team_prior_drives"] is not None and row["opponent_prior_drives_allowed"] is not None:
                predicted_c = (row["team_prior_drives"] + row["opponent_prior_drives_allowed"]) / 2
                _add(accumulators["C"], predicted_c, actual)
                if coefficients is not None:
                    predicted_d = (coefficients[0] + coefficients[1] * row["team_prior_drives"]
                                  + coefficients[2] * row["opponent_prior_drives_allowed"])
                    _add(accumulators["D"], predicted_d, actual)
            predicted_e = _env_blend_prediction(row)
            if predicted_e is not None:
                _add(accumulators["E"], predicted_e, actual)
            predicted_g = _shrunk_blend_prediction(row)
            if predicted_g is not None:
                _add(accumulators["G"], predicted_g, actual)
        return {
            "A_league_average": _finish(accumulators["A"]),
            "B_team_average": _finish(accumulators["B"]),
            "C_team_and_opponent_allowed_blend": _finish(accumulators["C"]),
            "D_linear_regression": _finish(accumulators["D"]),
            "E_environment_blend": _finish(accumulators["E"]),
            "G_shrunk_blend": _finish(accumulators["G"]),
        }

    by_season_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_season_matched: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_season_rows[int(row["season"])].append(row)
    for row in ols_rows:
        by_season_matched[int(row["season"])].append(row)

    cold_start_recovery = _accumulator()
    for row in cold_start:
        if row["actual_meaningful_drives"] is None:
            continue
        predicted_g = _shrunk_blend_prediction(row)
        if predicted_g is not None:
            _add(cold_start_recovery, predicted_g, float(row["actual_meaningful_drives"]))

    return {
        "dataset_version": dataset_version,
        "from_season": from_season,
        "to_season": to_season,
        "rows_evaluated": len(rows),
        "rows_matched": len(ols_rows),
        "rows_dropped_cold_start": len(cold_start),
        "league_average_drives": round(league_mean, 4) if league_mean is not None else None,
        "baseline_d_coefficients": (
            {"intercept": round(coefficients[0], 5),
             "team_prior_drives": round(coefficients[1], 5),
             "opponent_prior_drives_allowed": round(coefficients[2], 5)}
            if coefficients is not None else None
        ),
        "overall": _score(rows),
        "overall_matched": _score(ols_rows),
        "by_season": {str(season): _score(season_rows) for season, season_rows in sorted(by_season_rows.items())},
        "by_season_matched": {
            str(season): _score(season_rows) for season, season_rows in sorted(by_season_matched.items())
        },
        "cold_start_recovery": {
            **_finish(cold_start_recovery),
            "note": (
                "G is the only baseline defined at zero trailing games (it falls back to the "
                "league prior), so this scores it on exactly the rows_dropped_cold_start rows "
                "every other baseline above had to skip -- not a fair comparison to their MAE, "
                "just what shrinkage alone can recover."
            ),
        },
        "evaluation_note": (
            "Baseline A's league mean and Baseline D's regression are both fit on these same "
            "rows, so both are in-sample diagnostics of how hard the drive-count problem is, "
            "not out-of-sample model performance. Baselines B, C, E and G use only strictly-prior "
            "trailing data per row (E additionally uses the leaguewide trailing level, G is "
            "shrunk toward it) and are already leak-safe by construction -- no fitting involved. "
            "`overall`/`by_season` score each baseline on its own maximal eligible set -- B and G "
            "need only this team's own history, so they cover more rows than C/D/E, which also "
            "need the opponent's. `overall_matched`/`by_season_matched` restrict every baseline "
            "to exactly the rows C/D/E can score (`rows_matched`), so G's apparent MAE edge isn't "
            "partly just easier rows."
        ),
    }


def _predict(coefficients: list[float], feature_keys: tuple[str, ...], row: dict[str, Any]) -> float | None:
    values = [_feature_value(row, key) for key in feature_keys]
    if any(v is None for v in values):
        return None
    return coefficients[0] + sum(c * v for c, v in zip(coefficients[1:], values))


def fit_advanced_model(repository, *, from_season: int | None = None, to_season: int | None = None,
                       feature_keys: tuple[str, ...] = ADVANCED_FEATURES, l2: float = ADVANCED_MODEL_L2,
                       model_version: str = ADVANCED_MODEL_VERSION, dataset_version: str = DATASET_VERSION,
                       min_prior_games: int = 1) -> dict[str, Any]:
    """Fit and persist the advanced xDrives model (Milestone 4).

    Pace, opponent quality (Elo) and market context (total) join the two
    trailing-drive features Baseline D already used. Persisted the same way
    `win_probability_v2.py` persists its logistic coefficients, so a matchup
    projection can load a fitted model instead of refitting on every request.
    Fitting alone does not tell you whether this generalizes -- use
    `evaluate_advanced_model` for that with a train/test season split.
    """
    initialize(repository)
    rows = _read_dataset_rows(repository, from_season=from_season, to_season=to_season,
                              dataset_version=dataset_version)
    eligible = _eligible_rows(rows, feature_keys, min_prior_games=min_prior_games)
    coefficients = (
        _fit_ols(eligible, feature_keys, "actual_meaningful_drives", l2=l2, value_fn=_feature_value)
        if eligible else None
    )
    now = datetime.now(timezone.utc).isoformat()
    with closing(repository._connect()) as connection:
        connection.execute("""
          INSERT INTO cfb_xdrives_model(model_version,feature_json,coefficients_json,l2,
            from_season,to_season,training_rows,fitted_at)
          VALUES(?,?,?,?,?,?,?,?)
          ON CONFLICT(model_version) DO UPDATE SET
            feature_json=excluded.feature_json, coefficients_json=excluded.coefficients_json,
            l2=excluded.l2, from_season=excluded.from_season, to_season=excluded.to_season,
            training_rows=excluded.training_rows, fitted_at=excluded.fitted_at
        """, (model_version, json.dumps(list(feature_keys)),
              json.dumps(coefficients) if coefficients is not None else None,
              l2, from_season, to_season, len(eligible), now))
        connection.commit()
    return {
        "model_version": model_version,
        "feature_keys": list(feature_keys),
        "coefficients": coefficients,
        "l2": l2,
        "training_rows": len(eligible),
        "from_season": from_season,
        "to_season": to_season,
    }


def evaluate_advanced_model(repository, *, train_from: int, train_to: int, test_from: int, test_to: int,
                            feature_keys: tuple[str, ...] = ADVANCED_FEATURES, l2: float = ADVANCED_MODEL_L2,
                            dataset_version: str = DATASET_VERSION, min_prior_games: int = 1) -> dict[str, Any]:
    """A genuine temporal holdout: fit on [train_from, train_to], score on [test_from, test_to].

    `evaluate_baselines`' Baseline A and D are honestly disclosed as in-sample
    diagnostics. This is the out-of-sample check spec section 30 actually asks
    for -- "the advanced model should demonstrate improvement over the
    baseline" is only a meaningful claim when the model has never seen the
    seasons it is scored on. Baselines B and C need no fitting, so they are
    reported on the same test rows for a like-for-like comparison.
    """
    initialize(repository)
    train_rows = _read_dataset_rows(repository, from_season=train_from, to_season=train_to,
                                    dataset_version=dataset_version)
    train_eligible = _eligible_rows(train_rows, feature_keys, min_prior_games=min_prior_games)
    coefficients = (
        _fit_ols(train_eligible, feature_keys, "actual_meaningful_drives", l2=l2, value_fn=_feature_value)
        if train_eligible else None
    )
    league_mean = _mean(row["actual_meaningful_drives"] for row in train_eligible)

    test_rows = _read_dataset_rows(repository, from_season=test_from, to_season=test_to,
                                   dataset_version=dataset_version)
    test_eligible = [row for row in test_rows if row["team_prior_games"] >= min_prior_games
                     and row["actual_meaningful_drives"] is not None]

    def _score(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        accumulators = {label: _accumulator() for label in ("A", "B", "C", "E", "F", "G")}
        for row in rows:
            actual = float(row["actual_meaningful_drives"])
            if league_mean is not None:
                _add(accumulators["A"], league_mean, actual)
            if row["team_prior_drives"] is not None:
                _add(accumulators["B"], row["team_prior_drives"], actual)
            if row["team_prior_drives"] is not None and row["opponent_prior_drives_allowed"] is not None:
                predicted_c = (row["team_prior_drives"] + row["opponent_prior_drives_allowed"]) / 2
                _add(accumulators["C"], predicted_c, actual)
            predicted_env = _env_blend_prediction(row)
            if predicted_env is not None:
                _add(accumulators["E"], predicted_env, actual)
            if coefficients is not None:
                predicted_f = _predict(coefficients, feature_keys, row)
                if predicted_f is not None:
                    _add(accumulators["F"], predicted_f, actual)
            predicted_g = _shrunk_blend_prediction(row)
            if predicted_g is not None:
                _add(accumulators["G"], predicted_g, actual)
        return {
            "A_league_average": _finish(accumulators["A"]),
            "B_team_average": _finish(accumulators["B"]),
            "C_team_and_opponent_allowed_blend": _finish(accumulators["C"]),
            "E_environment_blend": _finish(accumulators["E"]),
            "F_advanced_regression": _finish(accumulators["F"]),
            "G_shrunk_blend": _finish(accumulators["G"]),
        }

    # F needs every ADVANCED_FEATURES value present (which now includes
    # `league_prior_drives`, plus e.g. a market line), a stricter requirement
    # than A/B/C/E -- scoring each baseline on its own maximal eligible set
    # (`test_results`) silently compares them on different rows.
    # `test_results_matched` restricts all five to exactly the rows F can
    # score, so the comparison that actually answers "does the advanced model
    # beat the baselines" is apples-to-apples.
    matched_rows = [row for row in test_eligible
                    if all(_feature_value(row, key) is not None for key in feature_keys)]

    return {
        "train_seasons": [train_from, train_to],
        "test_seasons": [test_from, test_to],
        "feature_keys": list(feature_keys),
        "l2": l2,
        "coefficients": coefficients,
        "training_rows": len(train_eligible),
        "test_rows_evaluated": len(test_eligible),
        "test_rows_matched": len(matched_rows),
        "test_results": _score(test_eligible),
        "test_results_matched": _score(matched_rows),
        "evaluation_note": (
            "A (league mean) and F (advanced regression) are fit only on train_seasons and "
            "scored on test_seasons -- a genuine out-of-sample comparison, unlike "
            "evaluate_baselines' in-sample A/D. E (environment blend), G (shrunk blend) and B/C "
            "need no fitting and are computed the same way regardless of season. `test_results` "
            "scores each baseline on its own maximal eligible set (their 'games' counts can "
            "differ -- F requires every advanced feature, e.g. a market line, which A/B/C/E/G do "
            "not need). `test_results_matched` scores all six on exactly the same rows -- the "
            "rows F can score -- for the actual apples-to-apples comparison. Both are still "
            "restricted to team_prior_games >= min_prior_games; unlike `evaluate_baselines`, this "
            "does not separately score G on true cold starts."
        ),
    }
