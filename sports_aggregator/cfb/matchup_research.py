"""Presentation packet for spread/total research on a live matchup.

This module intentionally separates:
- live matchup state (current model + stored market snapshot),
- historical research benchmarks,
- limitations / applicability notes.

It does not convert historical hit rates into a recommendation.
"""
from __future__ import annotations

from typing import Any

from sports_aggregator.cfb import market_ats_totals as mat
from sports_aggregator.cfb import totals_divergence_matrix as tdm
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.repository import CFBRepository


SPREAD_RESEARCH = {
    "full_convergence": {
        "label": "Full Convergence",
        "record": "77-64-2",
        "win_rate": 54.61,
        "n": 143,
        "mean_residual": 4.300,
        "note": "Frozen 2021-2025 Full Convergence: Margin Power >=1σ plus structural and Line Elo confirmation.",
    },
    "full_convergence_lt14": {
        "label": "Full Convergence · market spread <14",
        "record": "64-45-2",
        "win_rate": 58.72,
        "n": 111,
        "mean_residual": None,
        "note": "2021-2025 applicability subset. The 14+ region underperformed, so it remains a caution boundary rather than a fade rule.",
    },
}

TOTAL_RESEARCH = {
    "overall": {
        "label": "Football Lab totals vs close",
        "record": "1405-1226-12",
        "win_rate": 53.40,
        "n": 2643,
        "mean_residual": 1.419,
        "note": "Pooled closing-line result. 2025 weakened to 51.30%, so treat the pooled edge as non-stationary.",
    },
    "narrative_adjusted": {
        "label": "Narrative-adjusted totals",
        "record": "1403-1228-12",
        "win_rate": 53.33,
        "n": 2643,
        "mean_residual": 1.637,
        "note": "Direct additive narrative adjustment did not improve win rate or MAE; narrative remains context only.",
    },
}

TOTAL_REGIME_BENCHMARKS = {
    ("3-4.99", "away_lt1"): (60.71, 85, 3.539),
    ("3-4.99", "unchanged"): (55.13, 78, 3.309),
    ("3-4.99", "toward_lt1"): (54.93, 71, 3.461),
    ("3-4.99", "toward_1_plus"): (53.55, 156, 1.193),
    ("3-4.99", "away_1_plus"): (53.66, 205, 0.609),
    ("5-7.99", "toward_1_plus"): (57.32, 157, 2.377),
    ("5-7.99", "toward_lt1"): (56.96, 79, 3.922),
    ("5-7.99", "unchanged"): (55.17, 87, 1.117),
    ("5-7.99", "away_lt1"): (51.14, 88, -0.695),
    ("5-7.99", "away_1_plus"): (51.14, 176, 2.040),
    ("8+", "away_1_plus"): (61.74, 115, 3.302),
    ("8+", "away_lt1"): (59.46, 37, 3.171),
    ("8+", "unchanged"): (53.70, 54, 2.060),
    ("8+", "toward_1_plus"): (50.54, 93, 0.149),
    ("8+", "toward_lt1"): (40.38, 52, 0.076),
}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _market_totals(lines: dict[str, Any]) -> tuple[float | None, float | None]:
    providers = lines.get("providers") or []
    opens = [float(row["over_under_open"]) for row in providers
             if row.get("over_under_open") is not None]
    closes = [float(row["over_under"]) for row in providers
              if row.get("over_under") is not None]
    return _mean(opens), _mean(closes)


def _calibrated_live_total(
    repository: CFBRepository,
    *,
    season: int,
    raw_total: float,
) -> dict[str, Any]:
    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                """SELECT game_id,side,season,projected_offensive_points,actual_score_points
                   FROM cfb_projection_backtest
                   WHERE backtest_version=? AND season < ?""",
                (BACKTEST_VERSION, int(season)),
            )
        ]
    grouped: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["game_id"]), {})[str(row["side"])] = row
    pairs = []
    for sides in grouped.values():
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        vals = (
            home.get("projected_offensive_points"),
            away.get("projected_offensive_points"),
            home.get("actual_score_points"),
            away.get("actual_score_points"),
        )
        if any(v is None for v in vals):
            continue
        pairs.append((
            float(home["projected_offensive_points"]) + float(away["projected_offensive_points"]),
            float(home["actual_score_points"]) + float(away["actual_score_points"]),
        ))
    fit = mat._linear_fit(pairs)
    if not fit:
        return {"value": raw_total, "calibrated": False, "training_games": len(pairs)}
    return {
        "value": float(fit["intercept"]) + float(fit["slope"]) * float(raw_total),
        "calibrated": True,
        "training_games": int(fit["n"]),
        "intercept": float(fit["intercept"]),
        "slope": float(fit["slope"]),
    }


