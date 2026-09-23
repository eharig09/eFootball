"""Diagnostics for independently modeled EPA.

Provider PPA is used only as an external directional benchmark. It is never an
input to EP/EPA fitting or scoring.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import math
from typing import Any


def _acc() -> dict[str, float]:
    return {"n": 0.0, "sum_epa": 0.0, "sum_abs": 0.0, "sum_sq": 0.0,
            "positive": 0.0, "negative": 0.0}


def _add(acc: dict[str, float], value: float) -> None:
    acc["n"] += 1
    acc["sum_epa"] += value
    acc["sum_abs"] += abs(value)
    acc["sum_sq"] += value * value
    acc["positive"] += int(value > 0)
    acc["negative"] += int(value < 0)


def _finish(acc: dict[str, float]) -> dict[str, Any]:
    n = int(acc["n"])
    if not n:
        return {"plays": 0, "mean_epa": None, "mean_abs_epa": None,
                "rms_epa": None, "positive_share": None, "negative_share": None}
    return {
        "plays": n,
        "mean_epa": round(acc["sum_epa"] / n, 5),
        "mean_abs_epa": round(acc["sum_abs"] / n, 5),
        "rms_epa": round(math.sqrt(acc["sum_sq"] / n), 5),
        "positive_share": round(acc["positive"] / n, 4),
        "negative_share": round(acc["negative"] / n, 4),
    }


def validate_epa(repository, *, from_season: int | None = None,
                 to_season: int | None = None, model_version: str = "ep-v1") -> dict[str, Any]:
    """Summarize EPA behavior and compare directionally with stored provider PPA."""
    clauses = ["e.model_version=?", "e.epa IS NOT NULL", "p.period BETWEEN 1 AND 4"]
    params: list[Any] = [model_version]
    if from_season is not None:
        clauses.append("p.season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("p.season<=?"); params.append(int(to_season))

    sql = f"""
      SELECT p.season,m.rush_pass,p.provider_ppa,e.epa,e.possession_changed,
             e.immediate_net_points
      FROM cfb_play_epa e
      JOIN cfb_plays p USING(play_id)
      LEFT JOIN cfb_play_metrics m ON m.play_id=p.play_id AND m.metric_version='pbp-v1'
      WHERE {' AND '.join(clauses)}
    """

    overall = _acc()
    rush_pass: dict[str, dict[str, float]] = defaultdict(_acc)
    by_season: dict[int, dict[str, float]] = defaultdict(_acc)
    changed = _acc()
    retained = _acc()
    scoring = _acc()
    non_scoring = _acc()

    pair_n = 0
    sum_x = sum_y = sum_x2 = sum_y2 = sum_xy = 0.0

    with closing(repository._connect()) as connection:
        for row in connection.execute(sql, params):
            epa = float(row["epa"])
            _add(overall, epa)
            _add(by_season[int(row["season"])], epa)
            label = str(row["rush_pass"] or "other")
            _add(rush_pass[label], epa)
            if int(row["possession_changed"] or 0):
                _add(changed, epa)
            else:
                _add(retained, epa)
            if abs(float(row["immediate_net_points"] or 0.0)) > 1e-9:
                _add(scoring, epa)
            else:
                _add(non_scoring, epa)

            if row["provider_ppa"] is not None:
                x = epa
                y = float(row["provider_ppa"])
                pair_n += 1
                sum_x += x; sum_y += y
                sum_x2 += x * x; sum_y2 += y * y; sum_xy += x * y

    correlation = None
    if pair_n > 1:
        numerator = pair_n * sum_xy - sum_x * sum_y
        denom_x = pair_n * sum_x2 - sum_x * sum_x
        denom_y = pair_n * sum_y2 - sum_y * sum_y
        if denom_x > 0 and denom_y > 0:
            correlation = numerator / math.sqrt(denom_x * denom_y)

    return {
        "model_version": model_version,
        "from_season": from_season,
        "to_season": to_season,
        "overall": _finish(overall),
        "by_rush_pass": {k: _finish(v) for k, v in sorted(rush_pass.items())},
        "possession_change": _finish(changed),
        "possession_retained": _finish(retained),
        "scoring_plays": _finish(scoring),
        "non_scoring_plays": _finish(non_scoring),
        "by_season": {str(k): _finish(v) for k, v in sorted(by_season.items())},
        "provider_ppa_benchmark": {
            "paired_plays": pair_n,
            "pearson_correlation": round(correlation, 5) if correlation is not None else None,
            "note": "Provider PPA is an external benchmark only and is not used to fit or score EP/EPA.",
        },
        "evaluation_note": (
            f"EPA is offense-perspective and regulation-only in {model_version}. "
            "Possession-change EPA should usually "
            "be negative on average; scoring-play EPA should usually be positive on average."
        ),
    }
