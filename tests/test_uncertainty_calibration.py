"""Tests for the NFL Football Lab uncertainty-scale model's week feature."""
from __future__ import annotations

import random

from sports_aggregator.nfl import uncertainty_calibration as uc


def _synthetic_rows(n_per_week: int = 60) -> list[dict[str, float]]:
    """Residuals with genuinely larger variance in early weeks (1-3) than late (9-18).

    Other scale features are held at a fixed, uninformative value so the fitted
    scale must be explained by `week` and not accidentally by something else.
    """
    rng = random.Random(0)
    rows = []
    for week in range(1, 19):
        spread = 12.0 if week <= 3 else 4.0
        for _ in range(n_per_week):
            residual = rng.uniform(-spread, spread)
            rows.append({
                "week": week,
                "total_residual": residual,
                "margin_residual": residual,
                "cal_total": 44.0,
                "abs_cal_margin": 4.0,
                "sum_pred_drives": 22.0,
                "sum_pred_total_plays": 130.0,
                "abs_sum_pred_combined_epa": 5.0,
                "abs_diff_pred_drives": 1.0,
                "abs_diff_pred_pass_epa": 0.1,
                "abs_diff_pred_rush_epa": 0.1,
                "abs_diff_pred_combined_epa": 1.0,
            })
    return rows


def test_scale_features_include_week():
    assert "week" in uc.TOTAL_SCALE_FEATURES
    assert "week" in uc.MARGIN_SCALE_FEATURES


def test_scale_model_widens_early_weeks_when_residuals_are_noisier():
    rows = _synthetic_rows()
    total_model = uc._fit_scale(rows, uc.TOTAL_SCALE_FEATURES, "total_residual")
    margin_model = uc._fit_scale(rows, uc.MARGIN_SCALE_FEATURES, "margin_residual")
    assert total_model is not None
    assert margin_model is not None

    early_row = next(r for r in rows if r["week"] == 2)
    late_row = next(r for r in rows if r["week"] == 15)

    early_total_scale = uc._scale_predict(total_model, early_row)
    late_total_scale = uc._scale_predict(total_model, late_row)
    assert early_total_scale > late_total_scale

    early_margin_scale = uc._scale_predict(margin_model, early_row)
    late_margin_scale = uc._scale_predict(margin_model, late_row)
    assert early_margin_scale > late_margin_scale


def test_week_regime_summary_buckets_by_early_mid_late_season():
    rows = [{"week": w} for w in (1, 3, 4, 8, 9, 18)]
    for r in rows:
        r["cal_total"] = 44.0
        r["actual_total"] = 44.0
        r["total_global_hw_0_9"] = 10.0
        r["total_conditional_hw_0_9"] = 10.0
    buckets = uc._week_regime_summary(rows, "total", "global", 0.9)
    assert buckets["week<=3"]["n"] == 2
    assert buckets["week4-8"]["n"] == 2
    assert buckets["week9+"]["n"] == 2
