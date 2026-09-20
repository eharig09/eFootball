"""Walk-forward comparison of CFB score-construction/calibration methods.

Tests whether total calibration should preserve raw margin, scale scores
proportionally, allocate the total residual by projected efficiency, or pair
an independently calibrated total with an independently calibrated margin.

All calibration fits use only seasons before the test season.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION


def _linear_fit(pairs: list[tuple[float, float]]) -> dict[str, float] | None:
    if len(pairs) < 50:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 1e-12:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pairs) / var
    intercept = my - slope * mx
    return {"intercept": intercept, "slope": slope, "n": len(pairs)}


def _fit_margin(train: list[dict[str, Any]]) -> dict[str, Any] | None:
    features = ("raw_margin", "ppd_diff", "drive_diff")
    rows = [r for r in train if all(r.get(k) is not None for k in features)
            and r.get("actual_margin") is not None]
    if len(rows) < 100:
        return None
    means = {k: sum(float(r[k]) for r in rows) / len(rows) for k in features}
    scales = {}
    for k in features:
        s = math.sqrt(sum((float(r[k]) - means[k]) ** 2 for r in rows) / len(rows))
        scales[k] = s or 1.0
    size = len(features) + 1
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size
    l2 = 2.0
    for r in rows:
        x = [1.0] + [(float(r[k]) - means[k]) / scales[k] for k in features]
        y = float(r["actual_margin"])
        for i in range(size):
            xty[i] += x[i] * y
            for j in range(size):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, size):
        xtx[i][i] += l2

    # Gaussian elimination
    aug = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    n = size
    for col in range(n):
        pivot = max(range(col, n), key=lambda rr: abs(aug[rr][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for rr in range(col + 1, n):
            factor = aug[rr][col] / aug[col][col]
            for cc in range(col, n + 1):
                aug[rr][cc] -= factor * aug[col][cc]
    beta = [0.0] * n
    for rr in range(n - 1, -1, -1):
        beta[rr] = (aug[rr][n] - sum(aug[rr][cc] * beta[cc] for cc in range(rr + 1, n))) / aug[rr][rr]
    return {"features": features, "means": means, "scales": scales, "beta": beta, "n": len(rows)}


def _margin_predict(model: dict[str, Any], row: dict[str, Any]) -> float | None:
    if model is None:
        return None
    x = [1.0] + [
        (float(row[k]) - model["means"][k]) / model["scales"][k]
        for k in model["features"]
    ]
    return sum(a * b for a, b in zip(model["beta"], x))


def _bucket(value: float) -> str:
    if value < 3: return "<3"
    if value < 7: return "3-6.5"
    if value < 14: return "7-13.5"
    return "14+"


def _load_games(repository, from_season: int, to_season: int,
                backtest_version: str) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT p.game_id,p.season,p.side,p.projected_offensive_points,
                      p.projected_points_per_drive,p.projected_drives,
                      p.actual_score_points,
                      g.week,g.home_team,g.away_team,
                      (SELECT AVG(gl.spread) FROM game_lines gl
                       WHERE gl.game_id=p.game_id AND gl.spread IS NOT NULL) AS market_spread
               FROM cfb_projection_backtest p
               JOIN games g USING(game_id)
               WHERE p.backtest_version=? AND p.season BETWEEN ? AND ?
                 AND p.projected_offensive_points IS NOT NULL
                 AND p.actual_score_points IS NOT NULL
               ORDER BY p.season,g.week,p.game_id,p.side""",
            (backtest_version, int(from_season), int(to_season)),
        )]
    grouped: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        grouped[int(row["game_id"])][str(row["side"])] = row

    games = []
    for gid, sides in grouped.items():
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        raw_home = float(home["projected_offensive_points"])
        raw_away = float(away["projected_offensive_points"])
        actual_home = float(home["actual_score_points"])
        actual_away = float(away["actual_score_points"])
        games.append({
            "game_id": gid,
            "season": int(home["season"]),
            "week": int(home["week"] or 0),
            "home_team": home["home_team"],
            "away_team": home["away_team"],
            "raw_home": raw_home,
            "raw_away": raw_away,
            "raw_total": raw_home + raw_away,
            "raw_margin": raw_home - raw_away,
            "home_ppd": float(home["projected_points_per_drive"]) if home["projected_points_per_drive"] is not None else None,
            "away_ppd": float(away["projected_points_per_drive"]) if away["projected_points_per_drive"] is not None else None,
            "ppd_diff": (
                float(home["projected_points_per_drive"]) - float(away["projected_points_per_drive"])
                if home["projected_points_per_drive"] is not None and away["projected_points_per_drive"] is not None else None
            ),
            "drive_diff": (
                float(home["projected_drives"]) - float(away["projected_drives"])
                if home["projected_drives"] is not None and away["projected_drives"] is not None else None
            ),
            "actual_home": actual_home,
            "actual_away": actual_away,
            "actual_total": actual_home + actual_away,
            "actual_margin": actual_home - actual_away,
            "market_spread": float(home["market_spread"]) if home["market_spread"] is not None else None,
        })
    return games


