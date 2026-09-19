"""Extreme-tail walk-forward diagnostics for the multi-lens composite."""
from __future__ import annotations

import math
from statistics import median
from typing import Any

from sports_aggregator.cfb import narrative_composite as nc
from sports_aggregator.cfb import narrative_shapes_v2 as v2
from sports_aggregator.cfb.repository import CFBRepository

MAGNITUDE_BUCKETS = (
    (0.0, 0.50, "<0.50"),
    (0.50, 0.75, "0.50-0.75"),
    (0.75, 1.00, "0.75-1.00"),
    (1.00, 1.25, "1.00-1.25"),
    (1.25, 1.50, "1.25-1.50"),
    (1.50, float("inf"), ">=1.50"),
)
EXTREME_THRESHOLDS = (1.00, 1.25, 1.50)
STRONG_Z_THRESHOLD = 0.50


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    w = pos - lo
    return vals[lo] * (1 - w) + vals[hi] * w


def _signal_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    total = len(rows)
    for key in nc.SIGNAL_KEYS:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        unique = sorted(set(round(v, 8) for v in vals))
        out[key] = {
            "rows": len(vals),
            "coverage_rate": round(len(vals) / total, 4) if total else 0.0,
            "unique_values": len(unique),
            "mean": round(sum(vals) / len(vals), 4) if vals else None,
            "std": round(nc._std(vals), 4) if vals else None,
            "min": round(min(vals), 4) if vals else None,
            "max": round(max(vals), 4) if vals else None,
            "constant_or_missing": (not vals or len(unique) <= 1),
        }
    return out


def _score_with_keys(row: dict[str, Any], scales: dict[str, float],
                     keys: tuple[str, ...]) -> dict[str, Any]:
    zs = {}
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        zs[key] = float(value) / float(scales.get(key) or 1.0)
    values = list(zs.values())
    if not values:
        return {
            "available": 0, "score": None, "direction": 0,
            "agreement_ratio": None, "strong_positive": 0, "strong_negative": 0,
            "strong_agreement_count": 0, "zs": zs,
        }
    score = sum(values) / len(values)
    direction = 1 if score > 0 else -1 if score < 0 else 0
    agree = sum(1 for z in values if direction and ((z > 0) == (direction > 0)))
    strong_positive = sum(1 for z in values if z >= STRONG_Z_THRESHOLD)
    strong_negative = sum(1 for z in values if z <= -STRONG_Z_THRESHOLD)
    strong_agreement = strong_positive if direction > 0 else strong_negative if direction < 0 else 0
    return {
        "available": len(values),
        "score": score,
        "direction": direction,
        "agreement_ratio": agree / len(values) if direction else 0.0,
        "strong_positive": strong_positive,
        "strong_negative": strong_negative,
        "strong_agreement_count": strong_agreement,
        "zs": zs,
    }


def _bucket_metrics(rows: list[dict[str, Any]], scales: dict[str, float],
                    slope: float, *, keys: tuple[str, ...] = nc.SIGNAL_KEYS,
                    low: float, high: float) -> dict[str, Any]:
    chosen = []
    for row in rows:
        s = _score_with_keys(row, scales, keys)
        if s["score"] is None:
            continue
        mag = abs(float(s["score"]))
        if not (float(low) <= mag < float(high)):
            continue
        actual = float(row["market_margin_residual"])
        aligned = actual * int(s["direction"])
        pred = float(slope) * float(s["score"])
        chosen.append((s, actual, aligned, pred))
    if not chosen:
        return {"n": 0}
    aligned_vals = [x[2] for x in chosen]
    market_errors = [-x[1] for x in chosen]
    model_errors = [x[3] - x[1] for x in chosen]
    return {
        "n": len(chosen),
        "directional_hit_rate": round(sum(v > 0 for v in aligned_vals) / len(chosen), 4),
        "mean_aligned_residual": round(sum(aligned_vals) / len(chosen), 3),
        "median_aligned_residual": round(float(median(aligned_vals)), 3),
        "p25_aligned_residual": round(float(_percentile(aligned_vals, 0.25)), 3),
        "p75_aligned_residual": round(float(_percentile(aligned_vals, 0.75)), 3),
        "worst_aligned_miss": round(min(aligned_vals), 3),
        "best_aligned_result": round(max(aligned_vals), 3),
        "mean_agreement_ratio": round(
            sum(float(x[0]["agreement_ratio"]) for x in chosen) / len(chosen), 4),
        "mean_available_signals": round(
            sum(int(x[0]["available"]) for x in chosen) / len(chosen), 3),
        "mean_strong_agreement_count": round(
            sum(int(x[0]["strong_agreement_count"]) for x in chosen) / len(chosen), 3),
        "market_mae": round(sum(abs(e) for e in market_errors) / len(chosen), 4),
        "composite_mae": round(sum(abs(e) for e in model_errors) / len(chosen), 4),
        "mae_delta": round(
            sum(abs(e) for e in model_errors) / len(chosen)
            - sum(abs(e) for e in market_errors) / len(chosen), 4),
    }


def _threshold_metrics(rows: list[dict[str, Any]], scales: dict[str, float],
                       slope: float, threshold: float,
                       *, keys: tuple[str, ...] = nc.SIGNAL_KEYS) -> dict[str, Any]:
    return _bucket_metrics(
        rows, scales, slope, keys=keys, low=float(threshold), high=float("inf"))


