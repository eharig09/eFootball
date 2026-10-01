"""Does weather improve the calibrated NFL total, and does it survive a forecast?

Fits the total ridge (TOTAL_FEATURES) with and without game-window weather from
nfl_weather_history, walk-forward by season. Two questions, kept apart on purpose:

1. Effect size. Fit and score on reanalysis ("observed") weather for every season
   with coverage. This is an oracle -- no live pick has the realised weather.
2. Realism. For seasons where issued forecasts exist (wind from 2024), fit on
   observed weather and *score using the forecast issued lead_days earlier*. That
   is the number a live forecast could actually achieve.

All variants in a comparison are scored on the identical game set. The closing
total is only a benchmark, never a feature.
"""
from __future__ import annotations

from contextlib import closing
import math
from typing import Any

import numpy as np

from sports_aggregator.nfl.qb_player_ablation import _paired, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import TOTAL_FEATURES, _fit_ridge, _predict

MODEL_VERSION = "nfl-weather-total-ablation-v1"
WEATHER = ("wx_wind", "wx_gust", "wx_cold", "wx_precip", "wx_dome")
LEADS = (1, 2)


def _wx(row: dict[str, Any] | None) -> dict[str, float] | None:
    """Weather feature dict from a nfl_weather_history row; None if any input is missing."""
    if row is None:
        return None
    if row["indoor"]:
        return {"wx_wind": 0.0, "wx_gust": 0.0, "wx_cold": 0.0, "wx_precip": 0.0, "wx_dome": 1.0}
    if any(row[k] is None for k in ("temperature", "wind_speed", "wind_gust", "precipitation")):
        return None
    return {"wx_wind": float(row["wind_speed"]), "wx_gust": float(row["wind_gust"]),
            "wx_cold": max(0.0, 50.0 - float(row["temperature"])),
            "wx_precip": float(row["precipitation"]), "wx_dome": 0.0}


def load_weather(repository: NFLRepository):
    """(observed, {lead: forecast}) maps of game_id -> weather feature dict."""
    observed: dict[str, dict[str, float]] = {}
    forecast: dict[int, dict[str, dict[str, float]]] = {n: {} for n in LEADS}
    with closing(repository._connect()) as connection:
        for r in connection.execute("SELECT * FROM nfl_weather_history"):
            feats = _wx(r)
            if feats is None:
                continue
            if r["kind"] == "observed":
                observed[str(r["game_id"])] = feats
            elif int(r["lead_days"]) in forecast:
                forecast[int(r["lead_days"])][str(r["game_id"])] = feats
    return observed, forecast


def _total_lines(repository: NFLRepository) -> dict[str, float]:
    with closing(repository._connect()) as connection:
        return {str(r["game_id"]): float(r["total_line"]) for r in connection.execute(
            "SELECT game_id,total_line FROM games WHERE total_line IS NOT NULL")}


def _summary(pairs: list[tuple[float, float]]) -> dict[str, Any]:
    e = np.asarray([p - a for p, a in pairs])
    return {"n": len(pairs), "mae": round(float(np.abs(e).mean()), 4),
            "rmse": round(float(math.sqrt((e * e).mean())), 4), "bias": round(float(e.mean()), 4)}


def _paired_totals(rows, a: str, b: str) -> dict[str, Any]:
    shaped = [{"actual_margin": r["actual_total"], a: r[a], b: r[b]} for r in rows]
    return _paired(shaped, a, b)


def _coefficients(model) -> dict[str, float]:
    """Weather effects in points per natural unit (mph, deg below 50F, inch, dome=1)."""
    names = model["features"]
    beta = model["beta"][1:] / model["scales"]
    return {n: round(float(b), 3) for n, b in zip(names, beta) if n in WEATHER}


def report(repository: NFLRepository, *, start_season=2013, end_season=2025):
    rows = build_rows(repository, start_season, end_season)
    observed, forecast = load_weather(repository)
    lines = _total_lines(repository)
    base_ok = [r for r in rows if r.get("actual_total") is not None
               and all(r.get(k) is not None for k in TOTAL_FEATURES)]
    sample = []
    for r in base_ok:
        w = observed.get(str(r["game_id"]))
        if w is not None:
            sample.append({**r, **w})
    with_wx = TOTAL_FEATURES + WEATHER

    pooled, last_model = [], None
    realistic: dict[int, list[dict[str, Any]]] = {n: [] for n in LEADS}
    for season in sorted({int(r["season"]) for r in sample}):
        train = [r for r in sample if int(r["season"]) < season]
        test = [dict(r) for r in sample if int(r["season"]) == season]
        plain = _fit_ridge(train, TOTAL_FEATURES, "actual_total")
        weather = _fit_ridge(train, with_wx, "actual_total")
        if plain is None or weather is None or not test:
            continue
        last_model = weather
        for r in test:
            r["p_plain"] = _predict(plain, r)
            r["p_obs"] = _predict(weather, r)
            if str(r["game_id"]) in lines:
                r["market"] = lines[str(r["game_id"])]
        pooled.extend(test)
        for lead in LEADS:
            for r in test:
                f = forecast[lead].get(str(r["game_id"]))
                if f is not None:
                    realistic[lead].append({**r, "p_fcst": _predict(weather, {**r, **f})})

    def block(rows_, extra=()):
        bench = [r for r in rows_ if r.get("market") is not None]
        out = {"n": len(rows_),
               "pooled": {k: _summary([(r[k], r["actual_total"]) for r in rows_]) for k in ("p_plain", "p_obs", *extra)},
               "paired_logic": "negative mean_abs_err_diff = weather model better than no-weather ridge",
               "market_total": _summary([(r["market"], r["actual_total"]) for r in bench]) if bench else None}
        out["paired_obs_vs_plain"] = _paired_totals(rows_, "p_plain", "p_obs")
        for k in extra:
            out[f"paired_{k}_vs_plain"] = _paired_totals(rows_, "p_plain", k)
        return out

    outdoor = [r for r in pooled if not r["wx_dome"]]
    windy = [r for r in outdoor if r["wx_wind"] >= 15 or r["wx_precip"] >= 0.1 or r["wx_cold"] >= 25]
    by_season = {}
    for season in sorted({int(r["season"]) for r in pooled}):
        sub = [r for r in pooled if int(r["season"]) == season]
        by_season[season] = round(_summary([(r["p_obs"], r["actual_total"]) for r in sub])["mae"]
                                  - _summary([(r["p_plain"], r["actual_total"]) for r in sub])["mae"], 4)
    return {
        "version": MODEL_VERSION,
        "market_used": False,
        "weather_games": len(sample), "total_games": len(base_ok),
        "oracle_all_games": block(pooled),
        "oracle_outdoor_games": block(outdoor),
        "oracle_bad_weather_games": block(windy),
        "seasons_where_weather_beats_plain": f"{sum(v < 0 for v in by_season.values())}/{len(by_season)}",
        "mae_diff_by_season_obs_minus_plain": by_season,
        "realistic_issued_forecast": {
            f"lead_{n}_days": (block(realistic[n], ("p_fcst",)) if realistic[n] else None) for n in LEADS},
        "weather_effect_points_per_unit_last_fit": _coefficients(last_model) if last_model else {},
    }
