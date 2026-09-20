"""Early Convergence v2: stage-specific standardization for Weeks 2-3.

Uses Early Margin Power's Bayesian margin only in Weeks 2-3, standardizes its
market edge against prior seasons at the same week, and retains the frozen
structural and Line Elo confirmation concept.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.cfb import conditional_convergence as cc
from sports_aggregator.cfb import early_margin_power as emp
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.repository import CFBRepository

EARLY_SIGNAL_WEEKS = (2, 3)
THRESHOLD_Z = 1.0


def _std(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 20:
        return None
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) or None


def _early_edge_rows(repository: CFBRepository, from_season: int, to_season: int) -> list[dict[str, Any]]:
    rows = emp._early_rows(repository, from_season, to_season)
    out = []
    for row in rows:
        if int(row["week"]) not in EARLY_SIGNAL_WEEKS:
            continue
        margin = row.get("bayesian_margin")
        market = row.get("market_home_margin")
        if margin is None or market is None:
            continue
        out.append({**row, "early_margin_power_edge": float(margin) - float(market)})
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    vals = [float(r["aligned_residual"]) for r in rows]
    wins = sum(v > 0 for v in vals)
    losses = sum(v < 0 for v in vals)
    pushes = len(vals) - wins - losses
    return {
        "n": len(vals),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_ex_pushes": round(wins / (wins + losses), 4) if wins + losses else None,
        "mean_aligned_residual": round(sum(vals) / len(vals), 3),
    }


def _fbs_teams(repository: CFBRepository) -> set[str]:
    with repository._reader() as connection:
        return {
            str(r["school"])
            for r in connection.execute(
                """SELECT school FROM teams
                   WHERE LOWER(COALESCE(classification,''))='fbs'"""
            )
        }


def report(repository: CFBRepository, *, from_season: int = 2022,
           to_season: int = 2025) -> dict[str, Any]:
    early = _early_edge_rows(repository, int(from_season), int(to_season))
    fbs = _fbs_teams(repository)
    lens_rows = ipl.build_lens_rows(repository, test_season=int(to_season))
    lens_by_game = {int(r["game_id"]): r for r in lens_rows}

    by_season = {}
    pooled_selected = []
    scale_rows = []

    for season in range(int(from_season), int(to_season) + 1):
        train_early = [r for r in early if int(r["season"]) < season]
        test_early = [r for r in early if int(r["season"]) == season]
        week_scales = {
            week: _std(
                r["early_margin_power_edge"]
                for r in train_early if int(r["week"]) == week
            )
            for week in EARLY_SIGNAL_WEEKS
        }

        train_lens = [r for r in lens_rows if int(r["season"]) < season]
        structural_scales = cc._lens_scales(train_lens)
        selected = []
        candidates = 0
        for row in test_early:
            week = int(row["week"])
            early_scale = week_scales.get(week)
            if early_scale is None:
                continue
            candidates += 1
            edge = float(row["early_margin_power_edge"])
            z = edge / float(early_scale)
            if abs(z) < THRESHOLD_Z:
                continue
            direction = 1 if z > 0 else -1

            base = lens_by_game.get(int(row["game_id"]))
            if not base:
                continue
            structural = []
            for key in cc.STRUCTURAL_KEYS:
                if key in structural_scales and base.get(key) is not None:
                    structural.append(float(base[key]) / float(structural_scales[key]))
            if len(structural) < cc.MIN_STRUCTURAL_COMPONENTS:
                continue
            structural_z = sum(structural) / len(structural)
            if structural_z * direction <= 0:
                continue

            if (
                "line_elo_edge" not in structural_scales
                or base.get("line_elo_edge") is None
            ):
                continue
            line_z = float(base["line_elo_edge"]) / float(structural_scales["line_elo_edge"])
            if line_z * direction <= 0:
                continue

            aligned = (
                float(row["actual_home_margin"]) - float(row["market_home_margin"])
            ) * direction
            item = {
                "game_id": int(row["game_id"]),
                "season": season,
                "week": week,
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "early_margin_power_edge": round(edge, 3),
                "early_margin_power_z": round(z, 3),
                "structural_z": round(structural_z, 3),
                "line_elo_z": round(line_z, 3),
                "aligned_residual": round(aligned, 3),
                "result": "win" if aligned > 0 else "loss" if aligned < 0 else "push",
            }
            selected.append(item)
            pooled_selected.append(item)

        by_week = {
            str(week): _summary([r for r in selected if int(r["week"]) == week])
            for week in EARLY_SIGNAL_WEEKS
        }
        by_season[str(season)] = {
            "eligible_candidates": candidates,
            "week_scales": {
                str(k): round(v, 4) if v is not None else None
                for k, v in week_scales.items()
            },
            "overall": _summary(selected),
            "by_week": by_week,
        }
        scale_rows.append({
            "season": season,
            "week_2_scale": week_scales[2],
            "week_3_scale": week_scales[3],
        })

    peer_selected = [
        r for r in pooled_selected
        if r["home_team"] in fbs and r["away_team"] in fbs
    ]
    peer_by_season = {
        str(season): _summary([
            r for r in peer_selected if int(r["season"]) == season
        ])
        for season in range(int(from_season), int(to_season) + 1)
    }

    return {
        "version": "early-convergence-v2-peer-audit",
        "window": [int(from_season), int(to_season)],
        "signal_weeks": list(EARLY_SIGNAL_WEEKS),
        "threshold_z": THRESHOLD_Z,
        "standardization": "prior-season Early Margin Power edge distribution at the same week",
        "confirmation": "frozen structural cluster plus Line Elo direction",
        "by_season": by_season,
        "pooled": _summary(pooled_selected),
        "pooled_by_week": {
            str(week): _summary([r for r in pooled_selected if int(r["week"]) == week])
            for week in EARLY_SIGNAL_WEEKS
        },
        "peer_fbs_only": {
            "classification_value": "fbs",
            "fbs_team_count": len(fbs),
            "pooled": _summary(peer_selected),
            "by_week": {
                str(week): _summary([
                    r for r in peer_selected if int(r["week"]) == week
                ])
                for week in EARLY_SIGNAL_WEEKS
            },
            "by_season": peer_by_season,
            "selected_games": peer_selected,
        },
        "selected_games": pooled_selected,
        "notes": [
            "Week 1 is context only and never qualifies.",
            "Only Bayesian Early Margin Power is used as the primary signal.",
            "Early Margin Power is standardized against prior seasons at the same week.",
            "Structural and Line Elo confirmation logic remains unchanged.",
            "Frozen standard Margin Power and Full Convergence are not modified.",
            "peer_fbs_only is a diagnostic subset requiring both teams to have teams.classification='fbs'.",
        ],
    }