def _strong_vote_ladder(rows: list[dict[str, Any]], scales: dict[str, float]) -> list[dict[str, Any]]:
    result = []
    for minimum in (1, 2, 3, 4, 5):
        aligned = []
        agreements = []
        for row in rows:
            s = _score_with_keys(row, scales, nc.SIGNAL_KEYS)
            if s["score"] is None or s["strong_agreement_count"] < minimum:
                continue
            aligned.append(float(row["market_margin_residual"]) * int(s["direction"]))
            agreements.append(float(s["agreement_ratio"]))
        result.append({
            "min_strong_agreeing_lenses": minimum,
            "strong_z_threshold": STRONG_Z_THRESHOLD,
            "n": len(aligned),
            "directional_hit_rate": round(sum(v > 0 for v in aligned) / len(aligned), 4)
            if aligned else None,
            "mean_aligned_residual": round(sum(aligned) / len(aligned), 3) if aligned else None,
            "median_aligned_residual": round(float(median(aligned)), 3) if aligned else None,
            "mean_agreement_ratio": round(sum(agreements) / len(agreements), 4)
            if agreements else None,
        })
    return result


def _walk_forward_year(repository: CFBRepository, *, target_season: int,
                       all_narrative_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    prior = [r for r in all_narrative_rows if int(r["season"]) < int(target_season)]
    if not prior:
        return None
    validation_for_line = int(target_season) - 1
    line_rate, _ = v2._choose_line_rate(
        [r for r in all_narrative_rows if int(r["season"]) <= int(target_season)],
        validation_season=validation_for_line,
    )
    games = nc._composite_games(
        repository, test_season=int(target_season), line_rate=line_rate)
    train = [r for r in games if int(r["season"]) < int(target_season)]
    test = [r for r in games if int(r["season"]) == int(target_season)]
    if not train or not test:
        return None
    scales = nc._normalization(train)
    slope = nc._fit_slope(train, scales)
    buckets = []
    for low, high, label in MAGNITUDE_BUCKETS:
        buckets.append({
            "bucket": label,
            "low": low,
            "high": None if math.isinf(high) else high,
            "metrics": _bucket_metrics(test, scales, slope, low=low, high=high),
        })
    thresholds = {
        str(threshold): _threshold_metrics(test, scales, slope, threshold)
        for threshold in EXTREME_THRESHOLDS
    }
    return {
        "season": int(target_season),
        "training_seasons": [
            min(int(r["season"]) for r in train),
            max(int(r["season"]) for r in train),
        ],
        "line_elo_learning_rate": line_rate,
        "training_rows": len(train),
        "test_rows": len(test),
        "signal_audit": _signal_audit(train),
        "magnitude_buckets": buckets,
        "extreme_thresholds": thresholds,
        "strong_vote_ladder": _strong_vote_ladder(test, scales),
        "training_scales": {k: round(v, 4) for k, v in scales.items()},
        "calibration_slope": round(slope, 4),
    }


def _ablation_report(repository: CFBRepository, *,
                     test_season: int,
                     narrative_rows: list[dict[str, Any]]) -> dict[str, Any]:
    line_rate, _ = v2._choose_line_rate(
        narrative_rows, validation_season=int(test_season) - 1)
    games = nc._composite_games(
        repository, test_season=int(test_season), line_rate=line_rate)
    train = [r for r in games if int(r["season"]) < int(test_season)]
    test = [r for r in games if int(r["season"]) == int(test_season)]
    result = {}
    variants = {"all_signals": nc.SIGNAL_KEYS}
    for omitted in nc.SIGNAL_KEYS:
        variants[f"without_{omitted}"] = tuple(k for k in nc.SIGNAL_KEYS if k != omitted)
    for name, keys in variants.items():
        scales = {key: nc._std(r.get(key) for r in train if r.get(key) is not None) for key in keys}
        # Fit a one-dimensional calibration slope on the recomputed reduced composite.
        pairs = []
        for row in train:
            s = _score_with_keys(row, scales, keys)
            if s["score"] is not None:
                pairs.append((float(s["score"]), float(row["market_margin_residual"])))
        denom = sum(x * x for x, _ in pairs)
        slope = sum(x * y for x, y in pairs) / denom if denom else 0.0
        result[name] = {
            "signals": list(keys),
            "calibration_slope": round(slope, 4),
            "thresholds": {
                str(threshold): _threshold_metrics(
                    test, scales, slope, threshold, keys=keys)
                for threshold in EXTREME_THRESHOLDS
            },
        }
    return result


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    narrative_rows = v2._load_rows(repository)
    seasons = sorted({int(r["season"]) for r in narrative_rows})
    target_years = [s for s in seasons if s >= min(seasons) + 1 and s <= int(test_season)]
    walk_forward = []
    for season in target_years:
        item = _walk_forward_year(
            repository, target_season=season, all_narrative_rows=narrative_rows)
        if item:
            walk_forward.append(item)

    final_year = next((x for x in walk_forward if x["season"] == int(test_season)), None)
    final_audit = final_year["signal_audit"] if final_year else {}

    return {
        "version": "extreme-tail-composite-v1",
        "test_season": int(test_season),
        "magnitude_buckets": [label for _, _, label in MAGNITUDE_BUCKETS],
        "extreme_thresholds": list(EXTREME_THRESHOLDS),
        "strong_vote_z_threshold": STRONG_Z_THRESHOLD,
        "walk_forward_years": walk_forward,
        "final_signal_coverage_audit": final_audit,
        "leave_one_signal_out_ablation": _ablation_report(
            repository, test_season=int(test_season), narrative_rows=narrative_rows),
        "notes": [
            "Each target season is normalized and calibrated using only earlier seasons.",
            "Magnitude bucket boundaries are fixed in advance and are never selected on the target season.",
            "Strong votes require |z| >= 0.50; weak same-direction signals do not count as strong.",
            "Leave-one-signal-out ablations recompute the reduced composite and calibration slope from pretest history.",
            "FPI/CORE audit explicitly reports missing/constant signals so they cannot masquerade as real lenses.",
        ],
    }