def _construct(method: str, row: dict[str, Any], total_fit: dict[str, float],
               margin_model: dict[str, Any] | None) -> tuple[float, float]:
    calibrated_total = total_fit["intercept"] + total_fit["slope"] * row["raw_total"]
    delta = calibrated_total - row["raw_total"]

    if method == "equal":
        return row["raw_home"] + delta / 2, row["raw_away"] + delta / 2

    if method == "proportional":
        if row["raw_total"] <= 0:
            return row["raw_home"] + delta / 2, row["raw_away"] + delta / 2
        scale = calibrated_total / row["raw_total"]
        return row["raw_home"] * scale, row["raw_away"] * scale

    if method == "ppd_weighted":
        hp = max(0.0, float(row["home_ppd"] or 0.0))
        ap = max(0.0, float(row["away_ppd"] or 0.0))
        denom = hp + ap
        if denom <= 0:
            return row["raw_home"] + delta / 2, row["raw_away"] + delta / 2
        return row["raw_home"] + delta * (hp / denom), row["raw_away"] + delta * (ap / denom)

    if method == "dual":
        margin = _margin_predict(margin_model, row)
        if margin is None:
            margin = row["raw_margin"]
        return (calibrated_total + margin) / 2, (calibrated_total - margin) / 2

    raise ValueError(method)


def _metrics(rows: list[dict[str, Any]], method: str) -> dict[str, Any]:
    selected = [r for r in rows if r.get(f"{method}_home") is not None]
    if not selected:
        return {"n": 0}
    score_err = []
    total_err = []
    margin_err = []
    for r in selected:
        ph, pa = r[f"{method}_home"], r[f"{method}_away"]
        score_err.extend([abs(ph - r["actual_home"]), abs(pa - r["actual_away"])])
        total_err.append(abs((ph + pa) - r["actual_total"]))
        margin_err.append(abs((ph - pa) - r["actual_margin"]))
    return {
        "n": len(selected),
        "score_mae": round(sum(score_err) / len(score_err), 4),
        "total_mae": round(sum(total_err) / len(total_err), 4),
        "margin_mae": round(sum(margin_err) / len(margin_err), 4),
    }


def _compression(rows: list[dict[str, Any]], method: str) -> dict[str, Any]:
    usable = [r for r in rows if r.get("market_spread") not in (None, 0)
              and r.get(f"{method}_home") is not None]
    if not usable:
        return {"n": 0}
    records = []
    for r in usable:
        market_home_margin = -float(r["market_spread"])
        market_abs = abs(market_home_margin)
        favorite_home = market_home_margin > 0
        model_home_margin = r[f"{method}_home"] - r[f"{method}_away"]
        model_fav = model_home_margin if favorite_home else -model_home_margin
        change = model_fav - market_abs
        relation = (
            "favorite_flipped" if model_fav < 0
            else "favorite_more_favored" if change > 0.05
            else "underdog_closer" if change < -0.05
            else "same_margin"
        )
        records.append((market_abs, model_fav, change, relation))

    def summarize(vals):
        if not vals: return {"n": 0}
        n = len(vals)
        return {
            "n": n,
            "favorite_more_favored_rate": round(sum(v[3]=="favorite_more_favored" for v in vals)/n, 4),
            "underdog_closer_rate": round(sum(v[3]=="underdog_closer" for v in vals)/n, 4),
            "favorite_flipped_rate": round(sum(v[3]=="favorite_flipped" for v in vals)/n, 4),
            "mean_favorite_margin_change": round(sum(v[2] for v in vals)/n, 3),
            "mean_market_abs_spread": round(sum(v[0] for v in vals)/n, 3),
            "mean_model_favorite_margin": round(sum(v[1] for v in vals)/n, 3),
            "compression_ratio": round(
                (sum(v[1] for v in vals)/n) / (sum(v[0] for v in vals)/n), 4
            ) if sum(v[0] for v in vals) else None,
        }

    return {
        "overall": summarize(records),
        "by_bucket": {
            bucket: summarize([v for v in records if _bucket(v[0]) == bucket])
            for bucket in ("<3","3-6.5","7-13.5","14+")
        },
    }


def report(repository, *, from_season: int = 2021, to_season: int = 2025,
           training_from_season: int = 2021,
           backtest_version: str = BACKTEST_VERSION) -> dict[str, Any]:
    games = _load_games(repository, min(training_from_season, from_season), to_season, backtest_version)
    methods = ("equal", "proportional", "ppd_weighted", "dual")
    evaluated = []
    folds = []

    for season in range(from_season, to_season + 1):
        train = [r for r in games if training_from_season <= r["season"] < season]
        test = [dict(r) for r in games if r["season"] == season]
        total_fit = _linear_fit([(r["raw_total"], r["actual_total"]) for r in train])
        margin_model = _fit_margin(train)
        if total_fit is None or not test:
            continue
        for r in test:
            for method in methods:
                h, a = _construct(method, r, total_fit, margin_model)
                r[f"{method}_home"] = h
                r[f"{method}_away"] = a
        evaluated.extend(test)
        folds.append({
            "season": season,
            "train_games": len(train),
            "test_games": len(test),
            "total_fit": {k: round(v, 5) if isinstance(v, float) else v for k,v in total_fit.items()},
            "margin_model_rows": margin_model["n"] if margin_model else 0,
            "methods": {m: _metrics(test, m) for m in methods},
            "favorite_compression": {m: _compression(test, m) for m in methods},
        })

    return {
        "version": "cfb-score-construction-calibration-v1",
        "backtest_version": backtest_version,
        "from_season": from_season,
        "to_season": to_season,
        "training_from_season": training_from_season,
        "methods": {
            "equal": "Calibrated total; split total residual equally; preserve raw FL margin.",
            "proportional": "Scale both raw team scores by calibrated_total/raw_total.",
            "ppd_weighted": "Allocate total residual by each side's projected PPD share.",
            "dual": "Calibrate total and margin independently; margin uses raw FL margin + PPD diff + drive diff.",
        },
        "walk_forward": folds,
        "pooled": {m: _metrics(evaluated, m) for m in methods},
        "favorite_compression": {m: _compression(evaluated, m) for m in methods},
    }
