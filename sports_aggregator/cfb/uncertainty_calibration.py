"""Calibrated uncertainty for the CFB margin-v2 projection.

The engine publishes a point margin. This module says how far off that margin typically is and what
it implies for the home side's chance of winning, using only out-of-fold errors: the residual scale
used for a season comes from margin-v2 models fitted on strictly earlier seasons and scored on the
season after them, so no game's error ever informs its own interval.

The NFL work (nfl/uncertainty_calibration.py, `projection_cli distribution`) found a plain Gaussian
with one global out-of-fold sigma beat key-number, empirical and conditional-sigma variants on
home-win log loss, and that model-derived cover probabilities were worse than a constant 50%.
`evaluate` re-runs the comparison for CFB so the live packet only carries what held up; cover
probabilities are deliberately never produced.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from typing import Any

from sports_aggregator.cfb import derived_cache
from sports_aggregator.cfb.live_margin_calibration import (
    _historical_rows, fit_models, predict_with_models)

MODEL_VERSION = "cfb-uncertainty-v1"
LEVELS = (0.50, 0.80, 0.90)
MIN_RESIDUALS = 300          # below this a residual scale is not trusted and nothing is served
_Z = {0.50: 0.674490, 0.80: 1.281552, 0.90: 1.644854}


def _phi(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def out_of_fold(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each season's games scored by models fitted on the seasons before it.

    Games margin-v2 does not assess (no Elo) are skipped, exactly as live."""
    seasons = sorted({int(row["season"]) for row in history})
    out: list[dict[str, Any]] = []
    for season in seasons[1:]:
        models = fit_models([row for row in history if int(row["season"]) < season])
        for row in history:
            if int(row["season"]) != season:
                continue
            label, value, _ = predict_with_models(models, row)
            if label is None:
                continue
            out.append({"season": season, "game_id": row["game_id"], "predicted": float(value),
                        "actual": float(row["actual_margin"]), "tier": label})
    return out


def _rms(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values) / len(values))


def scale_for(residuals: list[dict[str, Any]], before_season: int) -> dict[str, Any] | None:
    """Global residual scale from out-of-fold errors of seasons strictly before `before_season`."""
    errors = [row["actual"] - row["predicted"] for row in residuals if row["season"] < before_season]
    if len(errors) < MIN_RESIDUALS:
        return None
    return {"margin_residual_scale": _rms(errors), "games": len(errors),
            "bias": sum(errors) / len(errors)}


def evaluate(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk-forward check of the Gaussian: coverage, sharpness and home-win log loss/Brier."""
    residuals = out_of_fold(history)
    seasons = sorted({row["season"] for row in residuals})
    per_season = []
    pooled = {"n": 0, "ll_gauss": 0.0, "ll_base": 0.0, "br_gauss": 0.0, "br_base": 0.0,
              "hits": {level: 0 for level in LEVELS}, "width": {level: 0.0 for level in LEVELS}}
    for season in seasons:
        scale = scale_for(residuals, season)
        if scale is None:
            continue
        sigma = scale["margin_residual_scale"]
        prior = [row for row in residuals if row["season"] < season]
        base_rate = sum(1 for row in prior if row["actual"] > 0) / len(prior)
        rows = [row for row in residuals if row["season"] == season and row["actual"] != 0]
        stats = {"season": season, "sigma": round(sigma, 3), "n": len(rows),
                 "ll_gauss": 0.0, "ll_base": 0.0, "br_gauss": 0.0, "br_base": 0.0,
                 "coverage": {}}
        for row in rows:
            win = 1.0 if row["actual"] > 0 else 0.0
            p = min(0.999, max(0.001, _phi(row["predicted"] / sigma)))
            q = min(0.999, max(0.001, base_rate))
            for key, prob in (("gauss", p), ("base", q)):
                stats[f"ll_{key}"] += -(win * math.log(prob) + (1 - win) * math.log(1 - prob))
                stats[f"br_{key}"] += (prob - win) ** 2
            error = abs(row["actual"] - row["predicted"])
            for level in LEVELS:
                if error <= _Z[level] * sigma:
                    pooled["hits"][level] += 1
        for level in LEVELS:
            pooled["width"][level] += 2 * _Z[level] * sigma * len(rows)
            hit = sum(1 for row in rows
                      if abs(row["actual"] - row["predicted"]) <= _Z[level] * sigma)
            stats["coverage"][f"{int(level * 100)}%"] = round(hit / len(rows), 3)
        for key in ("ll_gauss", "ll_base", "br_gauss", "br_base"):
            pooled[key] += stats[key]
            stats[key] = round(stats[key] / len(rows), 4)
        pooled["n"] += len(rows)
        per_season.append(stats)
    n = pooled["n"] or 1
    return {
        "model_version": MODEL_VERSION, "games": pooled["n"], "seasons": per_season,
        "pooled": {
            "log_loss_gaussian": round(pooled["ll_gauss"] / n, 4),
            "log_loss_base_rate": round(pooled["ll_base"] / n, 4),
            "brier_gaussian": round(pooled["br_gauss"] / n, 4),
            "brier_base_rate": round(pooled["br_base"] / n, 4),
            "coverage": {f"{int(level * 100)}%": round(pooled["hits"][level] / n, 3)
                         for level in LEVELS},
            "mean_interval_width": {f"{int(level * 100)}%": round(pooled["width"][level] / n, 2)
                                    for level in LEVELS},
        },
    }


def _scale_table(repository, target_season: int) -> dict[str, Any] | None:
    def build():
        history = _historical_rows(repository, target_season=int(target_season))
        return scale_for(out_of_fold(history), int(target_season) + 1)
    return derived_cache.derived(repository, "cfb_uncertainty_scale", build, int(target_season))


def live_packet(repository, *, target_season: int, margin: float | None) -> dict[str, Any] | None:
    """Residual scale, home-win probability and central intervals for a calibrated margin.

    `margin` is the engine's home-minus-away point margin (margin-v2). None when the margin is
    unavailable or there is too little out-of-fold history to trust a scale."""
    if margin is None:
        return None
    scale = _scale_table(repository, target_season)
    if scale is None:
        return None
    sigma = scale["margin_residual_scale"]
    margin = float(margin)
    return {
        "model_version": MODEL_VERSION, "margin": round(margin, 1),
        "margin_residual_scale": round(sigma, 1), "scale_games": scale["games"],
        "home_win_probability": round(min(0.99, max(0.01, _phi(margin / sigma))), 3),
        "intervals": {f"{int(level * 100)}%": [round(margin - _Z[level] * sigma, 1),
                                               round(margin + _Z[level] * sigma, 1)]
                      for level in LEVELS},
    }


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    from sports_aggregator.cfb.repository import CFBRepository
    load_dotenv()
    parser = argparse.ArgumentParser(description="Walk-forward check of CFB margin uncertainty")
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)
    repository = CFBRepository(args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    history = _historical_rows(repository, target_season=9999)
    print(json.dumps(evaluate(history), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
