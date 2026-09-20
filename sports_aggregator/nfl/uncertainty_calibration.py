"""Leak-safe NFL Football Lab uncertainty calibration.

Evaluates interval calibration AND sharpness for independently calibrated total
and margin forecasts. Residual models are trained only on prior seasons'
out-of-fold calibrated errors.

Compares:
- global empirical absolute-residual quantile
- conditional scale model + standardized residual quantile

Nominal levels: 50%, 80%, 90%.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import (
    _game_rows,
    TOTAL_FEATURES,
    MARGIN_FEATURES,
    RIDGE_ALPHA,
    _fit_ridge,
    _predict,
)

MODEL_VERSION = "nfl-uncertainty-calibration-v1"
LEVELS = (0.50, 0.80, 0.90)
MIN_ROWS = 200

TOTAL_SCALE_FEATURES = (
    "cal_total",
    "abs_cal_margin",
    "sum_pred_drives",
    "sum_pred_total_plays",
    "abs_sum_pred_combined_epa",
    "week",
)
MARGIN_SCALE_FEATURES = (
    "abs_cal_margin",
    "cal_total",
    "abs_diff_pred_drives",
    "abs_diff_pred_pass_epa",
    "abs_diff_pred_rush_epa",
    "abs_diff_pred_combined_epa",
    "week",
)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    return float(np.quantile(np.asarray(values, dtype=float), q, method="higher"))


def _fit_scale(rows: list[dict[str, Any]], features: tuple[str, ...], residual_key: str):
    eligible = [
        r for r in rows
        if r.get(residual_key) is not None
        and all(r.get(k) is not None for k in features)
    ]
    if len(eligible) < MIN_ROWS:
        return None
    x = np.asarray([[r[k] for k in features] for r in eligible], dtype=float)
    y = np.asarray([math.log(abs(float(r[residual_key])) + 0.5) for r in eligible], dtype=float)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales == 0] = 1.0
    z = (x-means)/scales
    design = np.column_stack([np.ones(len(z)), z])
    penalty = np.eye(design.shape[1]) * RIDGE_ALPHA
    penalty[0,0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    return {"features": features, "means": means, "scales": scales, "beta": beta, "n": len(eligible)}


def _scale_predict(model, row) -> float:
    x = np.asarray([row[k] for k in model["features"]], dtype=float)
    z = (x-model["means"])/model["scales"]
    log_scale = float(model["beta"][0] + z @ model["beta"][1:])
    return max(0.25, math.exp(log_scale) - 0.5)


def _interval_summary(rows: list[dict[str, Any]], pred_key: str, actual_key: str,
                      halfwidth_key: str) -> dict[str, Any]:
    vals = [
        (float(r[pred_key]), float(r[actual_key]), float(r[halfwidth_key]))
        for r in rows
        if r.get(pred_key) is not None and r.get(actual_key) is not None
        and r.get(halfwidth_key) is not None
    ]
    if not vals:
        return {"n": 0}
    covered = [abs(a-p) <= h for p,a,h in vals]
    widths = [2*h for _,_,h in vals]
    return {
        "n": len(vals),
        "coverage": round(sum(covered)/len(covered), 4),
        "mean_width": round(sum(widths)/len(widths), 4),
        "median_width": round(float(np.median(np.asarray(widths))), 4),
        "p90_width": round(float(np.quantile(np.asarray(widths), 0.90)), 4),
    }


def _regime_summary(rows: list[dict[str, Any]], kind: str, method: str, level: float):
    hk = f"{kind}_{method}_hw_{str(level).replace('.', '_')}"
    pred = "cal_total" if kind == "total" else "cal_margin"
    actual = "actual_total" if kind == "total" else "actual_margin"

    buckets = {}
    if kind == "margin":
        defs = (
            ("<3", lambda r: abs(float(r["cal_margin"])) < 3),
            ("3-6.5", lambda r: 3 <= abs(float(r["cal_margin"])) < 7),
            ("7-13.5", lambda r: 7 <= abs(float(r["cal_margin"])) < 14),
            ("14+", lambda r: abs(float(r["cal_margin"])) >= 14),
        )
    else:
        defs = (
            ("<40", lambda r: float(r["cal_total"]) < 40),
            ("40-47.5", lambda r: 40 <= float(r["cal_total"]) < 48),
            ("48-55.5", lambda r: 48 <= float(r["cal_total"]) < 56),
            ("56+", lambda r: float(r["cal_total"]) >= 56),
        )
    for label, fn in defs:
        buckets[label] = _interval_summary([r for r in rows if fn(r)], pred, actual, hk)
    return buckets


def _week_regime_summary(rows: list[dict[str, Any]], kind: str, method: str, level: float):
    hk = f"{kind}_{method}_hw_{str(level).replace('.', '_')}"
    pred = "cal_total" if kind == "total" else "cal_margin"
    actual = "actual_total" if kind == "total" else "actual_margin"
    defs = (
        ("week<=3", lambda r: int(r["week"]) <= 3),
        ("week4-8", lambda r: 4 <= int(r["week"]) <= 8),
        ("week9+", lambda r: int(r["week"]) >= 9),
    )
    return {
        label: _interval_summary([r for r in rows if fn(r)], pred, actual, hk)
        for label, fn in defs
    }


def _calibrated_oof(repository: NFLRepository, start_season: int, end_season: int):
    games = _game_rows(repository, start_season=start_season, end_season=end_season)
    seasons = sorted({int(r["season"]) for r in games})
    out = []
    for season in seasons:
        train = [r for r in games if int(r["season"]) < season]
        test = [dict(r) for r in games if int(r["season"]) == season]
        total_model = _fit_ridge(train, TOTAL_FEATURES, "actual_total")
        margin_model = _fit_ridge(train, MARGIN_FEATURES, "actual_margin")
        if total_model is None or margin_model is None:
            continue
        for r in test:
            r["cal_total"] = _predict(total_model, r)
            r["cal_margin"] = _predict(margin_model, r)
            r["total_residual"] = float(r["actual_total"]) - float(r["cal_total"])
            r["margin_residual"] = float(r["actual_margin"]) - float(r["cal_margin"])
            r["abs_cal_margin"] = abs(float(r["cal_margin"]))
            r["abs_sum_pred_combined_epa"] = abs(float(r["sum_pred_combined_epa"]))
            r["abs_diff_pred_drives"] = abs(float(r["diff_pred_drives"]))
            r["abs_diff_pred_pass_epa"] = abs(float(r["diff_pred_pass_epa"]))
            r["abs_diff_pred_rush_epa"] = abs(float(r["diff_pred_rush_epa"]))
            r["abs_diff_pred_combined_epa"] = abs(float(r["diff_pred_combined_epa"]))
            out.append(r)
    return out


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    rows = _calibrated_oof(repository, start_season, end_season)
    seasons = sorted({int(r["season"]) for r in rows})
    pooled = []
    folds = []

    for season in seasons:
        train = [r for r in rows if int(r["season"]) < season]
        test = [dict(r) for r in rows if int(r["season"]) == season]
        if len(train) < MIN_ROWS or not test:
            continue

        total_scale = _fit_scale(train, TOTAL_SCALE_FEATURES, "total_residual")
        margin_scale = _fit_scale(train, MARGIN_SCALE_FEATURES, "margin_residual")
        if total_scale is None or margin_scale is None:
            continue

        total_global_abs = [abs(float(r["total_residual"])) for r in train]
        margin_global_abs = [abs(float(r["margin_residual"])) for r in train]

        total_std = [
            abs(float(r["total_residual"])) / _scale_predict(total_scale, r)
            for r in train
        ]
        margin_std = [
            abs(float(r["margin_residual"])) / _scale_predict(margin_scale, r)
            for r in train
        ]

        for r in test:
            ts = _scale_predict(total_scale, r)
            ms = _scale_predict(margin_scale, r)
            r["total_scale"] = ts
            r["margin_scale"] = ms
            for level in LEVELS:
                suffix = str(level).replace(".", "_")
                r[f"total_global_hw_{suffix}"] = _quantile(total_global_abs, level)
                r[f"margin_global_hw_{suffix}"] = _quantile(margin_global_abs, level)
                r[f"total_conditional_hw_{suffix}"] = _quantile(total_std, level) * ts
                r[f"margin_conditional_hw_{suffix}"] = _quantile(margin_std, level) * ms

        pooled.extend(test)
        fold = {"season": season, "train_games": len(train), "test_games": len(test), "levels": {}}
        for level in LEVELS:
            suffix = str(level).replace(".", "_")
            fold["levels"][str(level)] = {
                "total": {
                    "global": _interval_summary(test, "cal_total", "actual_total", f"total_global_hw_{suffix}"),
                    "conditional": _interval_summary(test, "cal_total", "actual_total", f"total_conditional_hw_{suffix}"),
                },
                "margin": {
                    "global": _interval_summary(test, "cal_margin", "actual_margin", f"margin_global_hw_{suffix}"),
                    "conditional": _interval_summary(test, "cal_margin", "actual_margin", f"margin_conditional_hw_{suffix}"),
                },
            }
        folds.append(fold)

    pooled_levels = {}
    regimes = {}
    week_regimes = {}
    for level in LEVELS:
        suffix = str(level).replace(".", "_")
        pooled_levels[str(level)] = {
            "total": {
                "global": _interval_summary(pooled, "cal_total", "actual_total", f"total_global_hw_{suffix}"),
                "conditional": _interval_summary(pooled, "cal_total", "actual_total", f"total_conditional_hw_{suffix}"),
            },
            "margin": {
                "global": _interval_summary(pooled, "cal_margin", "actual_margin", f"margin_global_hw_{suffix}"),
                "conditional": _interval_summary(pooled, "cal_margin", "actual_margin", f"margin_conditional_hw_{suffix}"),
            },
        }
        regimes[str(level)] = {
            "total_global": _regime_summary(pooled, "total", "global", level),
            "total_conditional": _regime_summary(pooled, "total", "conditional", level),
            "margin_global": _regime_summary(pooled, "margin", "global", level),
            "margin_conditional": _regime_summary(pooled, "margin", "conditional", level),
        }
        week_regimes[str(level)] = {
            "total_global": _week_regime_summary(pooled, "total", "global", level),
            "total_conditional": _week_regime_summary(pooled, "total", "conditional", level),
            "margin_global": _week_regime_summary(pooled, "margin", "global", level),
            "margin_conditional": _week_regime_summary(pooled, "margin", "conditional", level),
        }

    return {
        "version": MODEL_VERSION,
        "market_used": False,
        "nominal_levels": list(LEVELS),
        "methodology": (
            "Walk-forward calibrated total/margin residuals. Conditional method fits "
            "log absolute residual scale on prior OOF residuals, then applies an empirical "
            "quantile of standardized prior residuals. Report emphasizes coverage and width."
        ),
        "scale_features": {
            "total": list(TOTAL_SCALE_FEATURES),
            "margin": list(MARGIN_SCALE_FEATURES),
        },
        "walk_forward": folds,
        "pooled": pooled_levels,
        "regimes": regimes,
        "week_regimes": week_regimes,
    }
