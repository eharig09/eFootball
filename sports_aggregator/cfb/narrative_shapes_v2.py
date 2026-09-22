"""Narrative Shape v2: calibrated Line Elo and continuous narrative asymmetry.

V2 keeps the production projection untouched.  It uses the already-built v1
narrative table for leak-safe previous-game context, but recomputes Line Elo
in-memory so current-line assimilation and next-line prediction are distinct.

Validation protocol for a 2025 report:
- 2022-23: fit/history period
- 2024: choose Line-Elo learning rate and ridge regularization
- 2025: untouched held-out evaluation
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.cfb import narrative_shapes as v1
from sports_aggregator.cfb.repository import CFBRepository

LINE_ELO_RATES = (0.10, 0.20, 0.35, 0.50, 0.75, 1.00)
RIDGE_L2_GRID = (0.0, 0.5, 2.0, 5.0, 10.0, 25.0)

CONTINUOUS_FEATURES = (
    "centered_line_gap_diff",
    "perception_change_diff",
    "previous_market_surprise_diff",
    "previous_elo_surprise_diff",
    "rolling_3g_market_surprise_diff",
    "rolling_3g_elo_surprise_diff",
    "lookahead_score_diff",
    "sandwich_score_diff",
    "previous_opponent_elo_diff",
    "market_vs_elo_margin",
)


def _rmse(values: list[float]) -> float | None:
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else None


def _mae(values: list[float]) -> float | None:
    return sum(abs(v) for v in values) / len(values) if values else None


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x, _ in pairs))
    dy = math.sqrt(sum((y - my) ** 2 for _, y in pairs))
    return num / (dx * dy) if dx and dy else None


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for row in range(col + 1, n):
            factor = a[row][col] / a[col][col]
            for j in range(col, n + 1):
                a[row][j] -= factor * a[col][j]
    out = [0.0] * n
    for row in range(n - 1, -1, -1):
        out[row] = (
            a[row][n] - sum(a[row][j] * out[j] for j in range(row + 1, n))
        ) / a[row][row]
    return out


def _fit_ridge(rows: list[dict[str, Any]], *, l2: float) -> dict[str, Any] | None:
    if not rows:
        return None
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for key in CONTINUOUS_FEATURES:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        means[key] = sum(vals) / len(vals) if vals else 0.0
        variance = (
            sum((v - means[key]) ** 2 for v in vals) / len(vals) if vals else 0.0
        )
        scales[key] = math.sqrt(variance) or 1.0

    size = len(CONTINUOUS_FEATURES) + 1
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size
    for row in rows:
        x = [1.0]
        for key in CONTINUOUS_FEATURES:
            raw = row.get(key)
            value = float(raw) if raw is not None else means[key]
            x.append((value - means[key]) / scales[key])
        y = float(row["market_margin_residual"])
        for i in range(size):
            xty[i] += x[i] * y
            for j in range(size):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, size):
        xtx[i][i] += float(l2)
    coefficients = _solve(xtx, xty)
    if coefficients is None:
        return None
    return {
        "coefficients": coefficients,
        "means": means,
        "scales": scales,
        "l2": float(l2),
    }


def _predict(model: dict[str, Any], row: dict[str, Any]) -> float:
    value = float(model["coefficients"][0])
    for i, key in enumerate(CONTINUOUS_FEATURES):
        raw = row.get(key)
        feature = float(raw) if raw is not None else float(model["means"][key])
        value += float(model["coefficients"][i + 1]) * (
            feature - float(model["means"][key])
        ) / float(model["scales"][key])
    return value


def _model_metrics(model: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors, pairs = [], []
    for row in rows:
        actual = float(row["market_margin_residual"])
        predicted = _predict(model, row)
        errors.append(predicted - actual)
        pairs.append((predicted, actual))
    return {
        "n": len(errors),
        "mae": round(_mae(errors), 4) if errors else None,
        "rmse": round(_rmse(errors), 4) if errors else None,
        "bias": round(sum(errors) / len(errors), 4) if errors else None,
        "prediction_correlation": (
            round(_pearson(pairs), 4) if _pearson(pairs) is not None else None
        ),
    }


def _load_rows(repository: CFBRepository) -> list[dict[str, Any]]:
    v1.initialize(repository)
    with repository._reader() as connection:
        return [dict(row) for row in connection.execute(
            """SELECT * FROM cfb_narrative_state
               WHERE narrative_version=?
               ORDER BY kickoff,game_id,side""",
            (v1.NARRATIVE_VERSION,),
        )]


def _line_elo_pass(rows: list[dict[str, Any]], *, learning_rate: float,
                   home_field_points: float = v1.DEFAULT_HOME_FIELD_POINTS
                   ) -> tuple[dict[tuple[int, str], dict[str, float | None]], dict[int, float]]:
    """Recompute pre-line and post-line market ratings chronologically."""
    by_game: dict[int, list[dict[str, Any]]] = defaultdict(list)
    order: list[int] = []
    for row in rows:
        gid = int(row["game_id"])
        if gid not in by_game:
            order.append(gid)
        by_game[gid].append(row)

    line_elo: dict[str, float] = defaultdict(lambda: 1500.0)
    last_true: dict[str, float] = {}
    gap_history: dict[str, list[float]] = defaultdict(list)
    state: dict[tuple[int, str], dict[str, float | None]] = {}
    line_prediction_error: dict[int, float] = {}
    # centered_gap's own population-mean-vs-population-mean centering does
    # NOT cancel line_elo's drift in practice: teams drop in and out of
    # line_elo/last_true's cumulative (never-reset, multi-season) pools at
    # different rates, so the "centered" gap still drifts hard -- measured
    # at +155 mean in 2019 growing to +304 by 2026, market_darling firing on
    # 79-95% of team-games every season (worse than v1's uncorrected bug).
    # Apply the same leak-safe, chronologically-expanding per-season
    # baseline correction used in narrative_shapes.py/matchup_research.py:
    # subtract that season's running mean of the raw centered gap so far.
    season_gap_sum: dict[int, float] = defaultdict(float)
    season_gap_n: dict[int, int] = defaultdict(int)

    for gid in order:
        game_rows = by_game[gid]
        home_row = next((r for r in game_rows if r["side"] == "home"), None)
        away_row = next((r for r in game_rows if r["side"] == "away"), None)
        if not home_row or not away_row:
            continue
        home, away = str(home_row["team"]), str(away_row["team"])
        market_home_margin = home_row.get("market_expected_margin")

        pre_home, pre_away = float(line_elo[home]), float(line_elo[away])
        if market_home_margin is not None:
            predicted_home_margin = (
                (pre_home - pre_away) / v1.ELO_POINTS_PER_SCORE_POINT
                + float(home_field_points)
            )
            line_prediction_error[gid] = (
                predicted_home_margin - float(market_home_margin)
            )
            post_home, post_away = v1._market_line_elo_observation(
                pre_home, pre_away, float(market_home_margin),
                home_field_points=home_field_points,
                learning_rate=learning_rate,
            )
        else:
            post_home, post_away = pre_home, pre_away

        line_elo[home], line_elo[away] = post_home, post_away

        # Update both teams' current pregame True Elo before calculating a
        # population-centered gap so processing order cannot move the center.
        if home_row.get("true_elo") is not None:
            last_true[home] = float(home_row["true_elo"])
        if away_row.get("true_elo") is not None:
            last_true[away] = float(away_row["true_elo"])

        season = int(home_row["season"])
        season_baseline = (
            season_gap_sum[season] / season_gap_n[season] if season_gap_n[season] else 0.0
        )
        raw_gaps_this_round: list[float] = []
        for row, team, opponent, pre_value, post_value in (
            (home_row, home, away, pre_home, post_home),
            (away_row, away, home, pre_away, post_away),
        ):
            true_elo = row.get("true_elo")
            active_line = [float(v) for v in line_elo.values()]
            active_true = [float(v) for v in last_true.values()]
            line_mean = sum(active_line) / len(active_line) if active_line else 1500.0
            true_mean = sum(active_true) / len(active_true) if active_true else 1500.0
            raw_centered_gap = (
                (post_value - line_mean) - (float(true_elo) - true_mean)
                if true_elo is not None else None
            )
            if raw_centered_gap is not None:
                raw_gaps_this_round.append(raw_centered_gap)
            centered_gap = (
                raw_centered_gap - season_baseline if raw_centered_gap is not None else None
            )
            prior_gap = gap_history[team][-1] if gap_history[team] else None
            perception_change = (
                centered_gap - prior_gap
                if centered_gap is not None and prior_gap is not None else None
            )
            state[(gid, team)] = {
                "pre_line_elo": pre_value,
                "post_line_elo": post_value,
                "centered_line_gap": centered_gap,
                "perception_change_v2": perception_change,
            }
            if centered_gap is not None:
                gap_history[team].append(float(centered_gap))
                if len(gap_history[team]) > 3:
                    gap_history[team].pop(0)

        for raw_gap in raw_gaps_this_round:
            season_gap_sum[season] += raw_gap
            season_gap_n[season] += 1

    return state, line_prediction_error


def _choose_line_rate(rows: list[dict[str, Any]], *, validation_season: int
                      ) -> tuple[float, list[dict[str, Any]]]:
    grid = []
    for rate in LINE_ELO_RATES:
        _, errors = _line_elo_pass(rows, learning_rate=rate)
        vals = [
            float(errors[int(r["game_id"])])
            for r in rows
            if r["side"] == "home"
            and int(r["season"]) == int(validation_season)
            and int(r["game_id"]) in errors
        ]
        grid.append({
            "learning_rate": rate,
            "n": len(vals),
            "line_mae": round(_mae(vals), 4) if vals else None,
            "line_rmse": round(_rmse(vals), 4) if vals else None,
        })
    eligible = [g for g in grid if g["line_rmse"] is not None]
    if not eligible:
        # Historical backfills can legitimately leave the requested validation
        # season without Line-Elo observations.  Falling back to the existing
        # production default keeps downstream lens research deterministic and
        # leak-safe instead of selecting from a future season or crashing.
        return float(v1.DEFAULT_LINE_LEARNING_RATE), grid
    selected = min(eligible, key=lambda g: (g["line_rmse"], g["line_mae"]))
    return float(selected["learning_rate"]), grid


def _difference(a: Any, b: Any) -> float | None:
    return float(a) - float(b) if a is not None and b is not None else None


def _feature_rows(rows: list[dict[str, Any]],
                  line_state: dict[tuple[int, str], dict[str, float | None]]
                  ) -> list[dict[str, Any]]:
    lookup = {(int(r["game_id"]), str(r["team"])): r for r in rows}
    output = []
    for row in rows:
        # One independent observation per game.  Every feature is oriented from
        # home-team perspective and the target is home market-margin residual.
        if row["side"] != "home" or row.get("market_margin_residual") is None:
            continue
        gid, team = int(row["game_id"]), str(row["team"])
        opponent = lookup.get((gid, str(row["opponent"])))
        if not opponent:
            continue
        own_line = line_state.get((gid, team), {})
        opp_line = line_state.get((gid, str(row["opponent"])), {})
        output.append({
            "game_id": gid,
            "season": int(row["season"]),
            "team": team,
            "opponent": str(row["opponent"]),
            "market_margin_residual": float(row["market_margin_residual"]),
            "centered_line_gap_diff": _difference(
                own_line.get("centered_line_gap"), opp_line.get("centered_line_gap")),
            "perception_change_diff": _difference(
                own_line.get("perception_change_v2"), opp_line.get("perception_change_v2")),
            "previous_market_surprise_diff": _difference(
                row.get("previous_market_surprise"),
                opponent.get("previous_market_surprise")),
            "previous_elo_surprise_diff": _difference(
                row.get("previous_elo_surprise"),
                opponent.get("previous_elo_surprise")),
            "rolling_3g_market_surprise_diff": _difference(
                row.get("rolling_3g_market_surprise"),
                opponent.get("rolling_3g_market_surprise")),
            "rolling_3g_elo_surprise_diff": _difference(
                row.get("rolling_3g_elo_surprise"),
                opponent.get("rolling_3g_elo_surprise")),
            "lookahead_score_diff": _difference(
                row.get("lookahead_score"), opponent.get("lookahead_score")),
            "sandwich_score_diff": _difference(
                row.get("sandwich_score"), opponent.get("sandwich_score")),
            "previous_opponent_elo_diff": _difference(
                row.get("previous_opponent_elo"), opponent.get("previous_opponent_elo")),
            "market_vs_elo_margin": row.get("market_vs_elo_margin"),
        })
    return output


def _feature_diagnostics(train: list[dict[str, Any]],
                         test: list[dict[str, Any]]) -> dict[str, Any]:
    output = {}
    for key in CONTINUOUS_FEATURES:
        train_pairs = [
            (float(r[key]), float(r["market_margin_residual"]))
            for r in train if r.get(key) is not None
        ]
        test_pairs = [
            (float(r[key]), float(r["market_margin_residual"]))
            for r in test if r.get(key) is not None
        ]
        train_corr = _pearson(train_pairs)
        test_corr = _pearson(test_pairs)
        output[key] = {
            "train_n": len(train_pairs),
            "test_n": len(test_pairs),
            "train_correlation": round(train_corr, 4) if train_corr is not None else None,
            "test_correlation": round(test_corr, 4) if test_corr is not None else None,
            "direction_persisted": (
                (train_corr > 0) == (test_corr > 0)
                if train_corr is not None and test_corr is not None else None
            ),
        }
    return output


def _v2_tags(row: dict[str, Any],
             line_state: dict[tuple[int, str], dict[str, float | None]]) -> list[str]:
    # Preserve result/schedule narratives from v1, but regenerate all Line-Elo
    # narratives from the calibrated, centered v2 state.
    market_tags = {"market_darling", "market_skepticism", "market_chase", "market_lag"}
    tags = [
        c for c in v1.CATEGORY_COLUMNS
        if c not in market_tags and int(row.get(c) or 0)
    ]
    state = line_state.get((int(row["game_id"]), str(row["team"])), {})
    gap = state.get("centered_line_gap")
    change = state.get("perception_change_v2")
    if gap is not None and float(gap) >= v1.MARKET_PERCEPTION_TAG_THRESHOLD:
        tags.append("market_darling")
    if gap is not None and float(gap) <= -v1.MARKET_PERCEPTION_TAG_THRESHOLD:
        tags.append("market_skepticism")
    if change is not None and float(change) >= 35.0:
        tags.append("market_chase")
    if change is not None and float(change) <= -35.0:
        tags.append("market_lag")
    return tags


def _canonical_interactions(rows: list[dict[str, Any]],
                            line_state: dict[tuple[int, str], dict[str, float | None]],
                            *, test_season: int,
                            min_train_rows: int, min_test_rows: int) -> list[dict[str, Any]]:
    lookup = {(int(r["game_id"]), str(r["team"])): r for r in rows}
    buckets: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: {"train": [], "test": []})
    for row in rows:
        if row["side"] != "home" or row.get("market_margin_residual") is None:
            continue
        opponent = lookup.get((int(row["game_id"]), str(row["opponent"])))
        if not opponent:
            continue
        own_tags = _v2_tags(row, line_state)
        opp_tags = _v2_tags(opponent, line_state)
        period = "test" if int(row["season"]) == int(test_season) else (
            "train" if int(row["season"]) < int(test_season) else None)
        if not period:
            continue
        home_resid = float(row["market_margin_residual"])
        for own in own_tags:
            for opp in opp_tags:
                first, second = sorted((own, opp))
                # Orient residual to the first canonical narrative so A-vs-B and
                # B-vs-A are one independent matchup finding, not two mirrors.
                value = home_resid if own == first else -home_resid
                buckets[(first, second)][period].append(value)

    output = []
    for (first, second), periods in buckets.items():
        tr, te = periods["train"], periods["test"]
        if len(tr) < int(min_train_rows) or len(te) < int(min_test_rows):
            continue
        train_mean = sum(tr) / len(tr)
        test_mean = sum(te) / len(te)
        output.append({
            "narrative_a": first,
            "narrative_b": second,
            "orientation": "market residual from narrative_a team's perspective",
            "train_n": len(tr),
            "test_n": len(te),
            "train_mean_market_residual": round(train_mean, 3),
            "test_mean_market_residual": round(test_mean, 3),
            "train_cover_like_rate": round(sum(v > 0 for v in tr) / len(tr), 4),
            "test_cover_like_rate": round(sum(v > 0 for v in te) / len(te), 4),
            "direction_persisted": (train_mean > 0) == (test_mean > 0),
        })
    output.sort(
        key=lambda r: (
            not r["direction_persisted"],
            -min(r["train_n"], r["test_n"]),
            -abs(r["test_mean_market_residual"]),
        )
    )
    return output


def report(repository: CFBRepository, *, test_season: int = 2025,
           min_train_rows: int = 30, min_test_rows: int = 12) -> dict[str, Any]:
    rows = _load_rows(repository)
    validation_season = int(test_season) - 1

    selected_rate, line_rate_grid = _choose_line_rate(
        rows, validation_season=validation_season)
    line_state, line_errors = _line_elo_pass(rows, learning_rate=selected_rate)
    features = _feature_rows(rows, line_state)

    ridge_train = [r for r in features if int(r["season"]) < validation_season]
    ridge_validation = [r for r in features if int(r["season"]) == validation_season]
    ridge_test = [r for r in features if int(r["season"]) == int(test_season)]

    ridge_grid = []
    for l2 in RIDGE_L2_GRID:
        model = _fit_ridge(ridge_train, l2=l2)
        if not model:
            continue
        metrics = _model_metrics(model, ridge_validation)
        ridge_grid.append({"l2": l2, "validation": metrics})
    selected_ridge = min(
        ridge_grid,
        key=lambda item: (
            item["validation"]["mae"],
            item["validation"]["rmse"],
        ),
    )
    final_train = [r for r in features if int(r["season"]) < int(test_season)]
    final_model = _fit_ridge(final_train, l2=float(selected_ridge["l2"]))
    test_metrics = _model_metrics(final_model, ridge_test) if final_model else {}

    baseline_errors = [float(r["market_margin_residual"]) for r in ridge_test]
    zero_baseline = {
        "n": len(baseline_errors),
        "mae": round(_mae(baseline_errors), 4) if baseline_errors else None,
        "rmse": round(_rmse(baseline_errors), 4) if baseline_errors else None,
        "bias": round(-sum(baseline_errors) / len(baseline_errors), 4)
        if baseline_errors else None,
    }

    validation_line_errors = [
        err for gid, err in line_errors.items()
        if any(
            int(r["game_id"]) == gid and r["side"] == "home"
            and int(r["season"]) == validation_season
            for r in rows
        )
    ]
    test_line_errors = [
        err for gid, err in line_errors.items()
        if any(
            int(r["game_id"]) == gid and r["side"] == "home"
            and int(r["season"]) == int(test_season)
            for r in rows
        )
    ]

    return {
        "version": "narrative-shape-v2",
        "test_season": int(test_season),
        "validation_season": validation_season,
        "line_elo": {
            "selected_learning_rate": selected_rate,
            "selection_grid": line_rate_grid,
            "validation_next_line_mae": round(_mae(validation_line_errors), 4)
            if validation_line_errors else None,
            "validation_next_line_rmse": round(_rmse(validation_line_errors), 4)
            if validation_line_errors else None,
            "test_next_line_mae": round(_mae(test_line_errors), 4)
            if test_line_errors else None,
            "test_next_line_rmse": round(_rmse(test_line_errors), 4)
            if test_line_errors else None,
            "state_definition": (
                "pre-line Line Elo predicts the current closing line; current line is "
                "then assimilated to form centered post-line market perception."
            ),
        },
        "continuous_asymmetry": {
            "features": list(CONTINUOUS_FEATURES),
            "diagnostics": _feature_diagnostics(final_train, ridge_test),
            "ridge": {
                "l2_grid": ridge_grid,
                "selected_l2": selected_ridge["l2"],
                "zero_adjustment_baseline": zero_baseline,
                "heldout_test": test_metrics,
                "mae_delta_vs_market_zero_residual": (
                    round(test_metrics["mae"] - zero_baseline["mae"], 4)
                    if test_metrics and zero_baseline["mae"] is not None else None
                ),
                "rmse_delta_vs_market_zero_residual": (
                    round(test_metrics["rmse"] - zero_baseline["rmse"], 4)
                    if test_metrics and zero_baseline["rmse"] is not None else None
                ),
            },
        },
        "canonical_narrative_interactions": _canonical_interactions(
            rows,
            line_state,
            test_season=int(test_season),
            min_train_rows=int(min_train_rows),
            min_test_rows=int(min_test_rows),
        )[:50],
        "notes": [
            "Only one oriented observation per game is used for continuous modeling.",
            "Mirrored A-vs-B / B-vs-A categorical interactions are canonicalized.",
            "Line-Elo learning rate is selected on the season before the held-out test.",
            "Ridge regularization is selected on the season before the held-out test.",
            "No Narrative v2 result is fed into production projections.",
        ],
    }
