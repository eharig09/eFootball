"""Does head-coach Elo or QB Elo actually predict CFB game outcomes -- solo,
together, averaged, or as a simple agreement count -- and does weather bend
that relationship?

Four questions, each answered separately rather than folded into one score:

1. Solo vs. combined: correlation with actual margin and win rate when
   "favored," for HC Elo alone, QB Elo alone, both as a two-feature fit, and
   a Z-score average of the two. All in-sample (not a walk-forward
   backtest): the ratings themselves are already leak-safe pregame
   snapshots (coach_elo.py / qb_elo.py replay chronologically), so this is
   a retrospective "was there a relationship" question, not a forecasting
   model being scored on held-out data.
2. Agreement counting: bucket games by how many of the two signals favor
   the home team (2-0, 1-1, 0-2) and compare win rate / average margin
   across buckets -- does agreement between two independent-ish signals
   carry more information than either alone.
3. Magnitude: bucket the combined signal by size (not just sign) and check
   whether actual margin scales with it -- a bigger Elo gap should predict
   a bigger actual margin if the relationship is a real dose-response, not
   just a coin-flip-beating direction call.
4. Weather interaction: does prediction quality (correlation strength,
   upset rate against a strongly-favored side) change in bad weather --
   testing "weather levels the field" (worse predictions) against "bad
   games are disproportionately bad-weather games" (upsets cluster there)
   as two distinct, separately-checked claims.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.weather_market_impact import PRECIP_BUCKETS, TEMP_BUCKETS, WIND_BUCKETS

#: How close the market itself rates a game -- same boundaries as NFL's
#: uncertainty-calibration regime buckets, so "the 3-6.5 range" means the
#: same thing here as it does there.
SPREAD_CLOSENESS_BUCKETS = (
    ("<3", lambda x: x < 3),
    ("3-6.5", lambda x: 3 <= x < 7),
    ("7-13.5", lambda x: 7 <= x < 14),
    ("14+", lambda x: x >= 14),
)
#: How much the curve disagrees with the market, in points.
DISAGREEMENT_BUCKETS = (
    ("<1", lambda x: x < 1),
    ("1-2.99", lambda x: 1 <= x < 3),
    ("3-5.99", lambda x: 3 <= x < 6),
    ("6+", lambda x: x >= 6),
)


def _load_dataset(repository: CFBRepository, *, start_season: int, end_season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        hc_rows = {row["game_id"]: dict(row) for row in connection.execute(
            """SELECT game_id,season,week,home_points,away_points,
                      home_pre_elo,away_pre_elo
               FROM cfb_coach_elo_games WHERE season BETWEEN ? AND ?""",
            (int(start_season), int(end_season)),
        )}
        qb_rows: dict[int, dict[str, float]] = defaultdict(dict)
        for row in connection.execute(
            """SELECT game_id,side,pre_rating FROM cfb_qb_elo_games
               WHERE season BETWEEN ? AND ?""",
            (int(start_season), int(end_season)),
        ):
            qb_rows[row["game_id"]][row["side"]] = row["pre_rating"]
        weather_rows = {row["game_id"]: dict(row) for row in connection.execute(
            """SELECT w.game_id,w.temperature,w.sustained_wind,w.precipitation_amount
               FROM (
                   SELECT gw.*, ROW_NUMBER() OVER (
                       PARTITION BY gw.game_id
                       ORDER BY ABS(strftime('%s', gw.forecast_generated_at) - strftime('%s', gw.kickoff_time))
                   ) AS rn
                   FROM game_weather gw
               ) w WHERE w.rn = 1"""
        )}
        market_rows = {row["game_id"]: row["spread"] for row in connection.execute(
            """SELECT game_id, AVG(spread) AS spread FROM game_lines
               WHERE spread IS NOT NULL GROUP BY game_id"""
        )}

    out = []
    for game_id, hc in hc_rows.items():
        if hc["home_points"] is None or hc["away_points"] is None:
            continue
        actual_margin = float(hc["home_points"]) - float(hc["away_points"])
        qb = qb_rows.get(game_id, {})
        qb_diff = (
            float(qb["home"]) - float(qb["away"])
            if "home" in qb and "away" in qb else None
        )
        weather = weather_rows.get(game_id, {})
        spread = market_rows.get(game_id)
        out.append({
            "game_id": game_id, "season": hc["season"], "week": hc["week"],
            "hc_diff": float(hc["home_pre_elo"]) - float(hc["away_pre_elo"]),
            "qb_diff": qb_diff,
            # Spread convention (matching margin_feature_ablation.py):
            # negative spread means the home team is favored by that many
            # points, so the market's implied home margin is -spread.
            "market_margin": -float(spread) if spread is not None else None,
            "actual_margin": actual_margin,
            "home_won": actual_margin > 0,
            "sustained_wind": weather.get("sustained_wind"),
            "temperature": weather.get("temperature"),
            "precipitation_amount": weather.get("precipitation_amount"),
        })
    return out


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    if n < 2:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    var_x = sum((x - mx) ** 2 for x in xs)
    var_y = sum((y - my) ** 2 for y in ys)
    denom = math.sqrt(var_x * var_y)
    return cov / denom if denom else None


def _win_rate_when_favored(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    favored = [r for r in rows if r.get(key) is not None and r[key] > 0]
    n = len(favored)
    if not n:
        return {"n": 0}
    wins = sum(1 for r in favored if r["home_won"])
    return {"n": n, "home_favored_win_rate": round(wins / n, 4)}


def _ols(rows: list[dict[str, Any]], features: tuple[str, ...], target: str) -> dict[str, Any] | None:
    """Plain OLS (no regularization -- this is descriptive, not a model
    being deployed), reporting R2 as how much of the margin's variance the
    feature set explains."""
    usable = [r for r in rows if r.get(target) is not None and all(r.get(f) is not None for f in features)]
    n = len(usable)
    if n < len(features) + 2:
        return None
    size = len(features) + 1
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size
    for r in usable:
        x = [1.0] + [float(r[f]) for f in features]
        y = float(r[target])
        for i in range(size):
            xty[i] += x[i] * y
            for j in range(size):
                xtx[i][j] += x[i] * x[j]
    aug = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda rr: abs(aug[rr][col]))
        if abs(aug[pivot][col]) < 1e-9:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for rr in range(col + 1, size):
            factor = aug[rr][col] / aug[col][col]
            for cc in range(col, size + 1):
                aug[rr][cc] -= factor * aug[col][cc]
    beta = [0.0] * size
    for rr in range(size - 1, -1, -1):
        beta[rr] = (aug[rr][size] - sum(aug[rr][cc] * beta[cc] for cc in range(rr + 1, size))) / aug[rr][rr]

    my = sum(float(r[target]) for r in usable) / n
    ss_tot = sum((float(r[target]) - my) ** 2 for r in usable)
    ss_res = 0.0
    for r in usable:
        pred = beta[0] + sum(beta[i + 1] * float(r[f]) for i, f in enumerate(features))
        ss_res += (float(r[target]) - pred) ** 2
    r2 = 1.0 - ss_res / ss_tot if ss_tot else None
    return {"n": n, "features": list(features), "coefficients": beta, "r_squared": round(r2, 4) if r2 is not None else None}


def _zscore_diff(rows: list[dict[str, Any]], key: str) -> dict[int, float]:
    values = [r[key] for r in rows if r.get(key) is not None]
    if not values:
        return {}
    n = len(values)
    mean = sum(values) / n
    stdev = math.sqrt(sum((v - mean) ** 2 for v in values) / n) or 1.0
    return {r["game_id"]: (r[key] - mean) / stdev for r in rows if r.get(key) is not None}


def _scored_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The subset with both ratings, each row carrying combined_z_avg --
    the shared starting point for every report below that needs the
    combined signal rather than either rating alone."""
    both = [r for r in rows if r.get("qb_diff") is not None]
    hc_z = _zscore_diff(both, "hc_diff")
    qb_z = _zscore_diff(both, "qb_diff")
    out = []
    for r in both:
        if r["game_id"] in hc_z and r["game_id"] in qb_z:
            out.append({**r, "combined_z_avg": (hc_z[r["game_id"]] + qb_z[r["game_id"]]) / 2.0})
    return out


