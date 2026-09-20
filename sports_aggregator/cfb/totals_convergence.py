"""Walk-forward convergence study for Football Lab game totals.

Frozen design for this first totals-convergence pass:
- Primary signal: calibrated Football Lab scoreboard-total edge vs closing total.
- Structural confirmation: a cluster of two decomposed production components:
  (a) pace-only total and (b) scoring-efficiency-only total.
- Independent confirmation: leak-safe trailing team game-total tendency.
- All normalizations use prior seasons only.
- No threshold is selected from outcomes; fixed primary z thresholds are
  0.5, 1.0 and 1.5.

The production-component lenses are calibrated to final scoreboard total using
prior seasons only before they are compared with the market.
"""
from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict, deque
from typing import Any

from sports_aggregator.cfb import market_ats_totals as mat
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.repository import CFBRepository

THRESHOLDS = (0.5, 1.0, 1.5)
TENDENCY_WINDOW = 8
MIN_TENDENCY_GAMES = 3


def _std(values) -> float:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 1.0
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) or 1.0


def _projection_rows(repository: CFBRepository) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                """SELECT game_id,team,side,season,week,kickoff,
                          projected_drives,projected_points_per_drive,
                          projected_offensive_points,actual_score_points
                   FROM cfb_projection_backtest
                   WHERE backtest_version=?
                   ORDER BY kickoff,game_id,side""",
                (BACKTEST_VERSION,),
            )
        ]
    grouped: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        grouped[int(row["game_id"])][str(row["side"])] = row

    out = []
    for gid, sides in grouped.items():
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        required = (
            home.get("projected_drives"), away.get("projected_drives"),
            home.get("projected_points_per_drive"),
            away.get("projected_points_per_drive"),
            home.get("projected_offensive_points"),
            away.get("projected_offensive_points"),
            home.get("actual_score_points"), away.get("actual_score_points"),
        )
        if any(v is None for v in required):
            continue
        out.append({
            "game_id": gid,
            "season": int(home["season"]),
            "week": int(home["week"]) if home.get("week") is not None else None,
            "kickoff": home["kickoff"],
            "home_team": home["team"],
            "away_team": away["team"],
            "home_projected_drives": float(home["projected_drives"]),
            "away_projected_drives": float(away["projected_drives"]),
            "home_projected_ppd": float(home["projected_points_per_drive"]),
            "away_projected_ppd": float(away["projected_points_per_drive"]),
            "projected_offensive_total": (
                float(home["projected_offensive_points"])
                + float(away["projected_offensive_points"])
            ),
            "actual_score_total": (
                float(home["actual_score_points"])
                + float(away["actual_score_points"])
            ),
        })
    return out


def _historical_tendency(rows: list[dict[str, Any]]) -> dict[int, float]:
    """Blend each team's trailing realized game totals, strictly before kickoff."""
    histories: dict[str, deque[float]] = defaultdict(
        lambda: deque(maxlen=TENDENCY_WINDOW))
    out: dict[int, float] = {}
    for row in sorted(rows, key=lambda r: (str(r["kickoff"]), int(r["game_id"]))):
        home = str(row["home_team"])
        away = str(row["away_team"])
        hh, ah = histories[home], histories[away]
        if len(hh) >= MIN_TENDENCY_GAMES and len(ah) >= MIN_TENDENCY_GAMES:
            hmean = sum(hh) / len(hh)
            amean = sum(ah) / len(ah)
            out[int(row["game_id"])] = (hmean + amean) / 2.0
        actual = float(row["actual_score_total"])
        hh.append(actual)
        ah.append(actual)
    return out


