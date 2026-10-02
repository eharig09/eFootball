"""NFL totals through a pace / xDrives lens.

A game's total is (drives) x (points per drive). The drive model (xDrives) already projects the number of drives; this
asks whether treating that explicitly -- and adjusting a total up or down by the projected EXCESS drives over the league
norm -- beats leaving drives as one feature among many, and whether the closing total misses pace.

Definitions (all pregame, walk-forward):

  xD            projected drives, both teams (home + away) from the walk-forward drive model
  excess drives xD minus the drive model's OWN mean projection in EARLIER seasons. (Centering on the model's own
                average from the previous season, not on actual drives, matters: the projection runs about a drive below the actual count, so
                measuring against actual drives made nearly every game look like a "low-drive" game.)
  excess points excess drives x the league's points per drive in earlier seasons
  ppd blend     points per drive for a side = mean(its own offense, the OPPONENT's defense), season-decayed and
                shrunk (the same matchup blend used by the lean model)
  pace total    xD_home x ppd_home + xD_away x ppd_away

Three questions, answered separately and honestly:

  1. How good are the projected drives?       (accuracy against actual drives, and how much of a total is drives)
  2. Does a pace-based total / adjustment improve the model?   (against the core total and the lean total)
  3. Does the closing total miss pace?        (outcome vs the line, bucketed by excess drives, with a rule fixed up front)

Weather variants use observed weather, which flatters them (see lean_model); the main comparisons are without weather.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import math
from pathlib import Path
from typing import Any

import numpy as np

from sports_aggregator.nfl.drive_projection import STATE_SEASON_DECAY
from sports_aggregator.nfl.lean_model import (
    QB_TOTAL, _attach, _market, _paired, _picks, _score, _side_split, _walk, blended_features,
)
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.qb_player_ablation import _shrunk_states, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import TOTAL_FEATURES, _fit_ridge, _predict
from sports_aggregator.nfl.weather_total_ablation import WEATHER, load_weather

MODEL_VERSION = "nfl-pace-totals-v1"
PRIOR_DRIVES = 30.0                  # drives of league-average scoring mixed into every points-per-drive rating
EXCESS_RULE_Z = 1.0                  # pre-registered: |excess z| >= 1 flags a pace lean (more drives = over)

PACE_ONLY = ("pace_total",)
LEAN_NO_WX = ("bl_succ_sum", "bl_to_sum", "bl_pen_sum") + QB_TOTAL
STAGES = (
    ("core_total", TOTAL_FEATURES),
    ("pace_only", PACE_ONLY),
    ("lean_no_weather", LEAN_NO_WX),
    ("lean_plus_excess", LEAN_NO_WX + ("excess_points",)),
    ("lean_plus_pace_total", LEAN_NO_WX + ("pace_total",)),
    ("lean_plus_both", LEAN_NO_WX + ("pace_total", "excess_points")),
    ("lean_weather", LEAN_NO_WX + WEATHER),
    ("lean_weather_plus_excess", LEAN_NO_WX + WEATHER + ("excess_points",)),
)


# ------------------------------------------------------------------------------------------ actual drives / scoring
def _team_games(repository: NFLRepository, start: int, end: int) -> list[dict[str, Any]]:
    """One record per (game, team): points, drives, and the opponent's points and drives."""
    with closing(repository._connect()) as connection:
        games = {str(r["game_id"]): dict(r) for r in connection.execute(
            """SELECT game_id,season,week,home_team,away_team,home_score,away_score FROM games
               WHERE season BETWEEN ? AND ? AND season_type='REG' AND completed=1 AND home_score IS NOT NULL""",
            (int(start), int(end)))}
        drives = {(str(r["game_id"]), canon_team(r["team"])): float(r["drives"]) for r in connection.execute(
            "SELECT game_id,team,drives FROM game_team_situational WHERE season BETWEEN ? AND ? AND drives>0",
            (int(start), int(end)))}
    out = []
    for gid, g in games.items():
        home, away = canon_team(g["home_team"]), canon_team(g["away_team"])
        dh, da = drives.get((gid, home)), drives.get((gid, away))
        if not dh or not da:
            continue
        for team, opp, pts, opp_pts, d, od in ((home, away, g["home_score"], g["away_score"], dh, da),
                                              (away, home, g["away_score"], g["home_score"], da, dh)):
            out.append({"game_id": gid, "team": team, "opponent": opp, "season": int(g["season"]), "week": int(g["week"]),
                        "pts": float(pts), "drives": d, "opp_pts": float(opp_pts), "opp_drives": od})
    return out


