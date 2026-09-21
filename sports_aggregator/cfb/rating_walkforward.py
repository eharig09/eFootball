"""Leak-safe walk-forward normalization for combined HC/QB Elo.

Pregame HC and QB Elo snapshots are already chronological. This module only
addresses the *normalization* layer used to combine their differently-scaled
rating differences.

For a target season, means and standard deviations are fit using prior seasons
only. No target-season rows contribute to their own normalization.
"""
from __future__ import annotations

import math
from typing import Any


def _mean_std(values: list[float]) -> tuple[float, float] | None:
    if not values:
        return None
    mean = sum(values) / len(values)
    stdev = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    return mean, (stdev or 1.0)


def walk_forward_combined_scores(
    rows: list[dict[str, Any]],
    *,
    minimum_training_rows: int = 30,
) -> tuple[dict[int, float], dict[str, Any]]:
    """Return game_id -> prior-seasons-only HC/QB combined z-average.

    Only rows with both HC and QB differences participate. Each target season
    is standardized against all eligible rows from earlier seasons.
    """
    both = [row for row in rows if row.get("qb_diff") is not None]
    seasons = sorted({int(row["season"]) for row in both})
    scores: dict[int, float] = {}
    season_diagnostics: dict[str, Any] = {}

    for season in seasons:
        train = [row for row in both if int(row["season"]) < season]
        target = [row for row in both if int(row["season"]) == season]
        hc_stats = _mean_std([float(row["hc_diff"]) for row in train])
        qb_stats = _mean_std([float(row["qb_diff"]) for row in train])
        ready = (
            len(train) >= int(minimum_training_rows)
            and hc_stats is not None
            and qb_stats is not None
        )
        season_diagnostics[str(season)] = {
            "training_rows": len(train),
            "target_rows": len(target),
            "minimum_training_rows": int(minimum_training_rows),
            "ready": ready,
            "hc_mean": round(hc_stats[0], 6) if hc_stats else None,
            "hc_std": round(hc_stats[1], 6) if hc_stats else None,
            "qb_mean": round(qb_stats[0], 6) if qb_stats else None,
            "qb_std": round(qb_stats[1], 6) if qb_stats else None,
        }
        if not ready:
            continue

        hc_mean, hc_std = hc_stats
        qb_mean, qb_std = qb_stats
        for row in target:
            hc_z = (float(row["hc_diff"]) - hc_mean) / hc_std
            qb_z = (float(row["qb_diff"]) - qb_mean) / qb_std
            scores[int(row["game_id"])] = (hc_z + qb_z) / 2.0

    return scores, {
        "method": "prior_seasons_only_zscore_average",
        "minimum_training_rows": int(minimum_training_rows),
        "eligible_rows_with_both_ratings": len(both),
        "scored_rows": len(scores),
        "by_target_season": season_diagnostics,
    }
