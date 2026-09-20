"""Input-quality and key-number diagnostics for frozen Full Convergence.

This is descriptive research only. It does not alter the frozen Full
Convergence definition or optimize thresholds from outcomes.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import convergence_action_policy as cap
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.repository import CFBRepository

KEY_NUMBERS = (3.0, 7.0, 10.0, 14.0)

CORE_LENSES = (
    "margin_power_edge",
    "football_lab_edge",
    "elo_edge",
    "efficiency_power_edge",
    "line_elo_edge",
)


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _key_number_region(abs_spread: float | None) -> str:
    if abs_spread is None:
        return "unknown"
    value = float(abs_spread)
    if value < 2.75:
        return "below_3"
    if value <= 3.25:
        return "around_3"
    if value < 6.75:
        return "between_3_and_7"
    if value <= 7.25:
        return "around_7"
    if value < 14.0:
        return "7_to_13.5"
    return "14_plus"


def _half_point_spread(abs_spread: float | None) -> str:
    if abs_spread is None:
        return "unknown"
    rounded = round(float(abs_spread) * 2.0) / 2.0
    return f"{rounded:.1f}"


def _crossed_keys(start_margin: float | None, end_margin: float | None) -> list[str]:
    if start_margin is None or end_margin is None:
        return []
    start, end = float(start_margin), float(end_margin)
    low, high = sorted((start, end))
    crossed = []
    for key in KEY_NUMBERS:
        if low < key <= high or low <= -key < high:
            crossed.append(str(int(key)))
    return crossed


def _crossing_label(keys: list[str]) -> str:
    if not keys:
        return "no_key_crossed"
    if len(keys) == 1:
        return f"crosses_{keys[0]}"
    return "crosses_multiple_" + "_".join(keys)


def _market_open_margins(repository: CFBRepository) -> dict[int, float]:
    with repository._reader() as connection:
        columns = {
            str(r["name"]).casefold(): str(r["name"])
            for r in connection.execute("PRAGMA table_info(game_lines)")
        }
        open_column = columns.get("spread_open")
        if not open_column:
            return {}
        rows = connection.execute(
            f"""SELECT game_id, AVG({open_column}) AS spread_open
                FROM game_lines
                WHERE {open_column} IS NOT NULL
                GROUP BY game_id"""
        )
        return {
            int(r["game_id"]): -float(r["spread_open"])
            for r in rows if r["spread_open"] is not None
        }


def _edge_magnitude_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    mag = abs(float(value))
    if mag < 5.0:
        return "<5"
    if mag < 8.0:
        return "5-7.99"
    if mag < 12.0:
        return "8-11.99"
    return "12+"


def _applicability_points(row: dict[str, Any]) -> tuple[int, list[str]]:
    points = 0
    reasons: list[str] = []
    region = row.get("key_number_region")
    edge_bucket = row.get("margin_power_edge_bucket")
    crossing = row.get("margin_power_key_crossing")

    if region == "14_plus":
        points -= 2
        reasons.append("market_14_plus")
    elif region == "between_3_and_7":
        points -= 1
        reasons.append("market_between_3_and_7")
    elif region in {"below_3", "around_3", "around_7", "7_to_13.5"}:
        points += 1
        reasons.append("market_region_supported")

    if edge_bucket == "8-11.99":
        points += 1
        reasons.append("edge_plausible_large")
    elif edge_bucket == "12+":
        points -= 1
        reasons.append("edge_extreme")

    if crossing == "crosses_multiple_3_7":
        points += 1
        reasons.append("crosses_3_and_7")
    elif crossing in {"crosses_multiple_3_7_10_14", "crosses_multiple_7_10_14"}:
        points -= 1
        reasons.append("crosses_many_keys")

    if row.get("all_three_structural_available") is True:
        points += 1
        reasons.append("complete_structural_inputs")
    elif row.get("all_three_structural_available") is False:
        points -= 2
        reasons.append("incomplete_structural_inputs")

    return points, reasons


def _applicability_band(points: int) -> str:
    if points >= 3:
        return "high"
    if points >= 1:
        return "medium"
    return "low"


def _result_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    wins = sum(float(r["aligned_residual"]) > 0 for r in rows)
    losses = sum(float(r["aligned_residual"]) < 0 for r in rows)
    pushes = len(rows) - wins - losses
    decisions = wins + losses
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_ex_pushes": round(wins / decisions, 4) if decisions else None,
        "mean_aligned_residual": round(
            sum(float(r["aligned_residual"]) for r in rows) / len(rows), 3
        ),
    }


def _group_summary(rows: list[dict[str, Any]], key) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(key(row))].append(row)
    return [
        {"value": value, **_result_summary(groups[value])}
        for value in sorted(groups)
    ]


def report(
    repository: CFBRepository,
    *,
    from_season: int = 2021,
    to_season: int = 2025,
) -> dict[str, Any]:
    raw = ipl.build_lens_rows(repository, test_season=int(to_season))
    raw = [
        r for r in raw
        if int(from_season) <= int(r["season"]) <= int(to_season)
    ]

    raw_by_game = {int(r["game_id"]): r for r in raw}
    opening_margins = _market_open_margins(repository)

    full = cap._full_rows(repository, test_season=int(to_season))
    full = [
        r for r in full
        if int(from_season) <= int(r["season"]) <= int(to_season)
    ]
    for row in full:
        spread = row.get("market_home_margin")
        row["abs_market_spread"] = abs(float(spread)) if spread is not None else None
        row["key_number_region"] = _key_number_region(row["abs_market_spread"])
        row["rounded_half_point_spread"] = _half_point_spread(row["abs_market_spread"])
        actual_margin = row.get("actual_home_margin")
        row["abs_actual_margin"] = (
            abs(float(actual_margin)) if actual_margin is not None else None
        )
        row["actual_margin_landed_key"] = (
            "3" if row["abs_actual_margin"] == 3.0
            else "7" if row["abs_actual_margin"] == 7.0
            else "other"
        )

        lens = raw_by_game.get(int(row["game_id"]), {})
        market_margin = (
            float(row["market_home_margin"])
            if row.get("market_home_margin") is not None else None
        )
        margin_power_edge = lens.get("margin_power_edge")
        football_lab_edge = lens.get("football_lab_edge")
        row["margin_power_edge_raw"] = (
            float(margin_power_edge) if margin_power_edge is not None else None
        )
        row["margin_power_edge_bucket"] = _edge_magnitude_bucket(
            row["margin_power_edge_raw"]
        )
        row["margin_power_implied_margin"] = (
            market_margin + float(margin_power_edge)
            if market_margin is not None and margin_power_edge is not None else None
        )
        row["football_lab_implied_margin"] = (
            market_margin + float(football_lab_edge)
            if market_margin is not None and football_lab_edge is not None else None
        )
        mp_keys = _crossed_keys(
            market_margin, row["margin_power_implied_margin"]
        )
        fl_keys = _crossed_keys(
            market_margin, row["football_lab_implied_margin"]
        )
        row["margin_power_crossed_keys"] = mp_keys
        row["margin_power_key_crossing"] = _crossing_label(mp_keys)
        row["football_lab_crossed_keys"] = fl_keys
        row["football_lab_key_crossing"] = _crossing_label(fl_keys)

        opening_margin = opening_margins.get(int(row["game_id"]))
        row["opening_market_home_margin"] = opening_margin
        market_keys = _crossed_keys(opening_margin, market_margin)
        row["market_open_to_close_crossed_keys"] = market_keys
        row["market_open_to_close_key_crossing"] = (
            _crossing_label(market_keys) if opening_margin is not None
            else "opening_unavailable"
        )
        row["all_three_structural_available"] = all(
            lens.get(key) is not None
            for key in ("football_lab_edge", "elo_edge", "efficiency_power_edge")
        )
        points, reasons = _applicability_points(row)
        row["applicability_points"] = points
        row["applicability_band"] = _applicability_band(points)
        row["applicability_reasons"] = reasons

        row["market_move_toward_margin_power"] = (
            abs(market_margin - float(row["margin_power_implied_margin"]))
            < abs(opening_margin - float(row["margin_power_implied_margin"]))
            if opening_margin is not None
            and market_margin is not None
            and row["margin_power_implied_margin"] is not None
            else None
        )

    season_quality = []
    for season in range(int(from_season), int(to_season) + 1):
        season_rows = [r for r in raw if int(r["season"]) == season]
        full_rows = [r for r in full if int(r["season"]) == season]
        lens_coverage = {}
        for lens in CORE_LENSES:
            n = sum(r.get(lens) is not None for r in season_rows)
            lens_coverage[lens] = {
                "rows": n,
                "coverage_rate": _rate(n, len(season_rows)),
            }
        all_core = sum(
            all(r.get(lens) is not None for lens in CORE_LENSES)
            for r in season_rows
        )
        all_structural = sum(
            all(r.get(lens) is not None for lens in (
                "football_lab_edge", "elo_edge", "efficiency_power_edge"
            ))
            for r in season_rows
        )
        full_all_structural = sum(
            all(r.get(lens) is not None for lens in (
                "football_lab_edge", "elo_edge", "efficiency_power_edge"
            ))
            for r in full_rows
        )
        season_quality.append({
            "season": season,
            "lens_rows": len(season_rows),
            "lens_coverage": lens_coverage,
            "all_core_lenses_rows": all_core,
            "all_core_lenses_rate": _rate(all_core, len(season_rows)),
            "all_three_structural_rows": all_structural,
            "all_three_structural_rate": _rate(all_structural, len(season_rows)),
            "full_convergence": {
                **_result_summary(full_rows),
                "all_three_structural_rows": full_all_structural,
                "all_three_structural_rate": _rate(
                    full_all_structural, len(full_rows)
                ),
            },
        })

    between = [r for r in full if r["key_number_region"] == "between_3_and_7"]

    return {
        "version": "convergence-quality-key-number-audit-v1",
        "window": [int(from_season), int(to_season)],
        "frozen_definition_unchanged": {
            "minimum_abs_margin_power_z": cap.FROZEN_THRESHOLD,
            "required_confirmations": 2,
        },
        "season_input_quality": season_quality,
        "full_convergence_overall": _result_summary(full),
        "full_convergence_by_key_number_region": _group_summary(
            full, lambda r: r["key_number_region"]
        ),
        "key_crossing_analysis": {
            "margin_power_market_to_model": _group_summary(
                full, lambda r: r["margin_power_key_crossing"]
            ),
            "football_lab_market_to_model": _group_summary(
                full, lambda r: r["football_lab_key_crossing"]
            ),
            "market_open_to_close": _group_summary(
                full, lambda r: r["market_open_to_close_key_crossing"]
            ),
            "margin_power_crossing_by_market_confirmation": _group_summary(
                full,
                lambda r: (
                    f'{r["margin_power_key_crossing"]}|'
                    f'market_{"toward" if r["market_move_toward_margin_power"] else "away"}'
                    if r["market_move_toward_margin_power"] is not None
                    else f'{r["margin_power_key_crossing"]}|market_unknown'
                ),
            ),
            "margin_power_crossing_by_season": _group_summary(
                full,
                lambda r: f'{r["season"]}|{r["margin_power_key_crossing"]}',
            ),
            "market_region_x_margin_power_crossing": _group_summary(
                full,
                lambda r: (
                    f'{r["key_number_region"]}|'
                    f'{r["margin_power_key_crossing"]}'
                ),
            ),
            "market_region_x_edge_magnitude": _group_summary(
                full,
                lambda r: (
                    f'{r["key_number_region"]}|'
                    f'{r["margin_power_edge_bucket"]}'
                ),
            ),
            "market_region_x_crossing_x_edge_magnitude": _group_summary(
                full,
                lambda r: (
                    f'{r["key_number_region"]}|'
                    f'{r["margin_power_key_crossing"]}|'
                    f'{r["margin_power_edge_bucket"]}'
                ),
            ),
        },
        "walk_forward_applicability": {
            "score_definition": {
                "market_region_supported": 1,
                "market_between_3_and_7": -1,
                "market_14_plus": -2,
                "edge_8_to_11_99": 1,
                "edge_12_plus": -1,
                "crosses_3_and_7_only": 1,
                "crosses_many_keys": -1,
                "complete_structural_inputs": 1,
                "incomplete_structural_inputs": -2,
                "bands": {"high": "3+", "medium": "1-2", "low": "0_or_less"},
            },
            "overall_by_band": _group_summary(
                full, lambda r: r["applicability_band"]
            ),
            "by_season_and_band": _group_summary(
                full, lambda r: f'{r["season"]}|{r["applicability_band"]}'
            ),
            "post_2021_by_band": _group_summary(
                [r for r in full if int(r["season"]) >= 2022],
                lambda r: r["applicability_band"],
            ),
        },
        "between_3_and_7": {
            "overall": _result_summary(between),
            "by_season": _group_summary(between, lambda r: r["season"]),
            "by_market_role": _group_summary(
                between, lambda r: r.get("market_role", "unknown")
            ),
            "by_selected_side": _group_summary(
                between, lambda r: r.get("selected_side", "unknown")
            ),
            "by_half_point_band": _group_summary(
                between,
                lambda r: (
                    "3.5-4.5" if float(r["abs_market_spread"]) < 4.75
                    else "4.75-5.5" if float(r["abs_market_spread"]) < 5.75
                    else "5.75-6.5"
                ),
            ),
            "by_rounded_half_point_spread": _group_summary(
                between, lambda r: r["rounded_half_point_spread"]
            ),
            "by_actual_margin_landed_key": _group_summary(
                between, lambda r: r["actual_margin_landed_key"]
            ),
        },
        "notes": [
            "Key-number regions use a +/-0.25 window around 3 and 7 because provider consensus spreads can average across books.",
            "The between-3-and-7 diagnostic is descriptive and does not create a new betting rule.",
            "Consensus spreads are also rounded to the nearest half point for micro-bucket inspection; raw consensus values remain unchanged for classification.",
            "Actual final margins landing exactly on 3 or 7 are reported to test whether key-number outcomes disproportionately drive the ATS result.",
            "Input-quality rates are computed on the same historical lens rows used by the convergence research.",
            "Primary key crossing uses Margin Power implied margin because Margin Power is the frozen primary convergence signal.",
            "Football Lab key crossing is retained as a secondary diagnostic, not a replacement primary signal.",
            "Opening-to-closing spread key crossings are reported only when game_lines exposes spread_open.",
            "Raw Margin Power edge magnitude is bucketed independently of key crossings to separate scoring-regime effects from larger-disagreement effects.",
            "Applicability points are fixed from predeclared football-structural observations and are not tuned on target-season outcomes.",
            "Applicability is descriptive context layered on top of Full Convergence; it does not redefine Full Convergence.",
        ],
    }
