"""Audit where a target CFB season drops out of the research pipeline.

This is diagnostic-only. It does not mutate or backfill any data. The report
checks raw/derived SQLite tables and then executes the same derived builders
used by the convergence research so the first zero-coverage stage is explicit.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

from sports_aggregator.cfb import convergence_four_signal as four
from sports_aggregator.cfb import convergence_robustness as cr
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.rating_predictive_power import _load_dataset, _scored_rows
from sports_aggregator.cfb.repository import CFBRepository

DEFAULT_SEASON = 2026


def _safe_sql_count(
    repository: CFBRepository,
    sql: str,
    params: tuple[Any, ...] = (),
) -> dict[str, Any]:
    try:
        with repository._reader() as connection:
            row = connection.execute(sql, params).fetchone()
        value = int(row[0]) if row is not None and row[0] is not None else 0
        return {"n": value, "status": "ok"}
    except sqlite3.Error as exc:
        return {"n": None, "status": "error", "error": str(exc)}


def _safe_builder(name: str, fn: Callable[[], list[dict[str, Any]]], season: int) -> dict[str, Any]:
    try:
        rows = fn()
        season_rows = [r for r in rows if int(r.get("season", -1)) == int(season)]
        seasons = sorted({int(r["season"]) for r in rows if r.get("season") is not None})
        return {
            "stage": name,
            "status": "ok",
            "n": len(season_rows),
            "all_rows": len(rows),
            "available_seasons": seasons,
        }
    except Exception as exc:  # diagnostic report should continue through later stages
        return {
            "stage": name,
            "status": "error",
            "n": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _raw_table_stages(repository: CFBRepository, season: int) -> list[dict[str, Any]]:
    specs = (
        (
            "games_all",
            """SELECT COUNT(*) FROM games WHERE season=?""",
            (season,),
        ),
        (
            "games_completed",
            """SELECT COUNT(*) FROM games
               WHERE season=? AND home_points IS NOT NULL AND away_points IS NOT NULL""",
            (season,),
        ),
        (
            "game_lines_games",
            """SELECT COUNT(DISTINCT gl.game_id)
               FROM game_lines gl JOIN games g ON g.game_id=gl.game_id
               WHERE g.season=? AND gl.spread IS NOT NULL""",
            (season,),
        ),
        (
            "cfb_narrative_state_games",
            """SELECT COUNT(DISTINCT game_id)
               FROM cfb_narrative_state WHERE season=?""",
            (season,),
        ),
        (
            "cfb_projection_backtest_games",
            """SELECT COUNT(DISTINCT game_id)
               FROM cfb_projection_backtest WHERE season=?""",
            (season,),
        ),
        (
            "cfb_xpoints_dataset_games",
            """SELECT COUNT(DISTINCT game_id)
               FROM cfb_xpoints_dataset WHERE season=?""",
            (season,),
        ),
        (
            "cfb_coach_elo_games",
            """SELECT COUNT(DISTINCT game_id)
               FROM cfb_coach_elo_games WHERE season=?""",
            (season,),
        ),
        (
            "cfb_qb_elo_games",
            """SELECT COUNT(DISTINCT game_id)
               FROM cfb_qb_elo_games WHERE season=?""",
            (season,),
        ),
        (
            "game_player_box_stats_games",
            """SELECT COUNT(DISTINCT p.game_id)
               FROM game_player_box_stats p
               JOIN games g ON g.game_id=p.game_id
               WHERE g.season=?""",
            (season,),
        ),
    )
    output = []
    for stage, sql, params in specs:
        result = _safe_sql_count(repository, sql, params)
        output.append({"stage": stage, **result})
    return output


def _lens_field_coverage(rows: list[dict[str, Any]], season: int) -> dict[str, Any]:
    target = [r for r in rows if int(r.get("season", -1)) == int(season)]
    fields = (
        "margin_power_edge",
        "football_lab_edge",
        "elo_edge",
        "efficiency_power_edge",
        "line_elo_edge",
        "narrative_interaction_edge",
    )
    coverage = {
        field: sum(1 for r in target if r.get(field) is not None)
        for field in fields
    }
    structural_ready = sum(
        1
        for r in target
        if sum(
            r.get(field) is not None
            for field in ("football_lab_edge", "elo_edge", "efficiency_power_edge")
        ) >= 2
    )
    line_ready = sum(1 for r in target if r.get("line_elo_edge") is not None)
    primary_ready = sum(1 for r in target if r.get("margin_power_edge") is not None)
    fully_state_ready = sum(
        1
        for r in target
        if r.get("margin_power_edge") is not None
        and r.get("line_elo_edge") is not None
        and sum(
            r.get(field) is not None
            for field in ("football_lab_edge", "elo_edge", "efficiency_power_edge")
        ) >= 2
    )
    return {
        "n": len(target),
        "field_non_null_counts": coverage,
        "primary_margin_power_ready": primary_ready,
        "structural_cluster_ready_min_2_of_3": structural_ready,
        "line_elo_ready": line_ready,
        "state_ready_primary_plus_structural_plus_line": fully_state_ready,
    }


def _coach_season_attribution_count(repository: CFBRepository, season: int) -> dict[str, Any]:
    result = _safe_sql_count(
        repository,
        """SELECT COUNT(*) FROM coach_seasons WHERE season=? AND games > 0""",
        (int(season),),
    )
    if result.get("status") != "ok":
        return result
    try:
        with repository._reader() as connection:
            teams = connection.execute(
                """SELECT COUNT(DISTINCT team_id)
                   FROM coach_seasons WHERE season=? AND games > 0""",
                (int(season),),
            ).fetchone()[0]
        return {**result, "teams": int(teams or 0)}
    except sqlite3.Error as exc:
        return {"n": None, "status": "error", "error": str(exc)}


def _derived_stages(repository: CFBRepository, season: int, elo_start_season: int) -> list[dict[str, Any]]:
    hc_qb_rows: list[dict[str, Any]] | None = None

    def load_hc_qb() -> list[dict[str, Any]]:
        nonlocal hc_qb_rows
        if hc_qb_rows is None:
            hc_qb_rows = _load_dataset(
                repository,
                start_season=int(elo_start_season),
                end_season=int(season),
            )
        return hc_qb_rows

    lens_rows: list[dict[str, Any]] | None = None

    def load_lens_rows() -> list[dict[str, Any]]:
        nonlocal lens_rows
        if lens_rows is None:
            lens_rows = ipl.build_lens_rows(repository, test_season=int(season))
        return lens_rows

    stages = [
        _safe_builder("hc_qb_dataset_completed", load_hc_qb, season),
        _safe_builder("hc_qb_scored_both_ratings", lambda: _scored_rows(load_hc_qb()), season),
        _safe_builder(
            "internal_power_lens_rows",
            load_lens_rows,
            season,
        ),
        _safe_builder(
            "convergence_classified_structural_and_line_available",
            lambda: cr._classified_with_context(repository, test_season=int(season)),
            season,
        ),
    ]

    try:
        classified = cr._classified_with_context(repository, test_season=int(season))
        margin = [
            r for r in classified
            if int(r.get("season", -1)) == int(season)
            and float(r["abs_margin_power_z"]) >= four.FROZEN_MARGIN_THRESHOLD
        ]
        stages.append({
            "stage": "convergence_margin_power_ge_1_sigma",
            "status": "ok",
            "n": len(margin),
            "all_rows": len(classified),
        })
    except Exception as exc:
        stages.append({
            "stage": "convergence_margin_power_ge_1_sigma",
            "status": "error",
            "n": None,
            "error": f"{type(exc).__name__}: {exc}",
        })

    try:
        complete, coverage = four._joined_rows(
            repository,
            test_season=int(season),
            elo_start_season=int(elo_start_season),
        )
        target = [r for r in complete if int(r["season"]) == int(season)]
        stages.append({
            "stage": "four_signal_complete_rows",
            "status": "ok",
            "n": len(target),
            "all_rows": len(complete),
            "join_coverage": coverage,
        })
    except Exception as exc:
        stages.append({
            "stage": "four_signal_complete_rows",
            "status": "error",
            "n": None,
            "error": f"{type(exc).__name__}: {exc}",
        })
    try:
        stages.append({
            "stage": "internal_power_lens_field_coverage",
            "status": "ok",
            **_lens_field_coverage(load_lens_rows(), season),
        })
    except Exception as exc:
        stages.append({
            "stage": "internal_power_lens_field_coverage",
            "status": "error",
            "n": None,
            "error": f"{type(exc).__name__}: {exc}",
        })

    return stages


def _diagnosis(stages: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [s for s in stages if s.get("status") == "ok"]
    first_zero = next((s for s in ok if int(s.get("n") or 0) == 0), None)
    errors = [s for s in stages if s.get("status") == "error"]
    last_positive = None
    if first_zero is not None:
        zero_index = stages.index(first_zero)
        for candidate in reversed(stages[:zero_index]):
            if candidate.get("status") == "ok" and int(candidate.get("n") or 0) > 0:
                last_positive = candidate
                break
    return {
        "first_zero_stage": first_zero.get("stage") if first_zero else None,
        "last_positive_stage_before_zero": last_positive.get("stage") if last_positive else None,
        "errors": [
            {"stage": s["stage"], "error": s.get("error")}
            for s in errors
        ],
        "interpretation": (
            "The first zero stage is the earliest observed coverage break. "
            "If an earlier stage errors because a table does not exist, inspect that error before "
            "treating a later zero as causal."
        ),
    }


def report(
    repository: CFBRepository,
    *,
    season: int = DEFAULT_SEASON,
    elo_start_season: int = 2015,
) -> dict[str, Any]:
    season = int(season)
    raw = _raw_table_stages(repository, season)
    derived = _derived_stages(repository, season, int(elo_start_season))
    ordered = raw + derived
    return {
        "version": "cfb-research-coverage-audit-v1",
        "season": season,
        "elo_start_season": int(elo_start_season),
        "raw_and_persisted_stages": raw,
        "coach_season_attribution": _coach_season_attribution_count(repository, season),
        "derived_pipeline_stages": derived,
        "diagnosis": _diagnosis(ordered),
        "notes": [
            "Diagnostic only: no tables are written or backfilled.",
            "Counts are target-season game counts unless a stage explicitly reports all_rows as context.",
            "The HC/QB dataset requires completed coach-Elo games; scored rows additionally require both QB sides.",
            "Coach Elo can only assign target-season games when coach_seasons contains target-season team/coach attribution rows.",
            "Lens field coverage distinguishes missing Margin Power, Structural-cluster components, and Line Elo when lens rows exist but convergence classification is zero.",
            "Internal power lenses depend on narrative/composite inputs plus Margin Power, xPoints efficiency, and projection-backtest calibration.",
            "Four-signal complete rows additionally require a non-neutral combined HC/QB Elo score.",
        ],
    }
