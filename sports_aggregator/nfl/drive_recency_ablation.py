"""NFL xDrives recency-window experiment.

Tests whether coefficient/environment drift is better handled by fitting only
recent prior seasons instead of an ever-expanding historical training set.
This does not yet alter the underlying team-history snapshots; it isolates
model-fit recency first.
"""
from __future__ import annotations

from typing import Any
import numpy as np

from sports_aggregator.nfl.drive_projection import build_rows, RIDGE_ALPHA, _summary
from sports_aggregator.nfl.drive_feature_ablation import FOOTBALL_FEATURES
from sports_aggregator.nfl.repository import NFLRepository


WINDOWS = (2, 3, 5)


def _fit(train: list[dict[str, Any]]):
    if len(train) < 100:
        return None
    x = np.asarray([[r[k] for k in FOOTBALL_FEATURES] for r in train], dtype=float)
    y = np.asarray([r["actual_drives"] for r in train], dtype=float)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales == 0] = 1.0
    z = (x - means) / scales
    design = np.column_stack([np.ones(len(z)), z])
    penalty = np.eye(design.shape[1]) * RIDGE_ALPHA
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    return {"means": means, "scales": scales, "beta": beta}


def _predict(model, row):
    x = np.asarray([row[k] for k in FOOTBALL_FEATURES], dtype=float)
    z = (x - model["means"]) / model["scales"]
    return float(model["beta"][0] + z @ model["beta"][1:])


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    rows = build_rows(repository, start_season=start_season, end_season=end_season)
    seasons = sorted({int(r["season"]) for r in rows})
    pooled = []
    folds = []

    for season in seasons:
        test = [dict(r) for r in rows if int(r["season"]) == season]
        expanding = [r for r in rows if int(r["season"]) < season]
        if len(expanding) < 100 or not test:
            continue
        models = {"expanding": _fit(expanding)}
        train_sizes = {"expanding": len(expanding)}
        for window in WINDOWS:
            train = [
                r for r in rows
                if season - window <= int(r["season"]) < season
            ]
            models[f"rolling_{window}y"] = _fit(train)
            train_sizes[f"rolling_{window}y"] = len(train)

        for r in test:
            for label, model in models.items():
                r[f"pred_{label}"] = _predict(model, r) if model else None
        pooled.extend(test)
        folds.append({
            "season": season,
            "train_rows": train_sizes,
            **{
                label: _summary(test, f"pred_{label}")
                for label in models
            },
        })

    labels = ("expanding",) + tuple(f"rolling_{w}y" for w in WINDOWS)
    return {
        "version": "nfl-drive-recency-ablation-v1",
        "features": list(FOOTBALL_FEATURES),
        "note": "Tests training-fit recency only; underlying team snapshots remain cumulative.",
        "walk_forward": folds,
        "pooled": {
            label: _summary(pooled, f"pred_{label}")
            for label in labels
        },
    }