def _component_raw(rows: list[dict[str, Any]], target_season: int) -> dict[int, dict[str, float]]:
    """Construct raw pace-only and efficiency-only total estimators.

    Pace-only:
      projected combined drives * prior-season mean points per team-drive.

    Efficiency-only:
      mean projected matchup PPD * prior-season mean combined drives per game.

    Both use only seasons before target_season for their environment constant.
    """
    train = [r for r in rows if int(r["season"]) < int(target_season)]
    if not train:
        return {}

    total_drives = sum(
        float(r["home_projected_drives"]) + float(r["away_projected_drives"])
        for r in train
    )
    total_actual_points = sum(float(r["actual_score_total"]) for r in train)
    points_per_projected_drive = (
        total_actual_points / total_drives if total_drives else None
    )

    mean_combined_drives = sum(
        float(r["home_projected_drives"]) + float(r["away_projected_drives"])
        for r in train
    ) / len(train)

    out = {}
    for row in rows:
        if int(row["season"]) != int(target_season):
            continue
        combined_drives = (
            float(row["home_projected_drives"])
            + float(row["away_projected_drives"])
        )
        mean_ppd = (
            float(row["home_projected_ppd"])
            + float(row["away_projected_ppd"])
        ) / 2.0
        if points_per_projected_drive is None:
            continue
        out[int(row["game_id"])] = {
            "pace_raw_total": combined_drives * points_per_projected_drive,
            "efficiency_raw_total": mean_ppd * mean_combined_drives,
        }
    return out


def _calibrated_lookup(
    rows: list[dict[str, Any]],
    raw_by_season: dict[int, dict[int, dict[str, float]]],
    target_season: int,
    key: str,
) -> dict[int, float]:
    """Fit raw component -> scoreboard total using prior seasons only."""
    pairs: list[tuple[float, float]] = []
    for season, lookup in raw_by_season.items():
        if int(season) >= int(target_season):
            continue
        actual = {
            int(r["game_id"]): float(r["actual_score_total"])
            for r in rows if int(r["season"]) == int(season)
        }
        for gid, values in lookup.items():
            if gid in actual and values.get(key) is not None:
                pairs.append((float(values[key]), actual[gid]))
    fit = mat._linear_fit(pairs)
    if not fit:
        return {}

    current = raw_by_season.get(int(target_season), {})
    return {
        gid: (
            float(fit["intercept"])
            + float(fit["slope"]) * float(values[key])
        )
        for gid, values in current.items()
        if values.get(key) is not None
    }


def _base_rows(repository: CFBRepository, *, test_season: int) -> list[dict[str, Any]]:
    projections = _projection_rows(repository)
    totals_base = {
        int(r["game_id"]): r
        for r in mat._totals_rows(repository, test_season=int(test_season))
    }
    tendency = _historical_tendency(projections)
    seasons = sorted({
        int(r["season"]) for r in projections
        if int(r["season"]) <= int(test_season)
    })

    raw_by_season = {
        season: _component_raw(projections, season)
        for season in seasons
    }

    pace_cal: dict[int, float] = {}
    eff_cal: dict[int, float] = {}
    for season in seasons:
        pace_cal.update(_calibrated_lookup(
            projections, raw_by_season, season, "pace_raw_total"))
        eff_cal.update(_calibrated_lookup(
            projections, raw_by_season, season, "efficiency_raw_total"))

    out = []
    for row in projections:
        season = int(row["season"])
        if season > int(test_season):
            continue
        gid = int(row["game_id"])
        base = totals_base.get(gid)
        if not base:
            continue
        market = float(base["market_total"])
        out.append({
            **base,
            "football_lab_total_edge": float(base["total_edge"]),
            "pace_total_edge": (
                float(pace_cal[gid]) - market if gid in pace_cal else None
            ),
            "efficiency_total_edge": (
                float(eff_cal[gid]) - market if gid in eff_cal else None
            ),
            "tendency_total_edge": (
                float(tendency[gid]) - market if gid in tendency else None
            ),
            "pace_projected_total": pace_cal.get(gid),
            "efficiency_projected_total": eff_cal.get(gid),
            "tendency_projected_total": tendency.get(gid),
        })
    return out


def _scales(train: list[dict[str, Any]]) -> dict[str, float]:
    keys = (
        "football_lab_total_edge",
        "pace_total_edge",
        "efficiency_total_edge",
        "tendency_total_edge",
    )
    return {
        key: _std(r.get(key) for r in train if r.get(key) is not None)
        for key in keys
        if sum(1 for r in train if r.get(key) is not None) >= 50
    }


