"""Audit/repair composite inputs and evaluate independent signal families.

This module does not change production projections. It:
1) audits historical FPI/CORE source coverage,
2) optionally rebuilds xPoints + narrative state when those sources exist,
3) calibrates Football Lab offense-margin to scoreboard margin walk-forward,
4) measures signal correlations,
5) evaluates family-level agreement/magnitude.
"""
from __future__ import annotations

from collections import defaultdict
import math
from statistics import median
from typing import Any

from sports_aggregator.cfb import narrative_shapes
from sports_aggregator.cfb import narrative_shapes_v2 as v2
from sports_aggregator.cfb import narrative_composite as nc
from sports_aggregator.cfb import xpoints
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.repository import CFBRepository

FAMILY_KEYS = (
    "football_lab_family",
    "elo_family",
    "external_power_family",
    "market_family",
    "narrative_family",
)

MAGNITUDE_BUCKETS = (
    (0.0, 0.50, "<0.50"),
    (0.50, 0.75, "0.50-0.75"),
    (0.75, 1.00, "0.75-1.00"),
    (1.00, 1.25, "1.00-1.25"),
    (1.25, 1.50, "1.25-1.50"),
    (1.50, float("inf"), ">=1.50"),
)


def _mean(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _std(values) -> float:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 1.0
    avg = sum(vals) / len(vals)
    return math.sqrt(sum((v - avg) ** 2 for v in vals) / len(vals)) or 1.0


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x, _ in pairs))
    dy = math.sqrt(sum((y - my) ** 2 for _, y in pairs))
    return num / (dx * dy) if dx and dy else None


