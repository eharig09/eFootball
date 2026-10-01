"""Where does the closing spread beat the QB-aware margin model?

Diagnostic only: the market line is scored as a forecast, never used as a
feature. Walk-forward margin forecasts (qb_player_ablation stack) are compared
with the closing spread by segment, so the next modelling effort goes where the
gap actually is. Reports mean absolute error for both, their per-game paired
difference (positive = market better) with a t-statistic, and the share of games
where the market was closer.
"""
from __future__ import annotations

from contextlib import closing
import math
from typing import Any, Callable

import numpy as np

from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict
from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, build_rows
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-market-gap-v1"


def _meta(repository: NFLRepository) -> dict[str, dict[str, Any]]:
    with closing(repository._connect()) as connection:
        return {str(g["game_id"]): dict(g) for g in connection.execute(
            """SELECT game_id,week,spread_line,home_rest,away_rest,division_game,game_date,weekday
               FROM games""")}


def _segments() -> dict[str, Callable[[dict[str, Any]], str]]:
    return {
        "week": lambda r: ("w01-03" if r["week"] <= 3 else "w04-06" if r["week"] <= 6 else
                           "w07-12" if r["week"] <= 12 else "w13-18"),
        "abs_spread": lambda r: ("pickem<=2.5" if abs(r["market"]) <= 2.5 else
                                 "3-6.5" if abs(r["market"]) <= 6.5 else
                                 "7-9.5" if abs(r["market"]) <= 9.5 else "10+"),
        "qb_change": lambda r: "qb_change" if r.get("any_qb_change") else "same_qbs",
        "rest": lambda r: ("home_rest_edge" if r["rest_diff"] > 0 else
                           "away_rest_edge" if r["rest_diff"] < 0 else "equal_rest"),
        "division": lambda r: "division" if r["division_game"] else "non_division",
        "slot": lambda r: "thu_mon" if r["weekday"] in ("Thursday", "Monday") else "sun_other",
        "model_vs_market_side": lambda r: ("model_more_home" if r["pred"] - r["market"] > 3 else
                                           "model_more_away" if r["pred"] - r["market"] < -3 else "agree_within_3"),
        "elo_gap": lambda r: "big_elo_gap" if abs(r.get("elo_diff") or 0) > 150 else "small_elo_gap",
    }


def _stat(rows: list[dict[str, Any]]) -> dict[str, Any]:
    m = np.asarray([abs(r["pred"] - r["actual"]) for r in rows])
    k = np.asarray([abs(r["market"] - r["actual"]) for r in rows])
    d = m - k
    se = d.std(ddof=1) / math.sqrt(len(d)) if len(d) > 2 else 0.0
    return {"n": len(rows), "model_mae": round(float(m.mean()), 3), "market_mae": round(float(k.mean()), 3),
            "gap": round(float(d.mean()), 3), "t": round(float(d.mean() / se), 2) if se else None,
            "market_closer": round(float((k < m).mean()), 3)}


def report(repository: NFLRepository, *, start_season=2013, end_season=2025):
    rows = build_rows(repository, start_season, end_season)
    meta = _meta(repository)
    sample = [r for r in rows if r.get("actual_margin") is not None
              and all(r.get(k) is not None for k in SHRUNK_CHANGE)]
    scored: list[dict[str, Any]] = []
    for season in sorted({int(r["season"]) for r in sample}):
        train = [r for r in sample if int(r["season"]) < season]
        model = _fit(train, SHRUNK_CHANGE)
        if model is None:
            continue
        for r in sample:
            if int(r["season"]) != season:
                continue
            g = meta.get(str(r["game_id"]))
            if not g or g["spread_line"] is None:
                continue
            scored.append({
                **r, "pred": _predict(model, r), "market": float(g["spread_line"]),
                "actual": float(r["actual_margin"]), "week": int(g["week"]),
                "rest_diff": float(g["home_rest"] or 0) - float(g["away_rest"] or 0),
                "division_game": g["division_game"], "weekday": g["weekday"],
            })
    out = {"version": MODEL_VERSION, "market_used": False, "games": len(scored),
           "overall": _stat(scored), "segments": {}}
    for name, fn in _segments().items():
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in scored:
            groups.setdefault(fn(r), []).append(r)
        out["segments"][name] = {k: _stat(v) for k, v in sorted(groups.items()) if len(v) >= 30}
    by_season = {}
    for season in sorted({int(r["season"]) for r in scored}):
        by_season[season] = _stat([r for r in scored if int(r["season"]) == season])["gap"]
    out["gap_by_season"] = by_season
    e_model = np.asarray([r["pred"] - r["actual"] for r in scored])
    e_market = np.asarray([r["market"] - r["actual"] for r in scored])
    out["error_correlation_model_vs_market"] = round(float(np.corrcoef(e_model, e_market)[0, 1]), 3)
    out["residual_of_model_explained_by_market_gap"] = round(float(np.corrcoef(
        e_model, np.asarray([r["pred"] - r["market"] for r in scored]))[0, 1]), 3)
    return out
