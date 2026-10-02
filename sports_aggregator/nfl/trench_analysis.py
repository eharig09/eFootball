"""Trench analysis: offensive line against defensive front, tested against the lean model and the closing lines.

Four questions, each answered on its own terms:

  1. MECHANISM  Does the matchup blend of the two lines predict what lines actually do (sack rate, EPA per dropback and
                per rush) -- and is the blend (own line with the opponent's front) better than either side alone?
  2. FINAL      Do trench features improve the lean model's margin or total?
  3. PRESSURE   The same, with charted pressure rate, for the seasons (2021+) where it exists.
  4. VALIDITY   Does the free-data line measure agree with PFF's 2025 pass-block grades (the only PFF season we hold)?

Vegas is scored as a benchmark only. Totals are compared without weather (see lean_model). Walk-forward by season.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np

from sports_aggregator.nfl import trench_features as tf
from sports_aggregator.nfl.lean_model import (
    QB_MARGIN, QB_TOTAL, _attach, _market, _paired, _picks, _score, _su, _walk, blended_features,
)
from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict as _predict_margin
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.qb_player_ablation import _shrunk_states, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import _fit_ridge, _predict
from sports_aggregator.nfl.weather_total_ablation import load_weather

MODEL_VERSION = "nfl-trench-analysis-v1"

PASS = ("tr_sack_{s}", "tr_hit_{s}")
RUN = ("tr_stuff_{s}", "tr_tfl_{s}")
AVAIL = ("tr_ol_out_{s}", "tr_dl_out_{s}")
LEAN = {"diff": ("bl_succ_diff", "bl_to_diff", "bl_pen_diff") + QB_MARGIN,
        "sum": ("bl_succ_sum", "bl_to_sum", "bl_pen_sum") + QB_TOTAL}


def _f(names: tuple[str, ...], suffix: str) -> tuple[str, ...]:
    return tuple(n.format(s=suffix) for n in names)


def stages(suffix: str, extra: tuple[str, ...] = ()) -> tuple[tuple[str, tuple[str, ...]], ...]:
    base = LEAN[suffix]
    return (
        ("lean", base),
        ("lean_pass_rush", base + _f(PASS, suffix)),
        ("lean_run_game", base + _f(RUN, suffix)),
        ("lean_both_lines", base + _f(PASS, suffix) + _f(RUN, suffix)),
        ("lean_line_availability", base + _f(AVAIL, suffix)),
        ("lean_trench_all", base + _f(PASS, suffix) + _f(RUN, suffix) + _f(AVAIL, suffix)),
        ("trench_only", _f(PASS, suffix) + _f(RUN, suffix) + _f(AVAIL, suffix)),
    ) + tuple((label, base + feats) for label, feats in extra)


# ------------------------------------------------------------------------------------------ 1. mechanism
def _corr(a, b) -> float | None:
    a, b = np.asarray(a, float), np.asarray(b, float)
    return round(float(np.corrcoef(a, b)[0, 1]), 3) if len(a) > 30 and a.std() and b.std() else None


def blend_vs_one_sided(snapshots, actuals) -> dict[str, Any]:
    """Which predicts a team-game's realised rate best: its own line, the opposing front, or the average of the two?"""
    out = {}
    for name, (num, den) in {"sack_rate": ("sacks", "dropbacks"), "hit_rate": ("hits", "dropbacks"),
                             "stuff_rate": ("stuffs", "rushes"), "pass_epa": ("pass_epa", "dropbacks"),
                             "rush_epa": ("rush_epa", "rushes")}.items():
        key = {"sack_rate": "sack", "hit_rate": "hit", "stuff_rate": "stuff", "pass_epa": "pass_epa", "rush_epa": "rush_epa"}[name]
        y, own, opp, mix, w = [], [], [], [], []
        for gid, f in snapshots.items():
            for team, other in (f["teams"], f["teams"][::-1]):
                a = actuals.get((gid, team))
                if not a or a[den] < 10:
                    continue
                s_own, s_opp = f["sides"][team], f["sides"][other]
                y.append(a[num] / a[den])
                own.append(s_own[f"off_{key}"])
                opp.append(s_opp[f"def_{key}"])
                mix.append(tf.blend(s_own, s_opp, key))
                w.append(a[den])
        out[name] = {"games": len(y), "corr_own_offence_only": _corr(own, y), "corr_opposing_defence_only": _corr(opp, y),
                     "corr_matchup_blend": _corr(mix, y)}
    return out