def source_audit(repository: CFBRepository, *, from_season: int = 2022,
                 to_season: int = 2025) -> dict[str, Any]:
    xpoints.initialize(repository)
    with repository._reader() as connection:
        tables = {str(r[0]) for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        seasons = list(range(int(from_season), int(to_season) + 1))
        fpi = {}
        core = {}
        xp = {}
        for season in seasons:
            if "fpi_game_projections" in tables:
                row = connection.execute(
                    """SELECT COUNT(*) AS rows,COUNT(DISTINCT f.game_id) AS games
                       FROM fpi_game_projections f
                       JOIN games g USING(game_id)
                       WHERE g.season=?""", (season,)).fetchone()
                fpi[str(season)] = {"rows": int(row["rows"]), "games": int(row["games"])}
            else:
                fpi[str(season)] = {"rows": 0, "games": 0}
            if "core_ratings" in tables:
                row = connection.execute(
                    """SELECT COUNT(*) AS rows,COUNT(DISTINCT team) AS teams,
                              COUNT(DISTINCT through_week) AS weeks
                       FROM core_ratings WHERE season=?""", (season,)).fetchone()
                core[str(season)] = {
                    "rows": int(row["rows"]), "teams": int(row["teams"]),
                    "weeks": int(row["weeks"]),
                }
            else:
                core[str(season)] = {"rows": 0, "teams": 0, "weeks": 0}
            row = connection.execute(
                """SELECT COUNT(*) AS rows,
                          SUM(CASE WHEN fpi_margin IS NOT NULL THEN 1 ELSE 0 END) AS fpi_rows,
                          SUM(CASE WHEN core_margin IS NOT NULL THEN 1 ELSE 0 END) AS core_rows
                   FROM cfb_xpoints_dataset
                   WHERE dataset_version=? AND season=?""",
                (xpoints.DATASET_VERSION, season)).fetchone()
            total = int(row["rows"] or 0)
            xp[str(season)] = {
                "rows": total,
                "fpi_rows": int(row["fpi_rows"] or 0),
                "core_rows": int(row["core_rows"] or 0),
                "fpi_coverage": round(int(row["fpi_rows"] or 0) / total, 4) if total else 0.0,
                "core_coverage": round(int(row["core_rows"] or 0) / total, 4) if total else 0.0,
            }
    return {
        "from_season": int(from_season),
        "to_season": int(to_season),
        "tables_present": {
            "fpi_game_projections": "fpi_game_projections" in tables,
            "core_ratings": "core_ratings" in tables,
        },
        "fpi_source_by_season": fpi,
        "core_source_by_season": core,
        "xpoints_coverage_by_season": xp,
        "repair_ready": (
            "fpi_game_projections" in tables and "core_ratings" in tables
            and any(v["rows"] > 0 for v in fpi.values())
            and any(v["rows"] > 0 for v in core.values())
        ),
    }


def rebuild_from_sources(repository: CFBRepository, *, from_season: int = 2022,
                         to_season: int = 2025) -> dict[str, Any]:
    before = source_audit(
        repository, from_season=from_season, to_season=to_season)
    if not before["repair_ready"]:
        return {
            "status": "not_rebuilt",
            "reason": (
                "FPI and/or CORE source tables are absent or empty. "
                "Populate source tables before rebuilding derived datasets."
            ),
            "before": before,
        }
    xp = xpoints.build_dataset(
        repository, from_season=from_season, to_season=to_season)
    narrative = narrative_shapes.build(
        repository, from_season=from_season, to_season=to_season)
    after = source_audit(
        repository, from_season=from_season, to_season=to_season)
    return {
        "status": "rebuilt",
        "xpoints": xp,
        "narrative": narrative,
        "before": before,
        "after": after,
    }


def _projection_games(repository: CFBRepository) -> dict[int, dict[str, Any]]:
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT p.game_id,p.team,p.side,p.season,
                      p.projected_offensive_points,p.actual_score_points
               FROM cfb_projection_backtest p
               WHERE p.backtest_version=?
               ORDER BY p.game_id,p.side""",
            (BACKTEST_VERSION,),
        )]
    grouped: dict[int, dict[str, Any]] = defaultdict(dict)
    for row in rows:
        grouped[int(row["game_id"])][str(row["side"])] = row
    return grouped


def _fit_scoreboard_calibration(projection_games: dict[int, dict[str, Any]],
                                seasons: set[int]) -> dict[str, float] | None:
    pairs = []
    for game in projection_games.values():
        home, away = game.get("home"), game.get("away")
        if not home or not away or int(home["season"]) not in seasons:
            continue
        values = (
            home.get("projected_offensive_points"),
            away.get("projected_offensive_points"),
            home.get("actual_score_points"),
            away.get("actual_score_points"),
        )
        if any(v is None for v in values):
            continue
        x = float(home["projected_offensive_points"]) - float(away["projected_offensive_points"])
        y = float(home["actual_score_points"]) - float(away["actual_score_points"])
        pairs.append((x, y))
    if len(pairs) < 20:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    denom = sum((x - mx) ** 2 for x, _ in pairs)
    slope = sum((x - mx) * (y - my) for x, y in pairs) / denom if denom else 0.0
    intercept = my - slope * mx
    return {"intercept": intercept, "slope": slope, "n": len(pairs)}


def _family_games(repository: CFBRepository, *, test_season: int,
                  line_rate: float) -> list[dict[str, Any]]:
    base = nc._composite_games(
        repository, test_season=test_season, line_rate=line_rate)
    narrative_rows = v2._load_rows(repository)
    narrative_lookup = {
        int(r["game_id"]): r for r in narrative_rows if r["side"] == "home"
    }
    projections = _projection_games(repository)

    seasons = sorted({int(r["season"]) for r in base})
    calibration_by_season = {}
    for season in seasons:
        calibration_by_season[season] = _fit_scoreboard_calibration(
            projections, {s for s in seasons if s < season})

    out = []
    for row in base:
        gid, season = int(row["game_id"]), int(row["season"])
        nrow = narrative_lookup.get(gid)
        if not nrow:
            continue
        market = nrow.get("market_expected_margin")
        if market is None:
            continue
        market = float(market)

        fl_edge = None
        game = projections.get(gid, {})
        home, away = game.get("home"), game.get("away")
        calibration = calibration_by_season.get(season)
        if home and away and calibration and home.get("projected_offensive_points") is not None and away.get("projected_offensive_points") is not None:
            offense_margin = (
                float(home["projected_offensive_points"])
                - float(away["projected_offensive_points"]))
            calibrated_margin = (
                float(calibration["intercept"])
                + float(calibration["slope"]) * offense_margin)
            fl_edge = calibrated_margin - market

        external_components = [
            row.get("fpi_edge"), row.get("core_edge")
        ]
        external_family = _mean(external_components)

        out.append({
            **row,
            "football_lab_family": fl_edge,
            "elo_family": row.get("elo_edge"),
            "external_power_family": external_family,
            "market_family": row.get("line_elo_edge"),
            "narrative_family": row.get("narrative_interaction_edge"),
            "scoreboard_calibration_n": (
                int(calibration["n"]) if calibration else 0),
        })
    return out


def _correlation_matrix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matrix = {}
    for a in FAMILY_KEYS:
        matrix[a] = {}
        for b in FAMILY_KEYS:
            pairs = [
                (float(r[a]), float(r[b]))
                for r in rows if r.get(a) is not None and r.get(b) is not None
            ]
            corr = _pearson(pairs)
            matrix[a][b] = {
                "n": len(pairs),
                "correlation": round(corr, 4) if corr is not None else None,
            }
    return matrix


def _score(row: dict[str, Any], scales: dict[str, float]) -> dict[str, Any]:
    vals = []
    for key in FAMILY_KEYS:
        value = row.get(key)
        if value is None:
            continue
        vals.append(float(value) / float(scales.get(key) or 1.0))
    if not vals:
        return {"available": 0, "score": None, "direction": 0, "agreement": None}
    score = sum(vals) / len(vals)
    direction = 1 if score > 0 else -1 if score < 0 else 0
    agree = sum(1 for z in vals if direction and ((z > 0) == (direction > 0)))
    return {
        "available": len(vals),
        "score": score,
        "direction": direction,
        "agreement": agree / len(vals) if direction else 0.0,
    }


def _bucket(rows: list[dict[str, Any]], scales: dict[str, float],
            low: float, high: float) -> dict[str, Any]:
    vals = []
    for row in rows:
        s = _score(row, scales)
        if s["score"] is None or s["available"] < 3:
            continue
        mag = abs(float(s["score"]))
        if not (low <= mag < high):
            continue
        aligned = float(row["market_margin_residual"]) * int(s["direction"])
        vals.append((aligned, s))
    if not vals:
        return {"n": 0}
    aligned = [v for v, _ in vals]
    return {
        "n": len(vals),
        "directional_hit_rate": round(sum(v > 0 for v in aligned) / len(aligned), 4),
        "mean_aligned_residual": round(sum(aligned) / len(aligned), 3),
        "median_aligned_residual": round(float(median(aligned)), 3),
        "mean_agreement": round(sum(float(s["agreement"]) for _, s in vals) / len(vals), 4),
        "mean_available_families": round(sum(int(s["available"]) for _, s in vals) / len(vals), 3),
    }


def family_report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    narrative_rows = v2._load_rows(repository)
    line_rate, _ = v2._choose_line_rate(
        narrative_rows, validation_season=int(test_season) - 1)
    rows = _family_games(
        repository, test_season=test_season, line_rate=line_rate)
    seasons = sorted({int(r["season"]) for r in rows})

    walk = []
    for season in [s for s in seasons if s > min(seasons) and s <= int(test_season)]:
        train = [r for r in rows if int(r["season"]) < season]
        test = [r for r in rows if int(r["season"]) == season]
        scales = {
            key: _std(r.get(key) for r in train if r.get(key) is not None)
            for key in FAMILY_KEYS
        }
        coverage = {
            key: {
                "rows": sum(1 for r in train if r.get(key) is not None),
                "coverage_rate": round(
                    sum(1 for r in train if r.get(key) is not None) / len(train), 4)
                if train else 0.0,
                "std": round(scales[key], 4),
            }
            for key in FAMILY_KEYS
        }
        buckets = []
        for low, high, label in MAGNITUDE_BUCKETS:
            buckets.append({
                "bucket": label,
                "metrics": _bucket(test, scales, low, high),
            })
        walk.append({
            "season": season,
            "train_rows": len(train),
            "test_rows": len(test),
            "coverage": coverage,
            "correlations": _correlation_matrix(train),
            "magnitude_buckets": buckets,
        })

    return {
        "version": "family-composite-v1",
        "test_season": int(test_season),
        "families": {
            "football_lab_family": "walk-forward calibrated final-score margin from production offensive-margin",
            "elo_family": "results-based Elo edge versus market",
            "external_power_family": "mean of available FPI and CORE edges",
            "market_family": "pre-line Line Elo edge versus closing market",
            "narrative_family": "prior-history narrative interaction edge",
        },
        "source_audit": source_audit(repository, from_season=min(seasons), to_season=int(test_season)),
        "walk_forward_years": walk,
        "notes": [
            "Family composite requires at least three populated families for a game.",
            "FPI/CORE stay absent if their source tables are not populated; no fallback value is invented.",
            "Football Lab family is scoreboard-aligned by prior-season calibration only.",
            "Correlation matrices are calculated on each season's training history before evaluating that season.",
        ],
    }
