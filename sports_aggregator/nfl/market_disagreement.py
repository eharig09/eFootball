"""NFL Football Lab market-disagreement backtest.

Uses the independently calibrated, market-free Football Lab total/margin as the
forecast. Closing spread/total are introduced ONLY after the forecast is fixed.

The conditional residual-scale model converts raw disagreement into a
standardized edge:
    edge_z = abs(model - market) / predicted_residual_scale

Backtests whether larger independent disagreements are more informative ATS and
against the closing total.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.uncertainty_calibration import (
    _calibrated_oof,
    _fit_scale,
    _scale_predict,
    TOTAL_SCALE_FEATURES,
    MARGIN_SCALE_FEATURES,
    MIN_ROWS,
)

MODEL_VERSION = "nfl-market-disagreement-v1"

Z_BUCKETS = (
    ("<0.25", 0.0, 0.25),
    ("0.25-0.49", 0.25, 0.50),
    ("0.50-0.74", 0.50, 0.75),
    ("0.75-0.99", 0.75, 1.00),
    ("1.00+", 1.00, float("inf")),
)
POINT_BUCKETS = (
    ("<2", 0.0, 2.0),
    ("2-3.9", 2.0, 4.0),
    ("4-5.9", 4.0, 6.0),
    ("6+", 6.0, float("inf")),
)


def _market_map(repository: NFLRepository, start_season: int, end_season: int):
    repository.initialize()
    with repository._connect() as connection:
        rows = connection.execute(
            """SELECT game_id,spread_line,total_line
               FROM games
               WHERE season BETWEEN ? AND ? AND completed=1""",
            (int(start_season), int(end_season)),
        )
        return {
            str(r["game_id"]): {
                "market_margin": float(r["spread_line"]) if r["spread_line"] is not None else None,
                "market_total": float(r["total_line"]) if r["total_line"] is not None else None,
            }
            for r in rows
        }


def _decision(row: dict[str, Any], kind: str):
    if kind == "margin":
        model = float(row["cal_margin"])
        market = float(row["market_margin"])
        actual = float(row["actual_margin"])
    else:
        model = float(row["cal_total"])
        market = float(row["market_total"])
        actual = float(row["actual_total"])
    edge = model-market
    realized = actual-market
    if edge == 0:
        result = "no_bet"
    elif realized == 0:
        result = "push"
    elif edge * realized > 0:
        result = "win"
    else:
        result = "loss"
    return edge, realized, result


def _record(rows: list[dict[str, Any]], kind: str):
    result_key = f"{kind}_result"
    wins = sum(r[result_key] == "win" for r in rows)
    losses = sum(r[result_key] == "loss" for r in rows)
    pushes = sum(r[result_key] == "push" for r in rows)
    decided = wins+losses
    if kind == "margin":
        edge_key = "margin_edge"
        realized_key = "margin_realized_edge"
    else:
        edge_key = "total_edge"
        realized_key = "total_realized_edge"
    directional = [
        (1.0 if float(r[edge_key]) > 0 else -1.0) * float(r[realized_key])
        for r in rows if float(r[edge_key]) != 0
    ]
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "decided": decided,
        "win_rate": round(wins/decided, 4) if decided else None,
        "mean_directional_market_result": round(sum(directional)/len(directional), 4)
        if directional else None,
        "mean_abs_model_market_edge": round(
            sum(abs(float(r[edge_key])) for r in rows)/len(rows), 4
        ) if rows else None,
    }


def _bucket(rows, key, definitions, kind):
    out = {}
    for label, lo, hi in definitions:
        group = [r for r in rows if lo <= float(r[key]) < hi]
        out[label] = _record(group, kind)
    return out


def _market_regimes(rows, kind):
    if kind == "margin":
        defs = (
            ("<3", lambda r: abs(float(r["market_margin"])) < 3),
            ("3-6.5", lambda r: 3 <= abs(float(r["market_margin"])) < 7),
            ("7-13.5", lambda r: 7 <= abs(float(r["market_margin"])) < 14),
            ("14+", lambda r: abs(float(r["market_margin"])) >= 14),
        )
    else:
        defs = (
            ("<40", lambda r: float(r["market_total"]) < 40),
            ("40-47.5", lambda r: 40 <= float(r["market_total"]) < 48),
            ("48-55.5", lambda r: 48 <= float(r["market_total"]) < 56),
            ("56+", lambda r: float(r["market_total"]) >= 56),
        )
    return {label: _record([r for r in rows if fn(r)], kind) for label, fn in defs}


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    rows = _calibrated_oof(repository, start_season, end_season)
    markets = _market_map(repository, start_season, end_season)
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

        evaluated = []
        for r in test:
            market = markets.get(str(r["game_id"]))
            if not market:
                continue
            r.update(market)
            if r.get("market_margin") is None or r.get("market_total") is None:
                continue
            r["margin_scale"] = _scale_predict(margin_scale, r)
            r["total_scale"] = _scale_predict(total_scale, r)

            me, mr, mres = _decision(r, "margin")
            te, tr, tres = _decision(r, "total")
            r["margin_edge"] = me
            r["margin_abs_edge"] = abs(me)
            r["margin_realized_edge"] = mr
            r["margin_result"] = mres
            r["margin_edge_z"] = abs(me)/r["margin_scale"]

            r["total_edge"] = te
            r["total_abs_edge"] = abs(te)
            r["total_realized_edge"] = tr
            r["total_result"] = tres
            r["total_edge_z"] = abs(te)/r["total_scale"]
            evaluated.append(r)

        pooled.extend(evaluated)
        folds.append({
            "season": season,
            "games": len(evaluated),
            "margin": {
                "all": _record(evaluated, "margin"),
                "by_z": _bucket(evaluated, "margin_edge_z", Z_BUCKETS, "margin"),
                "by_points": _bucket(evaluated, "margin_abs_edge", POINT_BUCKETS, "margin"),
            },
            "total": {
                "all": _record(evaluated, "total"),
                "by_z": _bucket(evaluated, "total_edge_z", Z_BUCKETS, "total"),
                "by_points": _bucket(evaluated, "total_abs_edge", POINT_BUCKETS, "total"),
            },
        })

    return {
        "version": MODEL_VERSION,
        "forecast_market_independent": True,
        "market_role": "benchmark/disagreement layer only",
        "spread_convention": (
            "nflverse spread_line is expected home margin: positive means home favored; "
            "Football Lab calibrated margin is home points minus away points."
        ),
        "standardization": "abs(model-market) / prior-OOF conditional residual scale",
        "z_buckets": [label for label,_,_ in Z_BUCKETS],
        "point_buckets": [label for label,_,_ in POINT_BUCKETS],
        "walk_forward": folds,
        "pooled": {
            "margin": {
                "all": _record(pooled, "margin"),
                "by_z": _bucket(pooled, "margin_edge_z", Z_BUCKETS, "margin"),
                "by_points": _bucket(pooled, "margin_abs_edge", POINT_BUCKETS, "margin"),
                "by_market_spread": _market_regimes(pooled, "margin"),
            },
            "total": {
                "all": _record(pooled, "total"),
                "by_z": _bucket(pooled, "total_edge_z", Z_BUCKETS, "total"),
                "by_points": _bucket(pooled, "total_abs_edge", POINT_BUCKETS, "total"),
                "by_market_total": _market_regimes(pooled, "total"),
            },
        },
    }