def mechanism(rows, snapshots_actuals) -> dict[str, Any]:
    """Walk-forward: does the line matchup add to a team's pass / rush EPA beyond the EPA blend itself?"""
    actuals = snapshots_actuals
    side_rows = []
    for r in rows:
        if r.get("tr_sack_h") is None or r.get("tr_ol_out_h") is None:
            continue
        for side, other, team_key in (("h", "a", "home_team"), ("a", "h", "away_team")):
            a = actuals.get((str(r["game_id"]), canon_team(r[team_key])))
            if not a or a["dropbacks"] < 10 or a["rushes"] < 5:
                continue
            side_rows.append({
                "season": int(r["season"]), "game_id": r["game_id"],
                "pass_epa": a["pass_epa"] / a["dropbacks"], "rush_epa": a["rush_epa"] / a["rushes"],
                "bl_pass_epa": r[f"tr_pass_epa_{side}"], "bl_rush_epa": r[f"tr_rush_epa_{side}"],
                "sack": r[f"tr_sack_{side}"], "hit": r[f"tr_hit_{side}"], "stuff": r[f"tr_stuff_{side}"], "tfl": r[f"tr_tfl_{side}"],
                "own_ol_out": r[f"tr_ol_out_{side}"], "opp_dl_out": r[f"tr_dl_out_{other}"]})
    result = {}
    for target, baseline, extras in (("pass_epa", ("bl_pass_epa",), ("sack", "hit", "own_ol_out", "opp_dl_out")),
                                     ("rush_epa", ("bl_rush_epa",), ("stuff", "tfl", "own_ol_out", "opp_dl_out"))):
        sets = (("epa_blend_only", baseline), ("plus_line_matchup", baseline + extras))
        pooled = []
        for season in sorted({r["season"] for r in side_rows}):
            train = [r for r in side_rows if r["season"] < season]
            test = [dict(r) for r in side_rows if r["season"] == season]
            models = {l: _fit_ridge(train, f, target) for l, f in sets}
            if any(m is None for m in models.values()) or not test:
                continue
            for r in test:
                for l, m in models.items():
                    r[f"pred_{l}"] = _predict(m, r)
            pooled.extend(test)
        base_err = np.asarray([r["pred_epa_blend_only"] - r[target] for r in pooled])
        full_err = np.asarray([r["pred_plus_line_matchup"] - r[target] for r in pooled])
        d = np.abs(full_err) - np.abs(base_err)
        y = np.asarray([r[target] for r in pooled])
        result[target] = {
            "team_games": len(pooled),
            "mae_epa_blend_only": round(float(np.abs(base_err).mean()), 4),
            "mae_plus_line_matchup": round(float(np.abs(full_err).mean()), 4),
            "r2_epa_blend_only": round(float(1 - (base_err ** 2).sum() / ((y - y.mean()) ** 2).sum()), 4),
            "r2_plus_line_matchup": round(float(1 - (full_err ** 2).sum() / ((y - y.mean()) ** 2).sum()), 4),
            "paired_t_line_matchup_vs_blend": round(float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))), 2)}
    return result


# ------------------------------------------------------------------------------------------ 4. PFF validity
def pff_agreement(repository: NFLRepository, actuals, season: int = 2025) -> dict[str, Any]:
    """Rank agreement between PFF's 2025 team pass-block grade and the free-data measures, over the 32 teams."""
    with closing(repository._connect()) as connection:
        grades = {}
        for r in connection.execute(
                """SELECT team, metric, value, pff_id FROM nfl_pff_player_metrics
                   WHERE season=? AND family='offense_blocking'
                     AND metric IN ('grades_pass_block','snap_counts_pass_block','pbe')""", (season,)):
            grades.setdefault((canon_team(r["team"]), r["pff_id"]), {})[r["metric"]] = r["value"]
    by_team: dict[str, dict[str, list[float]]] = {}
    for (team, _), m in grades.items():
        snaps = m.get("snap_counts_pass_block") or 0
        if snaps >= 100 and m.get("grades_pass_block") is not None:
            slot = by_team.setdefault(team, {"grade": [], "pbe": [], "w": []})
            slot["grade"].append(m["grades_pass_block"])
            slot["pbe"].append(m.get("pbe") if m.get("pbe") is not None else np.nan)
            slot["w"].append(snaps)
    free: dict[str, dict[str, float]] = {}
    for (gid, team), a in actuals.items():
        if a["season"] != season or a["dropbacks"] < 10:
            continue
        slot = free.setdefault(team, dict.fromkeys(("db", "sk", "ht", "pr", "pdb"), 0.0))
        slot["db"] += a["dropbacks"]; slot["sk"] += a["sacks"]; slot["ht"] += a["hits"]          # noqa: E702
        slot["pr"] += a["pressures"]; slot["pdb"] += a["press_db"]                               # noqa: E702
    teams = sorted(set(by_team) & set(free))
    if len(teams) < 20:
        return {"teams": len(teams), "note": "too few teams with both sources"}

    def rank(v):
        order = np.argsort(np.argsort(v))
        return order.astype(float)

    grade = np.array([np.average(by_team[t]["grade"], weights=by_team[t]["w"]) for t in teams])
    sack_hit = np.array([(free[t]["sk"] + free[t]["ht"]) / free[t]["db"] for t in teams])
    sack = np.array([free[t]["sk"] / free[t]["db"] for t in teams])
    press = np.array([free[t]["pr"] / free[t]["pdb"] if free[t]["pdb"] else np.nan for t in teams])
    ok = ~np.isnan(press)
    spearman = lambda a, b: round(float(np.corrcoef(rank(a), rank(b))[0, 1]), 3)           # noqa: E731
    return {"teams": len(teams), "season": season,
            "spearman_pff_grade_vs_sack_plus_hit_rate_allowed": spearman(grade, sack_hit),
            "spearman_pff_grade_vs_sack_rate_allowed": spearman(grade, sack),
            "spearman_pff_grade_vs_pressure_rate_allowed": spearman(grade[ok], press[ok]),
            "expected_sign": "negative: a better PFF line allows fewer sacks, hits and pressures"}