def solo_vs_combined_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    both = _scored_rows(rows)

    return {
        "hc_only_full_sample": {
            "n": len(rows),
            "correlation_with_margin": round(_pearson([(r["hc_diff"], r["actual_margin"]) for r in rows]) or 0, 4),
            **_win_rate_when_favored(rows, "hc_diff"),
            "r_squared": (_ols(rows, ("hc_diff",), "actual_margin") or {}).get("r_squared"),
        },
        "on_the_common_sample_where_both_ratings_exist": {
            "n": len(both),
            "hc_only": {
                "correlation_with_margin": round(_pearson([(r["hc_diff"], r["actual_margin"]) for r in both]) or 0, 4),
                **_win_rate_when_favored(both, "hc_diff"),
            },
            "qb_only": {
                "correlation_with_margin": round(_pearson([(r["qb_diff"], r["actual_margin"]) for r in both]) or 0, 4),
                **_win_rate_when_favored(both, "qb_diff"),
            },
            "both_two_feature_ols": _ols(both, ("hc_diff", "qb_diff"), "actual_margin"),
            "zscore_averaged": {
                "correlation_with_margin": round(
                    _pearson([(r["combined_z_avg"], r["actual_margin"]) for r in both if "combined_z_avg" in r]) or 0, 4),
                **_win_rate_when_favored(
                    [{"combined_z_avg": r.get("combined_z_avg"), "home_won": r["home_won"]} for r in both],
                    "combined_z_avg"),
            },
        },
    }