def _state(row: dict[str, Any], scales: dict[str, float]) -> dict[str, Any] | None:
    if "football_lab_total_edge" not in scales:
        return None
    primary_edge = row.get("football_lab_total_edge")
    if primary_edge is None:
        return None
    primary_z = float(primary_edge) / float(scales["football_lab_total_edge"])
    direction = 1 if primary_z > 0 else -1 if primary_z < 0 else 0
    if not direction:
        return None

    component_zs = []
    for key in ("pace_total_edge", "efficiency_total_edge"):
        if key in scales and row.get(key) is not None:
            component_zs.append(float(row[key]) / float(scales[key]))
    structural_z = (
        sum(component_zs) / len(component_zs) if len(component_zs) == 2 else None
    )

    tendency_z = None
    if "tendency_total_edge" in scales and row.get("tendency_total_edge") is not None:
        tendency_z = (
            float(row["tendency_total_edge"])
            / float(scales["tendency_total_edge"])
        )

    structural_agrees = (
        structural_z is not None and structural_z * direction > 0
    )
    tendency_agrees = tendency_z is not None and tendency_z * direction > 0
    available_confirmations = int(structural_z is not None) + int(tendency_z is not None)
    confirmation_count = int(structural_agrees) + int(tendency_agrees)

    return {
        "primary_z": primary_z,
        "primary_direction": direction,
        "structural_z": structural_z,
        "structural_z_aligned": (
            structural_z * direction if structural_z is not None else None
        ),
        "tendency_z": tendency_z,
        "tendency_z_aligned": (
            tendency_z * direction if tendency_z is not None else None
        ),
        "available_confirmations": available_confirmations,
        "confirmation_count": confirmation_count,
        "confirmation_combination": (
            "structural+tendency" if structural_agrees and tendency_agrees
            else "structural_only" if structural_agrees
            else "tendency_only" if tendency_agrees
            else "none"
        ),
    }


def _classified(repository: CFBRepository, *, test_season: int) -> list[dict[str, Any]]:
    rows = _base_rows(repository, test_season=int(test_season))
    seasons = sorted({int(r["season"]) for r in rows})
    out = []
    for season in seasons:
        train = [r for r in rows if int(r["season"]) < season]
        test = [r for r in rows if int(r["season"]) == season]
        scales = _scales(train)
        for row in test:
            state = _state(row, scales)
            if state is None:
                continue
            cooked = dict(row)
            cooked.update(state)
            cooked["abs_primary_z"] = abs(float(state["primary_z"]))
            cooked["aligned_total_residual"] = (
                float(row["actual_total_residual"])
                * int(state["primary_direction"])
            )
            aligned = float(cooked["aligned_total_residual"])
            cooked["result"] = (
                "win" if aligned > 0 else "loss" if aligned < 0 else "push"
            )
            out.append(cooked)
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return mat._bet_summary(rows, residual_key="aligned_total_residual")