# ------------------------------------------------------------------------------------------ report
def _evaluate(sample, stage_list, target, line_key, fit, predict, base="lean"):
    pooled, folds = _walk(sample, stage_list, fit, predict)
    labels = [l for l, _ in stage_list]
    return pooled, folds, {
        "n": len(pooled),
        "pooled": {**{l: _score(pooled, f"pred_{l}", target) for l in labels}, "vegas": _score(pooled, line_key, target)},
        "paired_vs_lean": {l: _paired(pooled, f"pred_{base}", f"pred_{l}", target) for l in labels if l != base},
        "paired_vs_vegas": {l: _paired(pooled, line_key, f"pred_{l}", target) for l in labels},
        "seasons_better_than_lean": {l: f"{sum(_score(f, f'pred_{l}', target)['mae'] < _score(f, f'pred_{base}', target)['mae'] for f in folds)}/{len(folds)}"
                                     for l in labels if l != base}}


def report(repository: NFLRepository, *, start_season=2013, end_season=2025, cache: str | Path = "instance/nflverse_raw"):
    rows = build_rows(repository, start_season, end_season)
    states = _shrunk_states(repository, start_season, end_season)
    _attach(rows, blended_features(repository, start_season, end_season, cache), states, load_weather(repository)[0])
    snapshots, actuals = tf.pregame_snapshots(repository, start_season, end_season, cache, with_actuals=True)
    tf.attach(rows, snapshots, tf.line_availability(repository, start_season, end_season))
    market = _market(repository)
    for r in rows:
        r["market_margin"], r["market_total"] = market.get(str(r["game_id"]), (None, None))

    press_m = (("lean_pressure", ("tr_press_{s}",)),)
    out: dict[str, Any] = {"version": MODEL_VERSION, "market_used": False}
    out["mechanism"] = {"matchup_blend_vs_one_sided": blend_vs_one_sided(snapshots, actuals),
                        "does_the_line_matchup_add_to_epa": mechanism(rows, actuals)}
    out["pff_2025_agreement"] = pff_agreement(repository, actuals)

    for kind, suffix, target, line, fit, predict in (
            ("margin", "diff", "actual_margin", "market_margin", _fit, _predict_margin),
            ("total", "sum", "actual_total", "market_total", lambda t, f: _fit_ridge(t, f, "actual_total"), _predict)):
        stage_list = stages(suffix)
        need = set().union(*(set(f) for _, f in stage_list))
        sample = [r for r in rows if r.get(target) is not None and r.get(line) is not None
                  and all(r.get(k) is not None for k in need)]
        pooled, folds, block = _evaluate(sample, stage_list, target, line, fit, predict)
        if kind == "margin":
            block["straight_up"] = {l: _su(pooled, f"pred_{l}")["accuracy"] for l, _ in stage_list}
            block["ats_picks"] = {l: _picks(pooled, f"pred_{l}", line, target) for l in ("lean", "lean_both_lines", "lean_trench_all")}
        else:
            block["ou_picks"] = {l: _picks(pooled, f"pred_{l}", line, target) for l in ("lean", "lean_both_lines", "lean_trench_all")}
        # biggest mismatches only: does the model do better where the lines are most lopsided?
        key = "tr_havoc_diff" if kind == "margin" else "tr_havoc_sum"
        cut = float(np.quantile([abs(r[key] - np.median([x[key] for x in pooled])) for r in pooled], 0.9))
        extreme = [r for r in pooled if abs(r[key] - np.median([x[key] for x in pooled])) >= cut]
        block["most_lopsided_decile"] = {
            "n": len(extreme), "lean_mae": _score(extreme, "pred_lean", target)["mae"],
            "lean_trench_all_mae": _score(extreme, "pred_lean_trench_all", target)["mae"],
            "paired_trench_vs_lean": _paired(extreme, "pred_lean", "pred_lean_trench_all", target)}
        out[kind] = block

        # ---- charted pressure rate (2021+), a separate and shorter sample
        stage_p = (("lean", LEAN[suffix]), ("lean_pressure", LEAN[suffix] + (f"tr_press_{suffix}",)),
                   ("lean_pressure_and_lines", LEAN[suffix] + (f"tr_press_{suffix}",) + _f(PASS, suffix) + _f(RUN, suffix)))
        need_p = set().union(*(set(f) for _, f in stage_p))
        sample_p = [r for r in rows if int(r["season"]) >= 2021 and r.get(target) is not None and r.get(line) is not None
                    and all(r.get(k) is not None for k in need_p)]
        if len(sample_p) > 400:
            _, _, pblock = _evaluate(sample_p, stage_p, target, line, fit, predict)
            out[f"{kind}_charted_pressure_2021_plus"] = pblock
    return out
