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
        out.append({
            "game_id": game_id, "season": hc["season"], "week": hc["week"],
            "hc_diff": float(hc["home_pre_elo"]) - float(hc["away_pre_elo"]),
            "qb_diff": qb_diff,
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


def solo_vs_combined_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    both = [r for r in rows if r.get("qb_diff") is not None]
    hc_z = _zscore_diff(both, "hc_diff")
    qb_z = _zscore_diff(both, "qb_diff")
    for r in both:
        if r["game_id"] in hc_z and r["game_id"] in qb_z:
            r["combined_z_avg"] = (hc_z[r["game_id"]] + qb_z[r["game_id"]]) / 2.0

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
    both = [r for r in rows if r.get("qb_diff") is not None]
    hc_z = _zscore_diff(both, "hc_diff")
    qb_z = _zscore_diff(both, "qb_diff")
    scored = []
    for r in both:
        if r["game_id"] in hc_z and r["game_id"] in qb_z:
            combined = (hc_z[r["game_id"]] + qb_z[r["game_id"]]) / 2.0
            scored.append({**r, "combined_z_avg": combined})
    scored.sort(key=lambda r: r["combined_z_avg"])
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
    both = [r for r in rows if r.get("qb_diff") is not None]
    hc_z = _zscore_diff(both, "hc_diff")
    qb_z = _zscore_diff(both, "qb_diff")
    scored = []
    for r in both:
        if r["game_id"] in hc_z and r["game_id"] in qb_z:
            scored.append({**r, "combined_z_avg": (hc_z[r["game_id"]] + qb_z[r["game_id"]]) / 2.0})

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
    }