def season_norms(records: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    """For each season: mean drives per game and points per drive over strictly EARLIER seasons (leak-safe)."""
    per_season: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])      # [drive sum, point sum, team-games]
    for r in records:
        acc = per_season[r["season"]]
        acc[0] += r["drives"]
        acc[1] += r["pts"]
        acc[2] += 1
    norms, drives, points, games = {}, 0.0, 0.0, 0.0
    for season in sorted(per_season):
        if games:
            norms[season] = {"drives_per_game": 2 * drives / games, "ppd": points / drives}
        drives, points, games = drives + per_season[season][0], points + per_season[season][1], games + per_season[season][2]
    return norms


def ppd_snapshots(records: list[dict[str, Any]], start: int) -> dict[str, dict[str, dict[str, float]]]:
    """game_id -> {team: {'off_ppd', 'def_ppd'}} from earlier week batches, season-decayed with a league prior."""
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_week[(r["season"], r["week"])].append(r)
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pts = drv = 0.0
    out: dict[str, dict[str, dict[str, float]]] = {}
    for season, week in sorted(by_week):
        batch = by_week[(season, week)]
        league = pts / drv if drv else 2.1
        snapshots: dict[str, dict[str, float]] = {}
        for r in batch:
            for team in (r["team"], r["opponent"]):
                if team in snapshots or not history[team]:
                    continue
                num = den = onum = oden = 0.0
                for h in history[team]:
                    w = STATE_SEASON_DECAY ** max(0, season - h["season"])
                    num, den, onum, oden = num + w * h["pts"], den + w * h["drives"], onum + w * h["opp_pts"], oden + w * h["opp_drives"]
                snapshots[team] = {"off_ppd": (num + PRIOR_DRIVES * league) / (den + PRIOR_DRIVES),
                                   "def_ppd": (onum + PRIOR_DRIVES * league) / (oden + PRIOR_DRIVES)}
        if season >= start:
            for r in batch:
                if r["team"] in snapshots and r["opponent"] in snapshots:
                    out.setdefault(r["game_id"], {})[r["team"]] = snapshots[r["team"]]
        for r in batch:
            history[r["team"]].append(r)
            pts, drv = pts + r["pts"], drv + r["drives"]
    return out


