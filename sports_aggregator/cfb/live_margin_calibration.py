"""Production CFB margin calibration.

Frozen after walk-forward/common-sample validation:
  raw Football Lab margin
  + projected PPD differential
  + projected drive differential
  + Elo differential
  + CORE margin
  + FPI margin
  + recent margin differential

Vegas is intentionally excluded from model fitting and prediction.
If a live external rating is unavailable, fall back through nested
walk-forward-tested feature sets rather than imputing market information.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.xpoints import DATASET_VERSION as XPOINTS_VERSION

MODEL_VERSION = "margin-v2"
L2 = 2.0

FEATURE_SETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("plus_recent", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff",
        "core_margin", "fpi_margin", "recent_margin_diff",
    )),
    ("plus_fpi", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff",
        "core_margin", "fpi_margin",
    )),
    ("plus_core", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
    )),
    ("plus_elo", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff",
    )),
    ("base", ("raw_margin", "ppd_diff", "drive_diff")),
)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    aug = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for row in range(col + 1, n):
            factor = aug[row][col] / aug[col][col]
            for idx in range(col, n + 1):
                aug[row][idx] -= factor * aug[col][idx]
    out = [0.0] * n
    for row in range(n - 1, -1, -1):
        out[row] = (
            aug[row][n]
            - sum(aug[row][idx] * out[idx] for idx in range(row + 1, n))
        ) / aug[row][row]
    return out


def _fit(rows: list[dict[str, Any]], features: tuple[str, ...]) -> dict[str, Any] | None:
    eligible = [
        row for row in rows
        if row.get("actual_margin") is not None
        and all(row.get(key) is not None for key in features)
    ]
    if len(eligible) < 100:
        return None
    means = {key: sum(float(r[key]) for r in eligible) / len(eligible) for key in features}
    scales = {}
    for key in features:
        scale = math.sqrt(
            sum((float(r[key]) - means[key]) ** 2 for r in eligible) / len(eligible)
        )
        scales[key] = scale or 1.0

    size = len(features) + 1
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size
    for row in eligible:
        x = [1.0] + [
            (float(row[key]) - means[key]) / scales[key] for key in features
        ]
        y = float(row["actual_margin"])
        for i in range(size):
            xty[i] += x[i] * y
            for j in range(size):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, size):
        xtx[i][i] += L2
    beta = _solve(xtx, xty)
    if beta is None:
        return None
    return {
        "features": features,
        "means": means,
        "scales": scales,
        "beta": beta,
        "training_rows": len(eligible),
    }


def _historical_rows(repository, *, target_season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT p.game_id,p.season,p.side,p.team,
                      p.projected_offensive_points,p.projected_points_per_drive,
                      p.projected_drives,p.actual_score_points,
                      x.elo_difference,x.core_margin,x.fpi_margin,
                      x.team_recent_margin,x.opponent_recent_margin
               FROM cfb_projection_backtest p
               LEFT JOIN cfb_xpoints_dataset x
                 ON x.game_id=p.game_id AND x.team=p.team AND x.dataset_version=?
               WHERE p.backtest_version=? AND p.season<?
                 AND p.projected_offensive_points IS NOT NULL
                 AND p.actual_score_points IS NOT NULL
               ORDER BY p.season,p.game_id,p.side""",
            (XPOINTS_VERSION, BACKTEST_VERSION, int(target_season)),
        )]

    grouped: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        grouped[int(row["game_id"])][str(row["side"])] = row

    out = []
    for gid, sides in grouped.items():
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        hp = float(home["projected_offensive_points"])
        ap = float(away["projected_offensive_points"])
        ha = float(home["actual_score_points"])
        aa = float(away["actual_score_points"])
        out.append({
            "game_id": gid,
            "season": int(home["season"]),
            "raw_margin": hp - ap,
            "ppd_diff": (
                float(home["projected_points_per_drive"])
                - float(away["projected_points_per_drive"])
                if home.get("projected_points_per_drive") is not None
                and away.get("projected_points_per_drive") is not None
                else None
            ),
            "drive_diff": (
                float(home["projected_drives"]) - float(away["projected_drives"])
                if home.get("projected_drives") is not None
                and away.get("projected_drives") is not None
                else None
            ),
            "elo_diff": (
                float(home["elo_difference"])
                if home.get("elo_difference") is not None else None
            ),
            "core_margin": (
                float(home["core_margin"])
                if home.get("core_margin") is not None else None
            ),
            "fpi_margin": (
                float(home["fpi_margin"])
                if home.get("fpi_margin") is not None else None
            ),
            "recent_margin_diff": (
                float(home["team_recent_margin"]) - float(home["opponent_recent_margin"])
                if home.get("team_recent_margin") is not None
                and home.get("opponent_recent_margin") is not None
                else None
            ),
            "actual_margin": ha - aa,
        })
    return out


def live_features(projection: dict[str, Any]) -> dict[str, float | None]:
    home = projection.get("home") or {}
    away = projection.get("away") or {}
    quality = (projection.get("opponent_quality") or {}).get("home") or {}
    components = quality.get("components") or {}
    home_points = projection.get("home_points_snapshot") or {}
    away_points = projection.get("away_points_snapshot") or {}

    hp = home.get("expected_points")
    ap = away.get("expected_points")
    hppd = home.get("points_per_drive")
    appd = away.get("points_per_drive")
    hd = home.get("drives")
    ad = away.get("drives")
    elo_points = components.get("elo")

    return {
        "raw_margin": float(hp) - float(ap) if hp is not None and ap is not None else None,
        "ppd_diff": float(hppd) - float(appd) if hppd is not None and appd is not None else None,
        "drive_diff": float(hd) - float(ad) if hd is not None and ad is not None else None,
        # matchup_quality_snapshot converts Elo to point-like scale by /25.
        # Historical margin-v2 was fit on raw Elo difference.
        "elo_diff": float(elo_points) * 25.0 if elo_points is not None else None,
        "core_margin": (
            float(components["core"]) if components.get("core") is not None else None
        ),
        "fpi_margin": (
            float(components["fpi"]) if components.get("fpi") is not None else None
        ),
        "recent_margin_diff": (
            float(home_points["recent_margin"]) - float(away_points["recent_margin"])
            if home_points.get("recent_margin") is not None
            and away_points.get("recent_margin") is not None
            else None
        ),
    }


def predict_live(repository, *, target_season: int,
                 projection: dict[str, Any]) -> dict[str, Any]:
    features = live_features(projection)
    history = _historical_rows(repository, target_season=int(target_season))

    for label, feature_names in FEATURE_SETS:
        if any(features.get(key) is None for key in feature_names):
            continue
        model = _fit(history, feature_names)
        if model is None:
            continue
        value = float(model["beta"][0])
        for idx, key in enumerate(feature_names, 1):
            value += (
                float(model["beta"][idx])
                * (float(features[key]) - float(model["means"][key]))
                / float(model["scales"][key])
            )
        return {
            "value": value,
            "model_version": MODEL_VERSION,
            "variant": label,
            "features": {key: features[key] for key in feature_names},
            "missing_features": [
                key for key in FEATURE_SETS[0][1] if features.get(key) is None
            ],
            "training_games": int(model["training_rows"]),
            "l2": L2,
            "market_used": False,
        }

    raw = features.get("raw_margin")
    return {
        "value": raw,
        "model_version": MODEL_VERSION,
        "variant": "raw_fallback",
        "features": features,
        "missing_features": [
            key for key in FEATURE_SETS[0][1] if features.get(key) is None
        ],
        "training_games": 0,
        "l2": L2,
        "market_used": False,
    }
