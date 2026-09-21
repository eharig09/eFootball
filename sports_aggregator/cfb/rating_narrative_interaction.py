"""Research HC Elo, QB Elo and joint HC+QB direction against narrative state.

The analysis is directional and market-relative:
- HC direction = sign(home coach pregame Elo - away coach pregame Elo)
- QB direction = sign(home QB pregame Elo - away QB pregame Elo)
- BOTH direction exists only when HC and QB point to the same side
- Narrative direction = sign(leak-safe narrative_interaction_edge)

A hit means the selected side beat the market-implied margin, not merely won
the game. Narrative edges are rebuilt walk-forward one target season at a time.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.rating_predictive_power import _load_dataset
from sports_aggregator.cfb.repository import CFBRepository

DEFAULT_START_SEASON = 2020
DEFAULT_END_SEASON = 2025

SPREAD_BUCKETS = (
    ("<3", lambda x: x < 3),
    ("3-6.5", lambda x: 3 <= x < 7),
    ("7-13.5", lambda x: 7 <= x < 14),
    ("14+", lambda x: x >= 14),
)


def _direction(value: float | None) -> int:
    if value is None:
        return 0
    return 1 if float(value) > 0 else -1 if float(value) < 0 else 0


def _spread_bucket(market_margin: float | None) -> str | None:
    if market_margin is None:
        return None
    value = abs(float(market_margin))
    for label, predicate in SPREAD_BUCKETS:
        if predicate(value):
            return label
    return None


def _summary(rows: list[dict[str, Any]], direction_key: str) -> dict[str, Any]:
    usable = [
        row for row in rows
        if int(row.get(direction_key) or 0) != 0
        and row.get("market_residual") is not None
    ]
    if not usable:
        return {"n": 0, "wins": 0, "hit_rate": None,
                "mean_aligned_residual": None, "median_aligned_residual": None}
    aligned = [
        float(row["market_residual"]) * int(row[direction_key])
        for row in usable
    ]
    wins = sum(value > 0 for value in aligned)
    return {
        "n": len(aligned),
        "wins": wins,
        "hit_rate": round(wins / len(aligned), 4),
        "mean_aligned_residual": round(sum(aligned) / len(aligned), 3),
        "median_aligned_residual": round(float(median(aligned)), 3),
    }


def _agreement_summary(rows: list[dict[str, Any]], direction_key: str) -> dict[str, Any]:
    usable = [
        row for row in rows
        if int(row.get(direction_key) or 0) != 0
        and int(row.get("narrative_direction") or 0) != 0
    ]
    agree = [
        row for row in usable
        if int(row[direction_key]) == int(row["narrative_direction"])
    ]
    disagree = [
        row for row in usable
        if int(row[direction_key]) == -int(row["narrative_direction"])
    ]
    return {
        "all_with_narrative": _summary(usable, direction_key),
        "narrative_confirms": _summary(agree, direction_key),
        "narrative_disagrees": _summary(disagree, direction_key),
        "agreement_rate": (
            round(len(agree) / len(usable), 4) if usable else None
        ),
    }


def _walk_forward_narrative(
    repository: CFBRepository,
    start_season: int,
    end_season: int,
) -> dict[int, dict[str, Any]]:
    """One target season at a time so narrative calibration uses prior history."""
    out: dict[int, dict[str, Any]] = {}
    for season in range(int(start_season), int(end_season) + 1):
        rows = ipl.build_lens_rows(repository, test_season=season)
        for row in rows:
            if int(row.get("season", -1)) != season:
                continue
            out[int(row["game_id"])] = row
    return out


def _joined_rows(
    repository: CFBRepository,
    *,
    start_season: int,
    end_season: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    ratings = _load_dataset(
        repository,
        start_season=int(start_season),
        end_season=int(end_season),
    )
    narrative = _walk_forward_narrative(
        repository,
        int(start_season),
        int(end_season),
    )

    output = []
    coverage = {
        "hc_rows": len(ratings),
        "hc_qb_rows": 0,
        "narrative_rows": len(narrative),
        "joined_hc_narrative": 0,
        "joined_qb_narrative": 0,
        "joined_both_agree_narrative": 0,
    }
    for row in ratings:
        gid = int(row["game_id"])
        nrow = narrative.get(gid)
        if nrow is None:
            continue
        hc_direction = _direction(row.get("hc_diff"))
        qb_direction = _direction(row.get("qb_diff"))
        if row.get("qb_diff") is not None:
            coverage["hc_qb_rows"] += 1
        both_direction = (
            hc_direction
            if hc_direction != 0 and hc_direction == qb_direction
            else 0
        )
        narrative_direction = _direction(nrow.get("narrative_interaction_edge"))
        market_residual = None
        if row.get("market_margin") is not None:
            market_residual = (
                float(row["actual_margin"]) - float(row["market_margin"])
            )
        joined = {
            **row,
            "market_residual": market_residual,
            "spread_bucket": _spread_bucket(row.get("market_margin")),
            "hc_direction": hc_direction,
            "qb_direction": qb_direction,
            "both_direction": both_direction,
            "narrative_edge": nrow.get("narrative_interaction_edge"),
            "narrative_direction": narrative_direction,
        }
        output.append(joined)
        if hc_direction and narrative_direction:
            coverage["joined_hc_narrative"] += 1
        if qb_direction and narrative_direction:
            coverage["joined_qb_narrative"] += 1
        if both_direction and narrative_direction:
            coverage["joined_both_agree_narrative"] += 1
    return output, coverage


def _by_season(rows: list[dict[str, Any]], direction_key: str) -> dict[str, Any]:
    return {
        str(season): _agreement_summary(
            [row for row in rows if int(row["season"]) == season],
            direction_key,
        )
        for season in sorted({int(row["season"]) for row in rows})
    }


def _by_spread(rows: list[dict[str, Any]], direction_key: str) -> dict[str, Any]:
    return {
        label: _agreement_summary(
            [row for row in rows if row.get("spread_bucket") == label],
            direction_key,
        )
        for label, _ in SPREAD_BUCKETS
    }


def report(
    repository: CFBRepository,
    *,
    start_season: int = DEFAULT_START_SEASON,
    end_season: int = DEFAULT_END_SEASON,
) -> dict[str, Any]:
    rows, coverage = _joined_rows(
        repository,
        start_season=int(start_season),
        end_season=int(end_season),
    )
    signals = {}
    for label, key in (
        ("hc", "hc_direction"),
        ("qb", "qb_direction"),
        ("both_hc_qb_agree", "both_direction"),
    ):
        signals[label] = {
            "overall": _agreement_summary(rows, key),
            "by_season": _by_season(rows, key),
            "by_spread": _by_spread(rows, key),
        }

    narrative_only = _summary(
        [row for row in rows if row.get("narrative_direction")],
        "narrative_direction",
    )
    return {
        "version": "cfb-hc-qb-narrative-interaction-v1",
        "start_season": int(start_season),
        "end_season": int(end_season),
        "coverage": coverage,
        "narrative_only_market_relative": narrative_only,
        "signals": signals,
        "notes": [
            "Research-only; no convergence or routing rule is changed.",
            "BOTH means HC and QB point to the same side; split HC/QB games have no BOTH direction.",
            "Narrative direction comes from narrative_interaction_edge rebuilt separately for each target season.",
            "A hit means the selected side beat the market-implied margin; pushes are non-wins under the existing convention.",
            "Agreement/disagreement is descriptive and must not be promoted without a frozen out-of-sample test.",
        ],
    }
