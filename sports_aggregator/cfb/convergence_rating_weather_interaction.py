"""Does HC/QB Elo agreement, or weather, modify the existing CFB
convergence signal's hit rate -- particularly in the 3-6.5 spread bucket,
where convergence's own validated hit rate is weak (44.7% in the
quality-audit research, well below the 52.9% pooled "Full Convergence"
baseline and below breakeven against standard vig).

This is a small-sample study by construction: "Full Convergence" is
deliberately a rare, high-conviction state (153 games total, 2020-2025),
so any further split by elo agreement or weather cuts an already-small
sample into cells that can be single digits to a few dozen. Every result
below carries its own n; treat anything under ~20 per cell as directional,
not conclusive, and say so rather than letting a clean-looking percentage
imply more confidence than the sample supports.

Convergence's own selected_side/aligned_residual/hit are already
leak-safe walk-forward (conditional_convergence.py / convergence_robustness.py).
The HC/QB Elo combined signal and weather readings joined in here are the
same leak-safe pregame snapshots used throughout rating_predictive_power.py
and weather_market_impact.py. Nothing here is refit or recalibrated --
this only asks whether slicing convergence's existing, already-validated
picks by these two additional variables reveals a pattern worth possibly
using as a filter, not whether a new combined model beats either alone.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import convergence_action_policy as cap
from sports_aggregator.cfb.rating_predictive_power import _load_dataset, _scored_rows
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.weather_market_impact import PRECIP_BUCKETS, TEMP_BUCKETS, WIND_BUCKETS


def _joined_rows(repository: CFBRepository, *, test_season: int = 2025,
                 elo_start_season: int = 2015) -> list[dict[str, Any]]:
    convergence_rows = cap._full_rows(repository, test_season=test_season)
    elo_rows = {
        r["game_id"]: r
        for r in _load_dataset(repository, start_season=elo_start_season, end_season=test_season)
    }
    combined_z = {
        r["game_id"]: r["combined_z_avg"]
        for r in _scored_rows(list(elo_rows.values()))
    }

    out = []
    for row in convergence_rows:
        gid = row["game_id"]
        elo = elo_rows.get(gid)
        z = combined_z.get(gid)
        merged = dict(row)
        merged["combined_z_avg"] = z
        # Orient the Elo signal to convergence's own pick: positive means
        # the combined HC/QB signal agrees with the side convergence chose.
        merged["elo_aligned"] = (
            z if row["selected_side"] == "home" else -z
        ) if z is not None else None
        merged["sustained_wind"] = elo.get("sustained_wind") if elo else None
        merged["temperature"] = elo.get("temperature") if elo else None
        merged["precipitation_amount"] = elo.get("precipitation_amount") if elo else None
        out.append(merged)
    return out


def _hit_rate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    hits = sum(1 for r in rows if r["hit"])
    return {
        "n": n, "hits": hits,
        "hit_rate": round(hits / n, 4),
        "mean_aligned_residual": round(sum(r["aligned_residual"] for r in rows) / n, 3),
    }


def elo_agreement_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Does convergence hit more often when the HC/QB Elo signal happens to
    agree with the side convergence picked -- overall, and specifically
    within the 3-6.5 bucket where convergence is otherwise weak."""
    with_elo = [r for r in rows if r.get("elo_aligned") is not None]

    def split(group: list[dict[str, Any]]) -> dict[str, Any]:
        agree = [r for r in group if r["elo_aligned"] > 0]
        disagree = [r for r in group if r["elo_aligned"] < 0]
        return {
            "elo_agrees_with_pick": _hit_rate(agree),
            "elo_disagrees_with_pick": _hit_rate(disagree),
            "all_regardless_of_elo": _hit_rate(group),
        }

    by_bucket = {}
    for bucket in ("<3", "3-6.5", "7-13.5", "14+"):
        by_bucket[bucket] = split([r for r in with_elo if r["spread_bucket"] == bucket])

    return {
        "n_with_elo_signal": len(with_elo),
        "overall": split(with_elo),
        "by_spread_bucket": by_bucket,
    }


def _weather_bucket_label(row: dict[str, Any], field: str, buckets: tuple) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    for label, predicate in buckets:
        if predicate(value):
            return label
    return None


def weather_modifier_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Does convergence hit more or less often in specific weather
    conditions -- overall, and within the 3-6.5 bucket."""
    def by_dimension(pool: list[dict[str, Any]]) -> dict[str, Any]:
        out = {}
        for name, field, buckets in (
            ("wind", "sustained_wind", WIND_BUCKETS),
            ("precipitation", "precipitation_amount", PRECIP_BUCKETS),
            ("temperature", "temperature", TEMP_BUCKETS),
        ):
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in pool:
                label = _weather_bucket_label(r, field, buckets)
                if label:
                    grouped[label].append(r)
            out[name] = {label: _hit_rate(grouped.get(label, [])) for label, _ in buckets}
        return out

    return {
        "all_full_convergence_games": by_dimension(rows),
        "spread_3_6_5_only": by_dimension([r for r in rows if r["spread_bucket"] == "3-6.5"]),
    }


def combined_filter_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The practical question: restricted to the weak 3-6.5 bucket, does
    requiring Elo agreement (as a simple pass/fail filter on top of an
    already-fired convergence pick) turn a below-breakeven bucket into a
    usable one, and how many picks does that filter leave standing."""
    bucket = [r for r in rows if r["spread_bucket"] == "3-6.5" and r.get("elo_aligned") is not None]
    agree = [r for r in bucket if r["elo_aligned"] > 0]
    disagree = [r for r in bucket if r["elo_aligned"] <= 0]
    return {
        "spread_3_6_5_baseline": _hit_rate(bucket),
        "spread_3_6_5_filtered_to_elo_agreement": _hit_rate(agree),
        "spread_3_6_5_filtered_to_elo_disagreement": _hit_rate(disagree),
        "note": (
            "n this small (order of a few dozen split into two cells) means "
            "a double-digit swing in hit rate can easily be noise -- read "
            "this as 'worth tracking with more seasons,' not as a settled edge."
        ),
    }


def report(repository: CFBRepository, *, test_season: int = 2025, elo_start_season: int = 2015) -> dict[str, Any]:
    rows = _joined_rows(repository, test_season=test_season, elo_start_season=elo_start_season)
    return {
        "version": "cfb-convergence-rating-weather-interaction-v1",
        "test_season": test_season,
        "n_full_convergence_games": len(rows),
        "elo_agreement": elo_agreement_report(rows),
        "weather_modifier": weather_modifier_report(rows),
        "combined_filter_on_3_6_5": combined_filter_report(rows),
    }
