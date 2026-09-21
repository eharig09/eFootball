"""Incremental walk-forward feature ablation for the CFB margin model.

Builds on the validated dual total/margin architecture and asks which
pregame football-strength features actually improve margin/score accuracy.
Vegas is never a margin-model feature; market spread is used only for
compression diagnostics.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.cfb.game_projection import _shrunk_blend
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.xpoints import DATASET_VERSION as XPOINTS_VERSION
from sports_aggregator.cfb.xredzone import DATASET_VERSION as XREDZONE_VERSION
from sports_aggregator.cfb.xredzone import SHRINKAGE_PSEUDO_GAMES as REDZONE_PSEUDO_GAMES
from sports_aggregator.cfb.xturnovers import DATASET_VERSION as XTURNOVERS_VERSION


FEATURE_SETS = {
    "base": ("raw_margin", "ppd_diff", "drive_diff"),
    "plus_elo": ("raw_margin", "ppd_diff", "drive_diff", "elo_diff"),
    "plus_core": ("raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin"),
    "plus_fpi": ("raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin", "fpi_margin"),
    "plus_recent": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff",
    ),
    "plus_yards": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "yards_diff",
    ),
    "plus_returning_production": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "yards_diff", "returning_ppa_diff",
    ),
    # Do these two Milestone 9 shape signals add anything to margin once
    # plus_recent (the live margin-v2 variant) is already in? turnover_diff
    # differences a team-vs-league-prior giveaway estimate that xturnovers.py's
    # own backtest found LESS accurate than the league prior alone at
    # predicting turnovers themselves -- included anyway, since a feature
    # can still carry margin signal even if it's a mediocre turnover-rate
    # predictor in isolation. red_zone_diff differences each side's shrunk
    # trips-per-drive x red-zone-TD-rate (the pairing xredzone.py's own
    # backtest found DOES beat the league prior), so it's the more likely
    # of the two to earn its place.
    "plus_turnovers": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "turnover_diff",
    ),
    "plus_redzone": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "red_zone_diff",
    ),
    "plus_turnovers_redzone": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "turnover_diff", "red_zone_diff",
    ),
    # Built on plus_redzone (the live margin-v2 tier as of this pass), not
    # plus_recent: any further addition should earn its keep against what's
    # actually deployed, not an older baseline. hc_diff/qb_diff are the same
    # pregame person-Elo differentials Engine A already uses categorically as
    # its 4th convergence signal -- untested here as continuous regression
    # inputs to margin-v2 itself. Weather features are the latest pregame
    # forecast per game (same source the live matchup page already shows),
    # never post-game observed conditions.
    "plus_hc_qb_elo": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "red_zone_diff", "hc_diff", "qb_diff",
    ),
    "plus_weather": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "red_zone_diff",
        "weather_temperature", "weather_wind", "weather_precipitation",
    ),
    "plus_hc_qb_weather": (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "red_zone_diff", "hc_diff", "qb_diff",
        "weather_temperature", "weather_wind", "weather_precipitation",
    ),
}


def _fit_ridge(train: list[dict[str, Any]], features: tuple[str, ...], l2: float = 2.0):
    rows = [r for r in train if r.get("actual_margin") is not None
            and all(r.get(k) is not None for k in features)]
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
    for r in rows:
        x = [1.0] + [(float(r[k]) - means[k]) / scales[k] for k in features]
        y = float(r["actual_margin"])
        for i in range(size):
            xty[i] += x[i] * y
            for j in range(size):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, size):
        xtx[i][i] += l2
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
        beta[rr] = (
            aug[rr][n] - sum(aug[rr][cc] * beta[cc] for cc in range(rr + 1, n))
        ) / aug[rr][rr]
    return {
        "features": features, "means": means, "scales": scales,
        "beta": beta, "n": len(rows), "l2": l2,
    }


def _predict(model, row):
    if model is None or any(row.get(k) is None for k in model["features"]):
        return None
    value = model["beta"][0]
    for i, key in enumerate(model["features"], 1):
        value += model["beta"][i] * (
            (float(row[key]) - model["means"][key]) / model["scales"][key]
        )
    return float(value)


def _linear_total_fit(train):
    pairs = [(r["raw_total"], r["actual_total"]) for r in train
             if r.get("raw_total") is not None and r.get("actual_total") is not None]
    if len(pairs) < 50:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    var = sum((x - mx) ** 2 for x, _ in pairs)
    if var <= 1e-12:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pairs) / var
    return {"intercept": my - slope * mx, "slope": slope, "n": len(pairs)}


def _side_turnover_rate(row: dict[str, Any]) -> float | None:
    """Same shrink xredzone-style modules use, applied to the giveaway rate
    for this diff feature specifically -- not what the live projection
    serves for giveaways themselves, which uses the league prior outright
    (see game_projection.py's comment: shrinkage never beat it there)."""
    league = row.get("league_prior_giveaway_rate")
    if league is None:
        return None
    team = row.get("team_prior_giveaway_rate")
    allowed = row.get("opponent_prior_takeaway_rate")
    sample = min(row.get("to_team_games") or 0, row.get("to_opp_games") or 0)
    return _shrunk_blend(team, allowed, league, sample, pseudo_games=6.0)


def _side_red_zone_rate(row: dict[str, Any]) -> float | None:
    """Expected red-zone TDs per drive: shrunk trips-per-drive x shrunk
    red-zone TD rate, the same two shrinks the live projection now serves."""
    trips_league = row.get("league_prior_trips_per_drive")
    td_league = row.get("league_prior_red_zone_td_rate")
    if trips_league is None or td_league is None:
        return None
    sample = min(row.get("rz_team_games") or 0, row.get("rz_opp_games") or 0)
    trips = _shrunk_blend(
        row.get("team_prior_trips_per_drive"), row.get("opponent_prior_trips_allowed_per_drive"),
        trips_league, sample, pseudo_games=REDZONE_PSEUDO_GAMES["trips_per_drive"],
    )
    td_rate = _shrunk_blend(
        row.get("team_prior_red_zone_td_rate"), row.get("opponent_prior_red_zone_td_rate_allowed"),
        td_league, sample, pseudo_games=REDZONE_PSEUDO_GAMES["red_zone_td_rate"],
    )
    return trips * td_rate if trips is not None and td_rate is not None else None


def _load(repository, start: int, end: int, version: str):
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT p.game_id,p.season,p.side,p.team,p.opponent,
                      p.projected_offensive_points,p.projected_points_per_drive,
                      p.projected_drives,p.projected_total_yards,p.actual_score_points,
                      g.week,g.home_team,g.away_team,
                      x.elo_difference,x.core_margin,x.fpi_margin,
                      x.team_recent_margin,x.opponent_recent_margin,
                      rp.percent_ppa AS returning_percent_ppa,
                      t.team_prior_games AS to_team_games, t.team_prior_giveaway_rate,
                      t.opponent_prior_games AS to_opp_games, t.opponent_prior_takeaway_rate,
                      t.league_prior_giveaway_rate,
                      rz.team_prior_games AS rz_team_games,
                      rz.team_prior_trips_per_drive, rz.team_prior_red_zone_td_rate,
                      rz.opponent_prior_games AS rz_opp_games,
                      rz.opponent_prior_trips_allowed_per_drive,
                      rz.opponent_prior_red_zone_td_rate_allowed,
                      rz.league_prior_trips_per_drive, rz.league_prior_red_zone_td_rate,
                      hc.home_pre_elo AS hc_home_pre_elo, hc.away_pre_elo AS hc_away_pre_elo,
                      (SELECT q.pre_rating FROM cfb_qb_elo_games q
                       WHERE q.game_id=p.game_id AND q.side='home') qb_home_pre_rating,
                      (SELECT q.pre_rating FROM cfb_qb_elo_games q
                       WHERE q.game_id=p.game_id AND q.side='away') qb_away_pre_rating,
                      (SELECT w.temperature FROM game_weather w WHERE w.game_id=p.game_id
                       ORDER BY w.forecast_generated_at DESC LIMIT 1) weather_temperature,
                      (SELECT w.sustained_wind FROM game_weather w WHERE w.game_id=p.game_id
                       ORDER BY w.forecast_generated_at DESC LIMIT 1) weather_wind,
                      (SELECT w.precipitation_amount FROM game_weather w WHERE w.game_id=p.game_id
                       ORDER BY w.forecast_generated_at DESC LIMIT 1) weather_precipitation,
                      (SELECT w.indoor FROM game_weather w WHERE w.game_id=p.game_id
                       ORDER BY w.forecast_generated_at DESC LIMIT 1) weather_indoor,
                      (SELECT AVG(gl.spread) FROM game_lines gl
                       WHERE gl.game_id=p.game_id AND gl.spread IS NOT NULL) market_spread
               FROM cfb_projection_backtest p
               JOIN games g USING(game_id)
               LEFT JOIN cfb_xpoints_dataset x
                 ON x.game_id=p.game_id AND x.team=p.team AND x.dataset_version=?
               LEFT JOIN returning_production rp
                 ON rp.season=p.season AND rp.team=p.team
               LEFT JOIN cfb_xturnovers_dataset t
                 ON t.game_id=p.game_id AND t.team=p.team AND t.dataset_version=?
               LEFT JOIN cfb_xredzone_dataset rz
                 ON rz.game_id=p.game_id AND rz.team=p.team AND rz.dataset_version=?
               LEFT JOIN cfb_coach_elo_games hc
                 ON hc.game_id=p.game_id
               WHERE p.backtest_version=? AND p.season BETWEEN ? AND ?
                 AND p.projected_offensive_points IS NOT NULL
                 AND p.actual_score_points IS NOT NULL
               ORDER BY p.season,g.week,p.game_id,p.side""",
            (XPOINTS_VERSION, XTURNOVERS_VERSION, XREDZONE_VERSION,
             version, int(start), int(end)),
        )]
    grouped = defaultdict(dict)
    for r in rows:
        grouped[int(r["game_id"])][str(r["side"])] = r
    out = []
    for gid, sides in grouped.items():
        h, a = sides.get("home"), sides.get("away")
        if not h or not a:
            continue
        rh = float(h["projected_offensive_points"]); ra = float(a["projected_offensive_points"])
        ah = float(h["actual_score_points"]); aa = float(a["actual_score_points"])
        h_turnover, a_turnover = _side_turnover_rate(h), _side_turnover_rate(a)
        h_red_zone, a_red_zone = _side_red_zone_rate(h), _side_red_zone_rate(a)
        out.append({
            "game_id": gid, "season": int(h["season"]), "week": int(h["week"] or 0),
            "raw_total": rh + ra, "raw_margin": rh - ra,
            "actual_total": ah + aa, "actual_margin": ah - aa,
            "ppd_diff": (
                float(h["projected_points_per_drive"]) - float(a["projected_points_per_drive"])
                if h["projected_points_per_drive"] is not None and a["projected_points_per_drive"] is not None else None
            ),
            "drive_diff": (
                float(h["projected_drives"]) - float(a["projected_drives"])
                if h["projected_drives"] is not None and a["projected_drives"] is not None else None
            ),
            "yards_diff": (
                float(h["projected_total_yards"]) - float(a["projected_total_yards"])
                if h["projected_total_yards"] is not None and a["projected_total_yards"] is not None else None
            ),
            "elo_diff": float(h["elo_difference"]) if h["elo_difference"] is not None else None,
            "core_margin": float(h["core_margin"]) if h["core_margin"] is not None else None,
            "fpi_margin": float(h["fpi_margin"]) if h["fpi_margin"] is not None else None,
            "recent_margin_diff": (
                float(h["team_recent_margin"]) - float(h["opponent_recent_margin"])
                if h["team_recent_margin"] is not None and h["opponent_recent_margin"] is not None else None
            ),
            "turnover_diff": (
                a_turnover - h_turnover if h_turnover is not None and a_turnover is not None else None
            ),
            "red_zone_diff": (
                h_red_zone - a_red_zone if h_red_zone is not None and a_red_zone is not None else None
            ),
            "returning_ppa_diff": (
                float(h["returning_percent_ppa"]) - float(a["returning_percent_ppa"])
                if h["returning_percent_ppa"] is not None and a["returning_percent_ppa"] is not None else None
            ),
            "hc_diff": (
                float(h["hc_home_pre_elo"]) - float(h["hc_away_pre_elo"])
                if h["hc_home_pre_elo"] is not None and h["hc_away_pre_elo"] is not None else None
            ),
            "qb_diff": (
                float(h["qb_home_pre_rating"]) - float(h["qb_away_pre_rating"])
                if h["qb_home_pre_rating"] is not None and h["qb_away_pre_rating"] is not None else None
            ),
            # Indoor games have no real pregame forecast worth regressing on;
            # treated as missing rather than as 0 wind/typical temperature.
            "weather_temperature": (
                float(h["weather_temperature"])
                if h["weather_temperature"] is not None and not h["weather_indoor"] else None
            ),
            "weather_wind": (
                float(h["weather_wind"])
                if h["weather_wind"] is not None and not h["weather_indoor"] else None
            ),
            "weather_precipitation": (
                float(h["weather_precipitation"])
                if h["weather_precipitation"] is not None and not h["weather_indoor"] else None
            ),
            "market_spread": float(h["market_spread"]) if h["market_spread"] is not None else None,
        })
    return out


def _bucket(x):
    if x < 3: return "<3"
    if x < 7: return "3-6.5"
    if x < 14: return "7-13.5"
    return "14+"


def _metrics(rows, key):
    vals = [r for r in rows if r.get(key) is not None and r.get("cal_total") is not None]
    if not vals:
        return {"n": 0}
    margin_err = [abs(float(r[key]) - r["actual_margin"]) for r in vals]
    score_err = []
    for r in vals:
        ph = (r["cal_total"] + float(r[key])) / 2
        pa = (r["cal_total"] - float(r[key])) / 2
        actual_home = (r["actual_total"] + r["actual_margin"]) / 2
        actual_away = (r["actual_total"] - r["actual_margin"]) / 2
        score_err.extend([abs(ph - actual_home), abs(pa - actual_away)])
    return {
        "n": len(vals),
        "margin_mae": round(sum(margin_err)/len(margin_err), 4),
        "score_mae": round(sum(score_err)/len(score_err), 4),
    }


def _compression(rows, key):
    usable = [r for r in rows if r.get(key) is not None and r.get("market_spread") not in (None, 0)]
    if not usable:
        return {"n": 0}
    recs = []
    for r in usable:
        market_home = -float(r["market_spread"])
        fav_home = market_home > 0
        market_abs = abs(market_home)
        model_fav = float(r[key]) if fav_home else -float(r[key])
        change = model_fav - market_abs
        recs.append((market_abs, model_fav, change))
    def one(vals):
        if not vals: return {"n": 0}
        n = len(vals)
        mm = sum(v[0] for v in vals)/n
        fm = sum(v[1] for v in vals)/n
        return {
            "n": n,
            "compression_ratio": round(fm/mm, 4) if mm else None,
            "mean_favorite_margin_change": round(sum(v[2] for v in vals)/n, 3),
            "favorite_more_favored_rate": round(sum(v[2] > .05 for v in vals)/n, 4),
            "underdog_closer_rate": round(sum((v[1] >= 0 and v[2] < -.05) for v in vals)/n, 4),
            "favorite_flipped_rate": round(sum(v[1] < 0 for v in vals)/n, 4),
        }
    return {
        "overall": one(recs),
        "by_bucket": {b: one([v for v in recs if _bucket(v[0]) == b])
                      for b in ("<3","3-6.5","7-13.5","14+")},
    }


def report(repository, *, from_season=2023, to_season=2025,
           training_from_season=2021, backtest_version=BACKTEST_VERSION):
    games = _load(repository, min(training_from_season, from_season), to_season, backtest_version)
    folds = []; pooled = []
    for season in range(int(from_season), int(to_season)+1):
        train = [r for r in games if training_from_season <= r["season"] < season]
        test = [dict(r) for r in games if r["season"] == season]
        tf = _linear_total_fit(train)
        if tf is None or not test:
            continue
        models = {name: _fit_ridge(train, features) for name, features in FEATURE_SETS.items()}
        for r in test:
            r["cal_total"] = tf["intercept"] + tf["slope"] * r["raw_total"]
            for name, model in models.items():
                r[f"margin_{name}"] = _predict(model, r)
        pooled.extend(test)
        folds.append({
            "season": season,
            "train_games": len(train),
            "test_games": len(test),
            "models": {
                name: {
                    "training_rows": model["n"] if model else 0,
                    "features": list(FEATURE_SETS[name]),
                    "metrics": _metrics(test, f"margin_{name}"),
                    "compression": _compression(test, f"margin_{name}"),
                }
                for name, model in models.items()
            },
        })
    return {
        "version": "cfb-margin-feature-ablation-v1",
        "backtest_version": backtest_version,
        "from_season": from_season,
        "to_season": to_season,
        "training_from_season": training_from_season,
        "feature_sets": {k:list(v) for k,v in FEATURE_SETS.items()},
        "walk_forward": folds,
        "pooled": {
            name: {
                "metrics": _metrics(pooled, f"margin_{name}"),
                "compression": _compression(pooled, f"margin_{name}"),
            }
            for name in FEATURE_SETS
        },
    }
