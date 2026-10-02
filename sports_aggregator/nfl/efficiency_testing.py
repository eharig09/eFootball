"""Efficiency and special-teams tests for NFL margins and totals, against the lean model and the closing lines.

Baseline is the lean model (QB, blended success rate, turnovers, penalties), WITHOUT weather for totals, because the
weather effect rests on observed (not forecast) weather and would blur everything else (see lean_model).

Stages added to the baseline, one at a time and together:
  punting   net punt yards and punt-return yards per team
  kicker    field goals over expected (accuracy, range) as expected points, plus kickoff touchback rate
  eff       blended explosive rate, touchdown rate per drive, red-zone touchdown rate, three-and-out rate, field position
  ppd       a points-per-drive model (these components + QB + kicker) times the projected drives

A validation section checks the special-teams ratings directly: do pregame kicker and punter ratings predict the next
kicks? Everything is walk-forward by season; Vegas is a benchmark only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from sports_aggregator.nfl import efficiency_st as st
from sports_aggregator.nfl.lean_model import (
    QB_MARGIN, QB_TOTAL, _attach, _market, _paired, _picks, _score, _su, _walk, blended_features,
)
from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict as _predict_margin
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.pace_totals import _team_games
from sports_aggregator.nfl.qb_player_ablation import _shrunk_states, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import _fit_ridge, _predict
from sports_aggregator.nfl.weather_total_ablation import load_weather

MODEL_VERSION = "nfl-efficiency-special-teams-v1"

#: Fixed (not fitted) conversion for the special-teams adjustment: about 0.04 expected points per yard of field position,
#: and roughly 4.5 punts per team per game. The effects are real but far too small to learn weights for from ~2,400 games.
POINTS_PER_YARD = 0.04
PUNTS_PER_TEAM = 4.5

PUNT = ("st_punt_net_{s}", "st_punt_ret_{s}")
KICKER = ("st_fg_pts_{s}", "st_kicker_acc_{s}", "st_kicker_rng_{s}", "st_ko_tb_{s}")
EFF = tuple(f"eff_{n}_{{s}}" for n in ("explosive", "td", "rz_td", "three_out", "start"))
LEAN = {"diff": ("bl_succ_diff", "bl_to_diff", "bl_pen_diff") + QB_MARGIN,
        "sum": ("bl_succ_sum", "bl_to_sum", "bl_pen_sum") + QB_TOTAL}


def _f(names: tuple[str, ...], suffix: str) -> tuple[str, ...]:
    return tuple(n.format(s=suffix) for n in names)


def _stages(suffix: str, extra_ppd: tuple[str, ...]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    base = LEAN[suffix]
    stages = [
        ("lean", base),
        ("lean_punting", base + _f(PUNT, suffix)),
        ("lean_kicker", base + _f(KICKER, suffix)),
        ("lean_special_teams", base + _f(PUNT, suffix) + _f(KICKER, suffix)),
        ("lean_efficiency", base + _f(EFF, suffix)),
        ("lean_eff_and_st", base + _f(EFF, suffix) + _f(PUNT, suffix) + _f(KICKER, suffix)),
    ]
    for name in ("explosive", "td", "rz_td", "three_out", "start"):
        stages.append((f"lean_only_{name}", base + (f"eff_{name}_{suffix}",)))
    stages.append(("ppd_model_alone", extra_ppd))
    stages.append(("lean_plus_ppd_model", base + extra_ppd))
    return tuple(stages)


# ------------------------------------------------------------------------------------------ points-per-drive model
PPD_SIDE = ("eff_explosive", "eff_td", "eff_rz_td", "eff_three_out", "eff_start", "bl_succ")


def attach_ppd_model(rows: list[dict[str, Any]], states, team_games) -> None:
    """Walk-forward points-per-drive model; adds eff_pace_total / eff_pace_margin = projected drives x predicted ppd."""
    actual = {(r["game_id"], r["team"]): r for r in team_games}
    side_rows = []
    for r in rows:
        if r.get("eff_td_h") is None or r.get("bl_succ_h") is None or r.get("sum_pred_drives") is None:
            continue
        for side, team_key in (("h", "home_team"), ("a", "away_team")):
            team = canon_team(r[team_key])
            a = actual.get((str(r["game_id"]), team))
            state = states.get((str(r["game_id"]), r[team_key]))
            if not a or not state:
                continue
            row = {"game_id": r["game_id"], "season": int(r["season"]), "side": side, "ppd": a["pts"] / a["drives"],
                   "qb_epa": state["epa"], "qb_exp": state["exp"], "fg_pts": r[f"st_fg_pts_{side}"]}
            for name in PPD_SIDE:
                row[name] = r[f"{name}_{side}"]
            side_rows.append(row)
    features = PPD_SIDE + ("qb_epa", "qb_exp", "fg_pts")
    preds: dict[tuple[str, str], float] = {}
    for season in sorted({r["season"] for r in side_rows}):
        train = [r for r in side_rows if r["season"] < season]
        model = _fit_ridge(train, features, "ppd")
        if model is None:
            continue
        for r in side_rows:
            if r["season"] == season:
                preds[(str(r["game_id"]), r["side"])] = _predict(model, r)
    for r in rows:
        ph, pa = preds.get((str(r["game_id"]), "h")), preds.get((str(r["game_id"]), "a"))
        if ph is None or pa is None:
            continue
        xd_h, xd_a = (r["sum_pred_drives"] + r["diff_pred_drives"]) / 2.0, (r["sum_pred_drives"] - r["diff_pred_drives"]) / 2.0
        r["eff_pace_total"], r["eff_pace_margin"] = xd_h * ph + xd_a * pa, xd_h * ph - xd_a * pa


# ------------------------------------------------------------------------------------------ validation
def _quintiles(pairs: list[tuple[float, float, float]], label: str) -> dict[str, Any]:
    """(pregame rating, realised outcome, weight) -> mean realised outcome by rating quintile."""
    if len(pairs) < 100:
        return {"n": len(pairs)}
    arr = np.asarray(pairs)
    order = np.argsort(arr[:, 0])
    out = []
    for chunk in np.array_split(order, 5):
        w = arr[chunk, 2]
        out.append({"mean_pregame_rating": round(float(np.average(arr[chunk, 0], weights=w)), 4),
                    "mean_realised": round(float(np.average(arr[chunk, 1], weights=w)), 4), "n": int(w.sum())})
    ratings = arr[:, 0]
    slope = float(np.polyfit(ratings, arr[:, 1], 1)[0]) if ratings.std() else 0.0
    return {"label": label, "quintiles": out, "slope_realised_on_rating": round(slope, 3),
            "top_minus_bottom": round(out[-1]["mean_realised"] - out[0]["mean_realised"], 4)}


def validate(extras: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    kicks = [k for k in extras["kicks"] if k["known"]]
    return {
        "kicker_accuracy_short_kicks": _quintiles([(k["pre"], k["actual_over_expected"], 1.0) for k in kicks if not k["long"]],
                                                  "made-over-expected per short attempt"),
        "kicker_range_long_kicks": _quintiles([(k["pre"], k["actual_over_expected"], 1.0) for k in kicks if k["long"]],
                                              "made-over-expected per long attempt"),
        "punter_net_yards": _quintiles([(p["pre_net"], p["net"], p["punts"]) for p in extras["punts"]], "net yards per punt"),
        "kicks_with_known_kicker": len(kicks),
    }


# ------------------------------------------------------------------------------------------ report
def _block(rows, folds, stages, target, base, line_key, fit_name):
    labels = [l for l, _ in stages]
    out = {"n": len(rows), "pooled": {l: _score(rows, f"pred_{l}", target) for l in labels},
           "vegas": _score(rows, line_key, target),
           "paired_vs_lean": {l: _paired(rows, f"pred_{base}", f"pred_{l}", target) for l in labels if l != base},
           "paired_vs_vegas": {l: _paired(rows, line_key, f"pred_{l}", target) for l in labels},
           "seasons_better_than_lean": {l: f"{sum(_score(f, f'pred_{l}', target)['mae'] < _score(f, f'pred_{base}', target)['mae'] for f in folds)}/{len(folds)}"
                                        for l in labels if l != base}}
    return out


def report(repository: NFLRepository, *, start_season=2013, end_season=2025, cache: str | Path = "instance/nflverse_raw"):
    rows = build_rows(repository, start_season, end_season)
    states = _shrunk_states(repository, start_season, end_season)
    _attach(rows, blended_features(repository, start_season, end_season, cache), states, load_weather(repository)[0])
    features, extras = st.pregame_features(repository, start_season, end_season, cache, with_extras=True)
    st.attach(rows, features)
    # the matchup-blended success and turnover rates by side (needed for the points-per-drive model)
    for r in rows:
        if "bl_succ_h" in r:
            r["bl_succ_a"] = r.get("bl_succ_a")
    attach_ppd_model(rows, states, _team_games(repository, 2010, end_season))
    market = _market(repository)
    for r in rows:
        r["market_margin"], r["market_total"] = market.get(str(r["game_id"]), (None, None))

    # ---------------------------------------------------------------- margin
    stages_m = _stages("diff", ("eff_pace_margin",))
    need = set().union(*(set(f) for _, f in stages_m))
    msample = [r for r in rows if r.get("actual_margin") is not None and r.get("market_margin") is not None
               and all(r.get(k) is not None for k in need)]
    mpool, mfolds = _walk(msample, stages_m, _fit, _predict_margin)
    margin = _block(mpool, mfolds, stages_m, "actual_margin", "lean", "market_margin", "margin")
    margin["straight_up"] = {**{l: _su(mpool, f"pred_{l}")["accuracy"] for l, _ in stages_m},
                             "vegas": _su(mpool, "market_margin")["accuracy"]}
    for r in mpool:       # fixed-weight special teams: kicker points count at face value, punting yards converted to points
        punt_points = POINTS_PER_YARD * PUNTS_PER_TEAM * r["st_punt_net_diff"]
        r["pred_lean_kicker_fixed"] = r["pred_lean"] + r["st_fg_pts_diff"]
        r["pred_lean_punting_fixed"] = r["pred_lean"] + punt_points
        r["pred_lean_st_fixed"] = r["pred_lean"] + r["st_fg_pts_diff"] + punt_points
    fixed_m = ("lean_kicker_fixed", "lean_punting_fixed", "lean_st_fixed")
    margin["fixed_weight_special_teams"] = {
        "pooled": {l: _score(mpool, f"pred_{l}", "actual_margin") for l in fixed_m},
        "paired_vs_lean": {l: _paired(mpool, "pred_lean", f"pred_{l}", "actual_margin") for l in fixed_m},
        "seasons_better_than_lean": {l: f"{sum(_score(f, f'pred_{l}', 'actual_margin')['mae'] < _score(f, 'pred_lean', 'actual_margin')['mae'] for f in mfolds)}/{len(mfolds)}" for l in fixed_m},
        "mean_adjustment_points": {l: round(float(np.mean([abs(r[f'pred_{l}'] - r['pred_lean']) for r in mpool])), 3) for l in fixed_m},
        "straight_up": {l: _su(mpool, f"pred_{l}")["accuracy"] for l in fixed_m},
        "ats_picks": {l: _picks(mpool, f"pred_{l}", "market_margin", "actual_margin") for l in fixed_m},
    }
    margin["ats_picks"] = {l: _picks(mpool, f"pred_{l}", "market_margin", "actual_margin") for l in ("lean", "lean_special_teams", "lean_efficiency", "lean_eff_and_st", "lean_plus_ppd_model")}

    # ---------------------------------------------------------------- total (no weather; see module docstring)
    stages_t = _stages("sum", ("eff_pace_total",))
    need_t = set().union(*(set(f) for _, f in stages_t))
    tsample = [r for r in rows if r.get("actual_total") is not None and r.get("market_total") is not None
               and all(r.get(k) is not None for k in need_t)]
    tpool, tfolds = _walk(tsample, stages_t, lambda t, f: _fit_ridge(t, f, "actual_total"), _predict)
    total = _block(tpool, tfolds, stages_t, "actual_total", "lean", "market_total", "total")
    for r in tpool:       # totals: the two kickers' combined points over average; punting is near zero-sum so not applied
        r["pred_lean_kicker_fixed"] = r["pred_lean"] + r["st_fg_pts_sum"]
    total["fixed_weight_kicker"] = {
        "pooled": _score(tpool, "pred_lean_kicker_fixed", "actual_total"),
        "paired_vs_lean": _paired(tpool, "pred_lean", "pred_lean_kicker_fixed", "actual_total"),
        "seasons_better_than_lean": f"{sum(_score(f, 'pred_lean_kicker_fixed', 'actual_total')['mae'] < _score(f, 'pred_lean', 'actual_total')['mae'] for f in tfolds)}/{len(tfolds)}",
        "mean_adjustment_points": round(float(np.mean([abs(r['st_fg_pts_sum']) for r in tpool])), 3),
        "ou_picks": _picks(tpool, "pred_lean_kicker_fixed", "market_total", "actual_total"),
    }
    total["ou_picks"] = {l: _picks(tpool, f"pred_{l}", "market_total", "actual_total") for l in ("lean", "lean_special_teams", "lean_efficiency", "lean_eff_and_st", "lean_plus_ppd_model")}
    total["ou_picks_edge_3plus"] = {l: _picks(tpool, f"pred_{l}", "market_total", "actual_total", 3.0) for l in ("lean", "lean_eff_and_st", "lean_plus_ppd_model")}

    league = {"net_punt": float(np.mean([f["sides"][t]["off_punt_net"] for f in features.values() for t in f["teams"]])),
              "punt_return": float(np.mean([f["sides"][t]["off_punt_ret"] for f in features.values() for t in f["teams"]])),
              "kicker_accuracy_sd": float(np.std([f["kicker"][t][0] for f in features.values() for t in f["teams"]])),
              "kicker_range_sd": float(np.std([f["kicker"][t][1] for f in features.values() for t in f["teams"]]))}
    return {"version": MODEL_VERSION, "market_used": False,
            "baseline_note": "totals baseline is the lean model WITHOUT weather (weather rests on observed, not forecast, data)",
            "special_teams_validation": validate(extras), "league_context": {k: round(v, 3) for k, v in league.items()},
            "margin": margin, "total": total}