def _edge_bucket(value: float) -> str:
    return mat._bucket(abs(float(value)), mat.EDGE_BUCKETS)


def matchup_research_packet(
    repository: CFBRepository,
    game: dict[str, Any],
    projection: dict[str, Any],
    lines: dict[str, Any],
) -> dict[str, Any]:
    away_points = (projection.get("away") or {}).get("expected_points")
    home_points = (projection.get("home") or {}).get("expected_points")
    if away_points is None or home_points is None:
        return {
            "available": False,
            "reason": "Projection points unavailable.",
            "spread_research": SPREAD_RESEARCH,
            "total_research": TOTAL_RESEARCH,
        }

    raw_total = float(away_points) + float(home_points)
    projected_home_margin = float(home_points) - float(away_points)
    spread = lines.get("consensus_spread")
    market_home_margin = -float(spread) if spread is not None else None
    spread_edge_home = (
        projected_home_margin - market_home_margin
        if market_home_margin is not None else None
    )
    spread_side = (
        game["home_team"] if spread_edge_home is not None and spread_edge_home > 0
        else game["away_team"] if spread_edge_home is not None and spread_edge_home < 0
        else None
    )

    calibration = _calibrated_live_total(
        repository, season=int(game["season"]), raw_total=raw_total)
    projected_total = float(calibration["value"])
    open_total, close_total = _market_totals(lines)

    open_edge = (
        projected_total - float(open_total) if open_total is not None else None
    )
    close_edge = (
        projected_total - float(close_total) if close_total is not None else None
    )
    model_direction = (
        "over" if open_edge is not None and open_edge > 0
        else "under" if open_edge is not None and open_edge < 0
        else None
    )
    aligned_move = None
    movement_state = None
    if open_total is not None and close_total is not None and model_direction:
        direction = 1 if model_direction == "over" else -1
        aligned_move = (float(close_total) - float(open_total)) * direction
        movement_state = tdm._movement_state(aligned_move)

    opening_bucket = _edge_bucket(open_edge) if open_edge is not None else None
    regime = TOTAL_REGIME_BENCHMARKS.get((opening_bucket, movement_state))
    regime_packet = None
    if regime:
        regime_packet = {
            "edge_bucket": opening_bucket,
            "movement_state": movement_state,
            "win_rate": regime[0],
            "n": regime[1],
            "mean_residual": regime[2],
            "label": f"{opening_bucket} edge · {movement_state.replace('_', ' ')}",
        }

    return {
        "available": True,
        "spread": {
            "projected_home_margin": projected_home_margin,
            "market_home_margin": market_home_margin,
            "model_market_edge_home": spread_edge_home,
            "model_side": spread_side,
            "market_spread": float(spread) if spread is not None else None,
            "market_spread_abs": abs(float(spread)) if spread is not None else None,
            "inside_lt14_applicability": (
                abs(float(spread)) < 14 if spread is not None else None
            ),
        },
        "totals": {
            "raw_projected_total": raw_total,
            "calibrated_projected_total": projected_total,
            "calibration": calibration,
            "opening_total": open_total,
            "closing_total": close_total,
            "opening_edge": open_edge,
            "closing_edge": close_edge,
            "opening_edge_bucket": opening_bucket,
            "model_direction": model_direction,
            "aligned_market_move": aligned_move,
            "movement_state": movement_state,
            "regime_benchmark": regime_packet,
        },
        "spread_research": SPREAD_RESEARCH,
        "total_research": TOTAL_RESEARCH,
        "research_notes": [
            "Spread Full Convergence is a historical research architecture; the live page does not label the current game Full Convergence unless all frozen component lenses are available.",
            "The <14 spread condition is shown as an applicability boundary, not a standalone signal.",
            "Totals regime benchmarks are descriptive historical cells, not confidence scores.",
            "Direct totals narrative adjustment did not improve the pooled model and is not applied to the live projection.",
            "Benchmark ROI figures are omitted here because historical spread/total price was not stored; records and hit rates are line-settlement metrics.",
        ],
    }
