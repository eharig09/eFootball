"""Common-sample walk-forward comparison for CFB margin feature sets.

Every candidate margin model is trained/evaluated on rows where the fullest
feature set is available, so differences cannot be attributed to coverage.
Vegas is used only for compression diagnostics.
"""
from __future__ import annotations

from typing import Any

from sports_aggregator.cfb.margin_feature_ablation import (
    FEATURE_SETS, _compression, _fit_ridge, _linear_total_fit, _load,
    _metrics, _predict,
)
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION


FULL_FEATURES = FEATURE_SETS["plus_yards"]


def _common(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r for r in rows
        if r.get("actual_margin") is not None
        and r.get("raw_total") is not None
        and r.get("actual_total") is not None
        and all(r.get(k) is not None for k in FULL_FEATURES)
    ]


def report(repository, *, from_season: int = 2023, to_season: int = 2025,
           training_from_season: int = 2021,
           backtest_version: str = BACKTEST_VERSION) -> dict[str, Any]:
    games = _load(
        repository,
        min(int(training_from_season), int(from_season)),
        int(to_season),
        backtest_version,
    )
    folds = []
    pooled: list[dict[str, Any]] = []

    for season in range(int(from_season), int(to_season) + 1):
        raw_train = [
            r for r in games
            if int(training_from_season) <= int(r["season"]) < season
        ]
        raw_test = [dict(r) for r in games if int(r["season"]) == season]
        train = _common(raw_train)
        test = _common(raw_test)
        total_fit = _linear_total_fit(train)
        if total_fit is None or not test:
            continue

        models = {
            name: _fit_ridge(train, features)
            for name, features in FEATURE_SETS.items()
        }

        for row in test:
            row["cal_total"] = (
                float(total_fit["intercept"])
                + float(total_fit["slope"]) * float(row["raw_total"])
            )
            for name, model in models.items():
                row[f"margin_{name}"] = _predict(model, row)

        pooled.extend(test)
        folds.append({
            "season": season,
            "raw_train_games": len(raw_train),
            "common_train_games": len(train),
            "raw_test_games": len(raw_test),
            "common_test_games": len(test),
            "coverage_rate": round(len(test) / len(raw_test), 4) if raw_test else 0.0,
            "models": {
                name: {
                    "features": list(features),
                    "training_rows": models[name]["n"] if models[name] else 0,
                    "metrics": _metrics(test, f"margin_{name}"),
                    "compression": _compression(test, f"margin_{name}"),
                }
                for name, features in FEATURE_SETS.items()
            },
        })

    return {
        "version": "cfb-margin-common-sample-v1",
        "backtest_version": backtest_version,
        "from_season": int(from_season),
        "to_season": int(to_season),
        "training_from_season": int(training_from_season),
        "common_sample_features": list(FULL_FEATURES),
        "feature_sets": {k: list(v) for k, v in FEATURE_SETS.items()},
        "walk_forward": folds,
        "pooled": {
            name: {
                "metrics": _metrics(pooled, f"margin_{name}"),
                "compression": _compression(pooled, f"margin_{name}"),
            }
            for name in FEATURE_SETS
        },
        "pooled_common_games": len(pooled),
    }