def agreement_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    both = [r for r in rows if r.get("qb_diff") is not None]
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in both:
        hc_favors_home = r["hc_diff"] > 0
        qb_favors_home = r["qb_diff"] > 0
        if hc_favors_home and qb_favors_home:
            label = "2-0 (both favor home)"
        elif not hc_favors_home and not qb_favors_home:
            label = "0-2 (both favor away)"
        else:
            label = "1-1 (split)"
        buckets[label].append(r)

    def summarize(group: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(group)
        if not n:
            return {"n": 0}
        wins = sum(1 for r in group if r["home_won"])
        return {
            "n": n,
            "home_win_rate": round(wins / n, 4),
            "mean_actual_margin_home_minus_away": round(sum(r["actual_margin"] for r in group) / n, 3),
        }

    return {label: summarize(buckets.get(label, [])) for label in
            ("2-0 (both favor home)", "1-1 (split)", "0-2 (both favor away)")}


def magnitude_report(rows: list[dict[str, Any]], *, n_buckets: int = 5) -> dict[str, Any]:
    scored = sorted(_scored_rows(rows), key=lambda r: r["combined_z_avg"])
    n = len(scored)
    size = max(1, n // n_buckets)
    out = []
    for i in range(n_buckets):
        chunk = scored[i * size:(i + 1) * size] if i < n_buckets - 1 else scored[i * size:]
        if not chunk:
            continue
        out.append({
            "bucket": i + 1,
            "n": len(chunk),
            "mean_combined_z_avg": round(sum(r["combined_z_avg"] for r in chunk) / len(chunk), 3),
            "mean_actual_margin": round(sum(r["actual_margin"] for r in chunk) / len(chunk), 3),
            "home_win_rate": round(sum(1 for r in chunk if r["home_won"]) / len(chunk), 4),
        })
    return {"buckets_low_to_high_combined_signal": out}


def _weather_bucket_label(row: dict[str, Any], dimension: str, buckets: tuple) -> str | None:
    value = row.get(dimension)
    if value is None:
        return None
    for label, predicate in buckets:
        if predicate(value):
            return label
    return None


def weather_interaction_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = _scored_rows(rows)

    # An "upset": the combined signal strongly favored one side (top tercile
    # by |combined_z_avg|) but the other side won anyway.
    with_signal = sorted((r for r in scored if r.get("combined_z_avg") is not None),
                         key=lambda r: abs(r["combined_z_avg"]))
    n_strong = max(1, len(with_signal) // 3)
    strong_signal_games = with_signal[-n_strong:]

    def upset_rate(games: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(games)
        if not n:
            return {"n": 0}
        upsets = sum(
            1 for r in games
            if (r["combined_z_avg"] > 0 and not r["home_won"]) or (r["combined_z_avg"] < 0 and r["home_won"])
        )
        return {"n": n, "upset_rate": round(upsets / n, 4)}

    by_dimension = {}
    for name, field, buckets in (
        ("wind", "sustained_wind", WIND_BUCKETS),
        ("precipitation", "precipitation_amount", PRECIP_BUCKETS),
        ("temperature", "temperature", TEMP_BUCKETS),
    ):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        grouped_strong: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in scored:
            label = _weather_bucket_label(r, field, buckets)
            if label:
                grouped[label].append(r)
        for r in strong_signal_games:
            label = _weather_bucket_label(r, field, buckets)
            if label:
                grouped_strong[label].append(r)
        by_dimension[name] = {
            label: {
                "correlation_with_margin": round(
                    _pearson([(r["combined_z_avg"], r["actual_margin"]) for r in grouped.get(label, [])]) or 0, 4)
                if len(grouped.get(label, [])) >= 5 else None,
                "n_all_games": len(grouped.get(label, [])),
                "strong_signal_upset_rate": upset_rate(grouped_strong.get(label, [])),
            }
            for label, _ in buckets
        }

    return {
        "baseline_upset_rate_top_tercile_signal_strength": upset_rate(strong_signal_games),
        "baseline_correlation_all_weather_known_games": round(
            _pearson([(r["combined_z_avg"], r["actual_margin"]) for r in scored if r.get("sustained_wind") is not None]) or 0, 4),
        "by_weather_dimension": by_dimension,
    }


def _partial_correlation(triplets: list[tuple[float, float, float]]) -> dict[str, Any] | None:
    """r_xy.z -- the correlation between x and y with z's linear effect on
    both removed, via the standard closed-form single-control-variable
    formula (equivalent to correlating the residuals of x~z and y~z
    regressions, without needing to actually fit those regressions)."""
    if len(triplets) < 5:
        return None
    r_xy = _pearson([(x, y) for x, y, _ in triplets])
    r_xz = _pearson([(x, z) for x, _, z in triplets])
    r_yz = _pearson([(y, z) for _, y, z in triplets])
    if r_xy is None or r_xz is None or r_yz is None:
        return None
    denominator = math.sqrt((1 - r_xz ** 2) * (1 - r_yz ** 2))
    if denominator == 0:
        return None
    return {
        "n": len(triplets),
        "raw_correlation_x_vs_y": round(r_xy, 4),
        "control_correlation_x_vs_z": round(r_xz, 4),
        "control_correlation_y_vs_z": round(r_yz, 4),
        "partial_correlation_x_vs_y_given_z": round((r_xy - r_xz * r_yz) / denominator, 4),
    }


def weather_week_controlled_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Temperature tracks week closely in this dataset (hotter early,
    colder late -- r=-0.58), and a team's/starter's current-season signal
    is also thinnest early. A raw temperature-vs-predictability
    relationship could just be rediscovering "early season is noisier,"
    not a real weather effect. Removes week's linear effect from both
    temperature and a continuous "how surprising was this result" measure
    (the absolute residual off a simple combined-signal margin fit, which
    uses the whole sample rather than just top-tercile "upsets") before
    crediting temperature with anything, then cross-checks with a
    transparent within-week-bucket view for anyone who'd rather not trust
    the partial-correlation algebra.
    """
    scored = _scored_rows(rows)
    fit = _ols(scored, ("combined_z_avg",), "actual_margin")
    if fit is None:
        return {"error": "insufficient data to fit a baseline margin model"}
    intercept, slope = fit["coefficients"]
    for r in scored:
        predicted = intercept + slope * r["combined_z_avg"]
        r["abs_residual"] = abs(r["actual_margin"] - predicted)

    usable = [r for r in scored if r.get("temperature") is not None and r.get("week") is not None]
    partial = _partial_correlation([(r["temperature"], r["abs_residual"], r["week"]) for r in usable])

    by_week_bucket = {}
    for label, predicate in (
        ("weeks 1-4", lambda w: w <= 4),
        ("weeks 5-9", lambda w: 5 <= w <= 9),
        ("weeks 10+", lambda w: w >= 10),
    ):
        group = [r for r in usable if predicate(r["week"])]
        by_week_bucket[label] = {
            "n": len(group),
            "mean_temperature": round(sum(r["temperature"] for r in group) / len(group), 1) if group else None,
            "correlation_temperature_vs_abs_residual": (
                round(_pearson([(r["temperature"], r["abs_residual"]) for r in group]) or 0, 4)
                if len(group) >= 5 else None
            ),
        }

    return {
        "n": len(usable),
        "baseline_margin_fit": {"intercept": round(intercept, 3), "slope": round(slope, 3), "r_squared": fit["r_squared"]},
        "correlation_week_vs_temperature": round(_pearson([(r["week"], r["temperature"]) for r in usable]) or 0, 4),
        "week_controlled": partial,
        "within_week_bucket_view": by_week_bucket,
    }


def _error_summary(group: list[dict[str, Any]], pred_key: str, actual_key: str = "actual_margin") -> dict[str, Any]:
    n = len(group)
    if not n:
        return {"n": 0}
    errors = [abs(r[pred_key] - r[actual_key]) for r in group]
    return {
        "n": n,
        "mae": round(sum(errors) / n, 3),
        "rmse": round(math.sqrt(sum(e * e for e in errors) / n), 3),
        "correlation_with_actual": round(_pearson([(r[pred_key], r[actual_key]) for r in group]) or 0, 4),
    }


def _walk_forward_curve_predictions(
    rows: list[dict[str, Any]], *, test_from_season: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fits actual_margin ~ combined_z_avg, and separately actual_margin ~
    market_margin + combined_z_avg, walk-forward -- trained only on
    strictly prior seasons, exactly like this codebase's other
    market-facing backtests (margin_feature_ablation.py,
    market_anchor_leverage.py). Comparing an in-sample-fit curve to a
    genuinely out-of-sample market price would be an unfair fight in the
    curve's favor.

    test_from_season defaults to 2020 in every caller, not the 2015 start
    of the coach/QB Elo history: per-game box scores (game_player_box_stats),
    which QB Elo's starter inference depends on, only exist from 2019
    onward in this database -- 2015-2018 games have zero QB Elo coverage,
    confirmed directly rather than assumed. Any season with insufficient
    training history is skipped automatically below, so an earlier
    test_from_season is harmless, just produces the same folds.

    Shared by vegas_comparison_report (does the curve/anchor beat the
    market on raw accuracy) and directional_edge_report (when the curve
    disagrees with the market, is it right about the direction) so the
    walk-forward fitting only happens once.
    """
    usable = [r for r in _scored_rows(rows) if r.get("market_margin") is not None]
    seasons = sorted({r["season"] for r in usable})
    folds = []
    pooled: list[dict[str, Any]] = []

    for season in seasons:
        if season < test_from_season:
            continue
        train = [r for r in usable if r["season"] < season]
        test = [dict(r) for r in usable if r["season"] == season]
        if not test:
            continue
        curve_fit = _ols(train, ("combined_z_avg",), "actual_margin")
        anchored_fit = _ols(train, ("market_margin", "combined_z_avg"), "actual_margin")
        if curve_fit is None or anchored_fit is None:
            continue
        c0, c1 = curve_fit["coefficients"]
        a0, a1, a2 = anchored_fit["coefficients"]
        for r in test:
            r["curve_predicted_margin"] = c0 + c1 * r["combined_z_avg"]
            r["anchored_predicted_margin"] = a0 + a1 * r["market_margin"] + a2 * r["combined_z_avg"]
        pooled.extend(test)
        folds.append({
            "season": season, "train_games": len(train), "test_games": len(test),
            "curve_alone": _error_summary(test, "curve_predicted_margin"),
            "market": _error_summary(test, "market_margin"),
            "anchored_market_plus_signal": _error_summary(test, "anchored_predicted_margin"),
        })
    return folds, pooled


def vegas_comparison_report(rows: list[dict[str, Any]], *, test_from_season: int = 2020) -> dict[str, Any]:
    """How well does the walk-forward curve (and an anchored market+curve
    blend) predict actual margin, compared to just using the closing line."""
    folds, pooled = _walk_forward_curve_predictions(rows, test_from_season=test_from_season)

    def closer_rate(group: list[dict[str, Any]], challenger_key: str) -> float | None:
        n = len(group)
        if not n:
            return None
        closer = sum(
            1 for r in group
            if abs(r[challenger_key] - r["actual_margin"]) < abs(r["market_margin"] - r["actual_margin"])
        )
        return round(closer / n, 4)

    return {
        "test_from_season": test_from_season,
        "walk_forward": folds,
        "pooled": {
            "n": len(pooled),
            "curve_alone": _error_summary(pooled, "curve_predicted_margin"),
            "market": _error_summary(pooled, "market_margin"),
            "anchored_market_plus_signal": _error_summary(pooled, "anchored_predicted_margin"),
            "curve_closer_than_market_rate": closer_rate(pooled, "curve_predicted_margin"),
            "anchored_closer_than_market_rate": closer_rate(pooled, "anchored_predicted_margin"),
        },
    }


def directional_edge_report(rows: list[dict[str, Any]], *, test_from_season: int = 2020) -> dict[str, Any]:
    """The curve loses on raw accuracy (vegas_comparison_report), but raw
    MAE and directional edge are different questions -- a signal can be a
    worse point estimate than the market while still being right more
    often than chance about *which side* of the market's own number the
    result lands on. Three separate cuts, matching the NFL market-leverage
    backtest's _directional() shape (edge = model - market, actual_edge =
    actual - market, a "win" is edge and actual_edge pointing the same way):

    1. Overall directional win rate against the market.
    2. Bucketed by how close the market itself rates the game
       (SPREAD_CLOSENESS_BUCKETS, "3-6.5" etc.) -- is the edge concentrated
       in games the market already considers close, not blowouts.
    3. Bucketed by how much the curve disagrees with the market
       (DISAGREEMENT_BUCKETS) -- does a bigger gap mean a more trustworthy
       pick, or just a louder wrong one.

    Also separately checks "upset spots": among games where the curve
    flips the favorite outright (it likes the market's underdog to win
    straight up, not just to cover), what fraction of those actually saw
    the underdog win -- compared to the baseline underdog win rate, so
    "the model calls upsets" is checked against "underdogs win X% of the
    time anyway," not against 50%. That comparison is bucket-matched by
    market closeness, not just pooled: flip calls concentrate in games the
    market already rates close (a bigger disagreement is needed to flip a
    big favorite), and close games have a higher baseline upset rate for
    reasons that have nothing to do with the model -- a pooled comparison
    alone made the effect look roughly 1.5x bigger than it is within any
    single closeness bucket, confirmed by checking both ways before
    reporting either.
    """
    _, pooled = _walk_forward_curve_predictions(rows, test_from_season=test_from_season)
    for r in pooled:
        r["model_edge"] = r["curve_predicted_margin"] - r["market_margin"]
        r["actual_edge"] = r["actual_margin"] - r["market_margin"]

    decided = [r for r in pooled if abs(r["model_edge"]) > 1e-9]

    def directional_summary(group: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(group)
        if not n:
            return {"n": 0}
        wins = sum(1 for r in group if r["model_edge"] * r["actual_edge"] > 0)
        losses = sum(1 for r in group if r["model_edge"] * r["actual_edge"] < 0)
        pushes = n - wins - losses
        decisions = wins + losses
        return {
            "n": n, "wins": wins, "losses": losses, "pushes": pushes,
            "win_rate_ex_pushes": round(wins / decisions, 4) if decisions else None,
        }

    by_market_closeness = {}
    by_disagreement_size = {}
    for label, predicate in SPREAD_CLOSENESS_BUCKETS:
        by_market_closeness[label] = directional_summary(
            [r for r in decided if predicate(abs(r["market_margin"]))])
    for label, predicate in DISAGREEMENT_BUCKETS:
        by_disagreement_size[label] = directional_summary(
            [r for r in decided if predicate(abs(r["model_edge"]))])

    def is_upset(r: dict[str, Any]) -> bool:
        return (r["market_margin"] > 0 and r["actual_margin"] < 0) or (
            r["market_margin"] < 0 and r["actual_margin"] > 0)

    flips_favorite = [
        r for r in pooled
        if r["market_margin"] != 0 and r["curve_predicted_margin"] != 0
        and (r["curve_predicted_margin"] > 0) != (r["market_margin"] > 0)
    ]

    def upset_rate(group: list[dict[str, Any]]) -> float | None:
        return round(sum(1 for r in group if is_upset(r)) / len(group), 4) if group else None

    upset_spots_by_bucket = {}
    for label, predicate in SPREAD_CLOSENESS_BUCKETS:
        in_bucket = [r for r in pooled if predicate(abs(r["market_margin"]))]
        flips_in_bucket = [r for r in flips_favorite if predicate(abs(r["market_margin"]))]
        upset_spots_by_bucket[label] = {
            "n_games_in_bucket": len(in_bucket),
            "baseline_upset_rate": upset_rate(in_bucket),
            "n_flip_calls": len(flips_in_bucket),
            "flip_call_upset_rate": upset_rate(flips_in_bucket),
        }

    return {
        "n": len(pooled), "n_with_a_real_disagreement": len(decided),
        "overall": directional_summary(decided),
        "by_market_closeness": by_market_closeness,
        "by_disagreement_size": by_disagreement_size,
        "upset_spots": {
            "games_where_curve_flips_the_market_favorite": len(flips_favorite),
            "naive_pooled_flip_call_upset_rate": upset_rate(flips_favorite),
            "naive_pooled_baseline_upset_rate": upset_rate(pooled),
            "bucket_matched_by_market_closeness": upset_spots_by_bucket,
        },
    }


def report(repository: CFBRepository, *, start_season: int = 2015, end_season: int = 2025) -> dict[str, Any]:
    rows = _load_dataset(repository, start_season=start_season, end_season=end_season)
    return {
        "version": "cfb-rating-predictive-power-v1",
        "start_season": start_season, "end_season": end_season,
        "games_with_hc_elo": len(rows),
        "games_with_both_hc_and_qb_elo": sum(1 for r in rows if r.get("qb_diff") is not None),
        "solo_vs_combined": solo_vs_combined_report(rows),
        "agreement_2_0_1_1_0_2": agreement_report(rows),
        "magnitude": magnitude_report(rows),
        "weather_interaction": weather_interaction_report(rows),
        "weather_week_controlled": weather_week_controlled_report(rows),
        "vegas_comparison": vegas_comparison_report(rows),
        "directional_edge": directional_edge_report(rows),
    }