def xd_norms(rows: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    """Mean and spread of the drive model's own projection in the most recent EARLIER season.

    One season back, not all earlier seasons: drives per game have drifted down over time, and an all-history mean
    leaves every recent game permanently "below normal" (a rule built on it flags almost only Unders).
    """
    by_season: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("sum_pred_drives") is not None:
            by_season[int(r["season"])].append(float(r["sum_pred_drives"]))
    norms = {}
    for season in sorted(by_season):
        earlier = [s for s in by_season if s < season and len(by_season[s]) >= 100]
        if earlier:
            last = by_season[max(earlier)]
            norms[season] = {"xd_mean": float(np.mean(last)), "xd_sd": float(np.std(last))}
    return norms


def attach_pace(rows: list[dict[str, Any]], records: list[dict[str, Any]], start: int) -> None:
    norms = season_norms(records)
    centers = xd_norms(rows)
    snaps = ppd_snapshots(records, start)
    actual: dict[str, float] = defaultdict(float)
    for r in records:
        actual[r["game_id"]] += r["drives"]
    for r in rows:
        gid, norm = str(r["game_id"]), norms.get(int(r["season"]))
        sides = snaps.get(gid)
        center = centers.get(int(r["season"]))
        if norm is None or center is None or not sides or "sum_pred_drives" not in r or "diff_pred_drives" not in r:
            continue
        home, away = canon_team(r["home_team"]), canon_team(r["away_team"])
        if home not in sides or away not in sides:
            continue
        xd_home = (r["sum_pred_drives"] + r["diff_pred_drives"]) / 2.0
        xd_away = (r["sum_pred_drives"] - r["diff_pred_drives"]) / 2.0
        ppd_home = (sides[home]["off_ppd"] + sides[away]["def_ppd"]) / 2.0      # own offense with the OPPONENT's defense
        ppd_away = (sides[away]["off_ppd"] + sides[home]["def_ppd"]) / 2.0
        r.update({
            "xd_sum": r["sum_pred_drives"], "excess_drives": r["sum_pred_drives"] - center["xd_mean"],
            "excess_z": (r["sum_pred_drives"] - center["xd_mean"]) / (center["xd_sd"] or 1.0),
            "excess_points": (r["sum_pred_drives"] - center["xd_mean"]) * norm["ppd"],
            "pace_total": xd_home * ppd_home + xd_away * ppd_away, "ppd_blend_mean": (ppd_home + ppd_away) / 2.0,
            "actual_drives": actual.get(gid),
        })


# ------------------------------------------------------------------------------------------ reporting
def _corr(a, b) -> float | None:
    return round(float(np.corrcoef(a, b)[0, 1]), 3) if len(a) > 5 else None


def _drive_diagnostics(rows) -> dict[str, Any]:
    sub = [r for r in rows if r.get("actual_drives") and r.get("xd_sum")]
    actual = np.asarray([r["actual_drives"] for r in sub])
    xd = np.asarray([r["xd_sum"] for r in sub])
    total = np.asarray([r["actual_total"] for r in sub])
    mean_drives = float(actual.mean())
    ppd = total / actual
    return {
        "games": len(sub),
        "xd_mean": round(float(xd.mean()), 3), "actual_drives_mean": round(mean_drives, 3),
        "xd_mae": round(float(np.abs(xd - actual).mean()), 3),
        "league_mean_mae": round(float(np.abs(mean_drives - actual).mean()), 3),
        "actual_drives_sd": round(float(actual.std()), 3), "xd_sd": round(float(xd.std()), 3),
        "corr_xd_vs_actual_drives": _corr(xd, actual),
        "corr_actual_drives_vs_total": _corr(actual, total),
        "corr_points_per_drive_vs_total": _corr(ppd, total),
        "total_variance_share": {
            # how much of the spread in totals is explained by drives alone vs scoring rate alone
            "drives_only_r2": round((_corr(actual, total) or 0) ** 2, 3),
            "points_per_drive_only_r2": round((_corr(ppd, total) or 0) ** 2, 3)},
        "corr_xd_vs_actual_total": _corr(xd, total),
    }


def _bucket_vs_line(rows) -> dict[str, Any]:
    """Outcome against the closing total, bucketed by excess drives; and the pre-registered rule, split by era."""
    sub = [r for r in rows if r.get("excess_drives") is not None and r.get("market_total") is not None
           and r.get("actual_total") is not None]
    edges = [-99, -1.0, -0.33, 0.33, 1.0, 99]
    names = ["z<=-1 (far fewer drives)", "-1..-0.33", "-0.33..0.33", "0.33..1", "z>=1 (far more drives)"]
    buckets = {}
    for lo, hi, name in zip(edges, edges[1:], names):
        g = [r for r in sub if lo <= r["excess_z"] < hi]
        if not g:
            continue
        decided = [r for r in g if r["actual_total"] != r["market_total"]]
        buckets[name] = {"n": len(g), "mean_actual_minus_line": round(float(np.mean([r["actual_total"] - r["market_total"] for r in g])), 3),
                         "over_rate": round(sum(r["actual_total"] > r["market_total"] for r in decided) / len(decided), 4)}
    slope_x = np.asarray([r["excess_points"] for r in sub])
    slope_y = np.asarray([r["actual_total"] - r["market_total"] for r in sub])
    xc = slope_x - slope_x.mean()
    slope = float((xc * (slope_y - slope_y.mean())).sum() / (xc ** 2).sum())
    resid = slope_y - slope_y.mean() - slope * xc
    se = math.sqrt((resid ** 2).sum() / (len(xc) - 2) / (xc ** 2).sum())

    def rule(rs):
        over = [r for r in rs if r["excess_z"] >= EXCESS_RULE_Z]
        under = [r for r in rs if r["excess_z"] <= -EXCESS_RULE_Z]
        picks = ([dict(r, side=1) for r in over] + [dict(r, side=-1) for r in under])
        decided = [r for r in picks if r["actual_total"] != r["market_total"]]
        wins = sum((r["actual_total"] > r["market_total"]) == (r["side"] == 1) for r in decided)
        n = len(decided)
        return {"n": n, "over_picks": len(over), "under_picks": len(under),
                "win_rate": round(wins / n, 4) if n else None,
                "z_vs_50": round((wins - n / 2) / math.sqrt(n / 4), 2) if n else None,
                "units_at_-110": round(wins - 1.1 * (n - wins), 1) if n else None}

    seasons = sorted({int(r["season"]) for r in sub})
    mid = seasons[len(seasons) // 2]
    return {"buckets": buckets,
            "slope_of_actual_minus_line_on_excess_points": {"slope": round(slope, 4), "t": round(slope / se, 2), "n": len(xc)},
            "rule_excess_z_ge_1": {"all": rule(sub), "first_half": rule([r for r in sub if r["season"] < mid]),
                                        "second_half": rule([r for r in sub if r["season"] >= mid]), "second_half_starts": mid}}


def _overlay_on_vegas(rows) -> dict[str, Any]:
    """Walk-forward: line + a fitted pace adjustment (target = actual total minus the closing total)."""
    sub = [dict(r, overlay_target=r["actual_total"] - r["market_total"], pace_vs_line=r["pace_total"] - r["market_total"])
           for r in rows if r.get("excess_points") is not None and r.get("market_total") is not None
           and r.get("pace_total") is not None and r.get("actual_total") is not None]
    sets = (("excess_only", ("excess_points",)), ("pace_edge", ("pace_vs_line",)), ("both", ("excess_points", "pace_vs_line")))
    out = {}
    pooled: list[dict[str, Any]] = []
    for season in sorted({int(r["season"]) for r in sub}):
        train = [r for r in sub if int(r["season"]) < season]
        test = [dict(r) for r in sub if int(r["season"]) == season]
        models = {l: _fit_ridge(train, f, "overlay_target") for l, f in sets}
        if any(m is None for m in models.values()) or not test:
            continue
        for r in test:
            for l, m in models.items():
                r[f"line_plus_{l}"] = r["market_total"] + _predict(m, r)
        pooled.extend(test)
    for l, _ in sets:
        out[l] = {"mae": _score(pooled, f"line_plus_{l}", "actual_total")["mae"],
                  "vs_vegas_alone": _paired(pooled, "market_total", f"line_plus_{l}", "actual_total"),
                  "ou_picks_all": _picks(pooled, f"line_plus_{l}", "market_total", "actual_total"),
                  "ou_picks_edge_1plus": _picks(pooled, f"line_plus_{l}", "market_total", "actual_total", 1.0)}
    out["vegas_mae"] = _score(pooled, "market_total", "actual_total")["mae"]
    out["n"] = len(pooled)
    return out


def report(repository: NFLRepository, *, start_season=2013, end_season=2025,
           cache: str | Path = "instance/nflverse_raw"):
    rows = build_rows(repository, 2011, end_season)       # earlier seasons only supply the centering history
    _attach(rows, blended_features(repository, start_season, end_season, cache),
            _shrunk_states(repository, start_season, end_season), load_weather(repository)[0])
    records = _team_games(repository, 2010, end_season)
    attach_pace(rows, records, start_season)
    rows = [r for r in rows if int(r["season"]) >= start_season]
    market = _market(repository)
    for r in rows:
        r["market_margin"], r["market_total"] = market.get(str(r["game_id"]), (None, None))

    need = set().union(*(set(f) for _, f in STAGES)) | {"excess_drives"}
    sample = [r for r in rows if r.get("actual_total") is not None and r.get("market_total") is not None
              and all(r.get(k) is not None for k in need)]
    pooled, folds = _walk(sample, STAGES, lambda t, f: _fit_ridge(t, f, "actual_total"), _predict)
    labels = [l for l, _ in STAGES]

    def season_wins(a, b):
        return f"{sum(_score(f, f'pred_{a}', 'actual_total')['mae'] < _score(f, f'pred_{b}', 'actual_total')['mae'] for f in folds)}/{len(folds)}"

    return {
        "version": MODEL_VERSION, "market_used": "only as the benchmark and in the clearly-labelled overlay test",
        "drive_diagnostics": _drive_diagnostics(sample),
        "totals": {
            "n": len(pooled),
            "pooled": {**{l: _score(pooled, f"pred_{l}", "actual_total") for l in labels},
                       "vegas": _score(pooled, "market_total", "actual_total")},
            "paired_vs_core_total": {l: _paired(pooled, "pred_core_total", f"pred_{l}", "actual_total") for l in labels[1:]},
            "excess_adjustment_vs_lean": {
                "no_weather": _paired(pooled, "pred_lean_no_weather", "pred_lean_plus_excess", "actual_total"),
                "with_weather(oracle)": _paired(pooled, "pred_lean_weather", "pred_lean_weather_plus_excess", "actual_total")},
            "pace_total_vs_lean_no_weather": _paired(pooled, "pred_lean_no_weather", "pred_lean_plus_pace_total", "actual_total"),
            "vs_vegas_paired": {l: _paired(pooled, "market_total", f"pred_{l}", "actual_total") for l in labels},
            "seasons_better_than_core_total": {l: season_wins(l, "core_total") for l in labels[1:]},
            "ou_picks_all": {l: _picks(pooled, f"pred_{l}", "market_total", "actual_total") for l in labels},
            "ou_picks_edge_3plus": {l: _picks(pooled, f"pred_{l}", "market_total", "actual_total", 3.0) for l in labels},
            "side_split_no_weather_best": _side_split(pooled, "pred_lean_plus_excess", "market_total", "actual_total"),
        },
        "does_the_line_miss_pace": _bucket_vs_line(sample),
        "overlay_on_the_closing_total": _overlay_on_vegas(sample),
    }