def _bucket_report(rows: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    eligible = [
        r for r in rows
        if float(r["abs_primary_z"]) >= float(threshold)
        and int(r["available_confirmations"]) == 2
    ]
    groups = defaultdict(list)
    for row in eligible:
        groups[int(row["confirmation_count"])].append(row)

    by_count = []
    for count in (0, 1, 2):
        subset = groups.get(count, [])
        by_count.append({
            "confirmation_count": count,
            **_summary(subset),
        })

    combos = defaultdict(list)
    for row in eligible:
        combos[str(row["confirmation_combination"])].append(row)
    by_combo = [
        {"combination": combo, **_summary(subset)}
        for combo, subset in sorted(combos.items())
    ]

    return {
        "minimum_abs_primary_z": threshold,
        "eligible_n": len(eligible),
        "by_confirmation_count": by_count,
        "by_confirmation_combination": by_combo,
    }


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    rows = _classified(repository, test_season=int(test_season))
    pooled = [_bucket_report(rows, threshold) for threshold in THRESHOLDS]

    yearly = []
    for season in sorted({int(r["season"]) for r in rows}):
        season_rows = [r for r in rows if int(r["season"]) == season]
        yearly.append({
            "season": season,
            "thresholds": [
                _bucket_report(season_rows, threshold)
                for threshold in THRESHOLDS
            ],
        })

    single_lens = {}
    for key in (
        "football_lab_total_edge",
        "pace_total_edge",
        "efficiency_total_edge",
        "tendency_total_edge",
    ):
        subset = []
        for row in rows:
            edge = row.get(key)
            if edge is None:
                continue
            direction = 1 if float(edge) > 0 else -1 if float(edge) < 0 else 0
            if not direction:
                continue
            cooked = dict(row)
            aligned = float(row["actual_total_residual"]) * direction
            cooked["aligned_total_residual"] = aligned
            cooked["result"] = (
                "win" if aligned > 0 else "loss" if aligned < 0 else "push"
            )
            subset.append(cooked)
        single_lens[key] = _summary(subset)

    return {
        "version": "totals-convergence-v1",
        "test_through_season": int(test_season),
        "primary": "calibrated Football Lab total edge vs closing total",
        "confirmations": {
            "structural": (
                "equal-weight z mean of pace-only and efficiency-only "
                "walk-forward calibrated total edges"
            ),
            "tendency": (
                "blend of each team's trailing realized game totals, "
                "strictly before kickoff"
            ),
        },
        "fixed_primary_z_thresholds": list(THRESHOLDS),
        "single_lens_performance": single_lens,
        "pooled_thresholds": pooled,
        "yearly_thresholds": yearly,
        "game_rows": rows,
        "notes": [
            "No threshold is selected from the outcomes in this report.",
            "Pace and efficiency are treated as one structural family so they do not receive two independent votes.",
            "All component calibrations use prior seasons only.",
            "Historical tendency requires at least three prior games for both teams and uses at most eight.",
            "Season-level explosiveness was intentionally excluded because the available table is not a point-in-time pregame snapshot.",
        ],
    }


def export_report(payload: dict[str, Any], output_dir: str) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "totals_convergence.json")
    summary_path = os.path.join(output_dir, "totals_convergence_summary.csv")
    games_path = os.path.join(output_dir, "totals_convergence_games.csv")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    flat = []
    for scope, year, blocks in [
        ("pooled", "", payload["pooled_thresholds"]),
        *[
            ("year", item["season"], item["thresholds"])
            for item in payload["yearly_thresholds"]
        ],
    ]:
        for block in blocks:
            threshold = block["minimum_abs_primary_z"]
            for row in block["by_confirmation_count"]:
                flat.append({
                    "scope": scope,
                    "season": year,
                    "threshold": threshold,
                    "confirmation_count": row["confirmation_count"],
                    **row,
                })

    fields = [
        "scope", "season", "threshold", "confirmation_count",
        "n", "wins", "losses", "pushes", "decisions",
        "win_rate_ex_pushes", "roi_minus_110", "roi_minus_105",
        "roi_even_money", "mean_aligned_residual", "median_aligned_residual",
    ]
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)

    games = payload["game_rows"]
    if games:
        with open(games_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(games[0].keys()))
            writer.writeheader()
            writer.writerows(games)
    else:
        with open(games_path, "w", encoding="utf-8") as handle:
            handle.write("")

    return {
        "json": json_path,
        "summary_csv": summary_path,
        "games_csv": games_path,
    }


def compact_console_summary(payload: dict[str, Any],
                            paths: dict[str, str]) -> dict[str, Any]:
    by_threshold = {}
    for block in payload["pooled_thresholds"]:
        full = next(
            (r for r in block["by_confirmation_count"]
             if int(r["confirmation_count"]) == 2),
            {"n": 0},
        )
        by_threshold[str(block["minimum_abs_primary_z"])] = full
    return {
        "version": payload["version"],
        "single_lens": payload["single_lens_performance"],
        "full_convergence_by_primary_z": by_threshold,
        "files": paths,
    }
