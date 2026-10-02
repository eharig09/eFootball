"""A deliberately lean NFL model: quarterback, success rates, and weather / penalty / turnover adjustments.

Inputs and nothing else: the starting quarterback, offensive and defensive success rate, and adjustments for
turnovers, penalties and weather. No drive model, no EPA chain, no Elo, no recent margin, no market.

The anchor is a matchup blend. A team's expected number in a game is the average of ITS OWN OFFENSE and THE
OPPONENT'S DEFENSE -- never its own side alone:

    expected success, home  = (home offense success   + away defense success allowed) / 2
    expected giveaways, home = (home giveaway rate     + away takeaway rate)            / 2
    expected penalty yards  = (home flags committed    + away flags drawn)              / 2

Margin and total features are the home-minus-away (margin) or home-plus-away (total) of those blends. Models are
fitted in stages so each adjustment's contribution is visible:

    anchor        blended success rate alone
    + quarterback shrunk QB rating, QB-change, experience
    + turnovers   blended giveaway/takeaway rate
    + penalties   blended penalty yards
    + weather     (totals) wind, gust, cold, precipitation, dome        = the lean model

Everything is a pregame snapshot built from earlier week batches (season-decayed, shrunk to the league mean).
Vegas is scored as a forecast and as a pick reference, never used as an input.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import math
from pathlib import Path
from typing import Any

import numpy as np

from sports_aggregator.nfl.context_ablation import _load_context
from sports_aggregator.nfl.drive_projection import STATE_SEASON_DECAY
from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict as _predict_margin
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, _shrunk_states, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import TOTAL_FEATURES, _fit_ridge, _predict
from sports_aggregator.nfl.weather_total_ablation import WEATHER, load_weather

MODEL_VERSION = "nfl-lean-model-v1"
PRIOR_GAMES = 3.0

QB_MARGIN = ("sq_epa_diff", "sq_change_diff", "sq_drop_diff", "sq_exp_diff")
QB_TOTAL = ("sq_epa_sum", "sq_exp_sum")

MARGIN_STAGES = (
    ("anchor", ("bl_succ_diff",)),
    ("anchor_qb", ("bl_succ_diff",) + QB_MARGIN),
    ("anchor_qb_turnovers", ("bl_succ_diff", "bl_to_diff") + QB_MARGIN),
    ("lean", ("bl_succ_diff", "bl_to_diff", "bl_pen_diff") + QB_MARGIN),
)
TOTAL_STAGES = (
    ("anchor", ("bl_succ_sum",)),
    ("anchor_qb", ("bl_succ_sum",) + QB_TOTAL),
    ("anchor_qb_turnovers", ("bl_succ_sum", "bl_to_sum") + QB_TOTAL),
    ("anchor_qb_to_penalties", ("bl_succ_sum", "bl_to_sum", "bl_pen_sum") + QB_TOTAL),
    ("lean", ("bl_succ_sum", "bl_to_sum", "bl_pen_sum") + QB_TOTAL + WEATHER),
    ("lean_plus_pace", ("bl_succ_sum", "bl_to_sum", "bl_pen_sum", "bl_plays_sum") + QB_TOTAL + WEATHER),
)


# ------------------------------------------------------------------------------------------ team-game records
def _records(repository: NFLRepository, start: int, end: int, cache: Path) -> list[dict[str, Any]]:
    """One record per (game, team): own offense numbers plus the numbers the opponent's offense posted against it."""
    with closing(repository._connect()) as connection:
        eff = {(str(r["game_id"]), canon_team(r["team"])): dict(r) for r in connection.execute(
            """SELECT game_id,season,week,team,opponent_team,plays,successful_plays FROM game_team_efficiency
               WHERE season BETWEEN ? AND ? AND plays>0""", (int(start), int(end)))}
    ctx = {(str(r["game_id"]), canon_team(r["team"])): r for r in _load_context(cache, start, end)}
    out = []
    for (gid, team), e in eff.items():
        opp = canon_team(e["opponent_team"])
        o_eff, c, o_c = eff.get((gid, opp)), ctx.get((gid, team)), ctx.get((gid, opp))
        if not o_eff or not c or not o_c:
            continue
        out.append({
            "game_id": gid, "team": team, "opponent": opp, "season": int(e["season"]), "week": int(e["week"]),
            "plays": float(e["plays"]), "succ": float(e["successful_plays"]),
            "opp_plays": float(o_eff["plays"]), "opp_succ": float(o_eff["successful_plays"]),
            "giveaways": c["giveaways"], "off_snaps": c["off_plays"],
            "takeaways": c["takeaways"], "def_snaps": c["def_plays"],
            "pen_committed": c["penalty_yards"], "pen_drawn": o_c["penalty_yards"],
        })
    return out


def _rates(records: list[dict[str, Any]], season: int, league: dict[str, float]) -> dict[str, float]:
    """Season-decayed rates for one team with PRIOR_GAMES of league-average play mixed in (sums over sums)."""
    keys = ("succ", "plays", "opp_succ", "opp_plays", "giveaways", "off_snaps", "takeaways", "def_snaps",
            "pen_committed", "pen_drawn", "games")
    acc = dict.fromkeys(keys, 0.0)
    for r in records:
        w = STATE_SEASON_DECAY ** max(0, season - r["season"])
        for k in keys[:-1]:
            acc[k] += w * r[k]
        acc["games"] += w
    for k in keys:
        acc[k] += PRIOR_GAMES * league[k]
    return {
        "off_succ": acc["succ"] / acc["plays"], "def_succ": acc["opp_succ"] / acc["opp_plays"],
        "giveaway": acc["giveaways"] / acc["off_snaps"], "takeaway": acc["takeaways"] / acc["def_snaps"],
        "pen_committed": acc["pen_committed"] / acc["games"], "pen_drawn": acc["pen_drawn"] / acc["games"],
        "plays_pg": acc["plays"] / acc["games"], "plays_allowed_pg": acc["opp_plays"] / acc["games"],
    }


def blended_features(repository: NFLRepository, start: int, end: int,
                     cache: str | Path = "instance/nflverse_raw") -> dict[str, dict[str, float]]:
    """game_id -> matchup-blend features (own offense averaged with the opponent's defense), whole-week batches."""
    records = _records(repository, max(2009, start - 2), end, Path(cache))
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_week[(r["season"], r["week"])].append(r)
    keys = ("succ", "plays", "opp_succ", "opp_plays", "giveaways", "off_snaps", "takeaways", "def_snaps",
            "pen_committed", "pen_drawn")
    totals = dict.fromkeys(keys, 0.0)
    count = 0
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    out: dict[str, dict[str, float]] = {}
    for season, week in sorted(by_week):
        batch = by_week[(season, week)]
        league = {k: (totals[k] / count if count else {"succ": 27.0, "plays": 62.0, "opp_succ": 27.0, "opp_plays": 62.0,
                                                       "giveaways": 1.3, "off_snaps": 62.0, "takeaways": 1.3,
                                                       "def_snaps": 62.0, "pen_committed": 52.0, "pen_drawn": 52.0}[k])
                  for k in keys}
        league["games"] = 1.0
        snapshots: dict[str, dict[str, float]] = {}
        for r in batch:
            for team in (r["team"], r["opponent"]):
                if team not in snapshots and history[team]:
                    snapshots[team] = _rates(history[team], season, league)
        pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for r in batch:
            pair[r["game_id"]][r["team"]] = r
        for gid, sides in pair.items():
            if len(sides) != 2:
                continue
            (t1, _), (t2, _) = list(sides.items())
            if t1 not in snapshots or t2 not in snapshots or season < start:
                continue
            out[gid] = {"_teams": (t1, t2), **{t: snapshots[t] for t in (t1, t2)}}
        for r in batch:
            history[r["team"]].append(r)
            for k in keys:
                totals[k] += r[k]
            count += 1
    return out


def _attach(rows: list[dict[str, Any]], blends: dict[str, dict[str, float]], states, weather) -> None:
    for r in rows:
        b = blends.get(str(r["game_id"]))
        if b is None:
            continue
        home, away = canon_team(r["home_team"]), canon_team(r["away_team"])
        if home not in b or away not in b:
            continue
        h, a = b[home], b[away]
        # Each side's expectation is its own offense averaged with the OPPONENT's defense.
        succ_h, succ_a = (h["off_succ"] + a["def_succ"]) / 2, (a["off_succ"] + h["def_succ"]) / 2
        to_h, to_a = (h["giveaway"] + a["takeaway"]) / 2, (a["giveaway"] + h["takeaway"]) / 2
        pen_h, pen_a = (h["pen_committed"] + a["pen_drawn"]) / 2, (a["pen_committed"] + h["pen_drawn"]) / 2
        plays_h, plays_a = (h["plays_pg"] + a["plays_allowed_pg"]) / 2, (a["plays_pg"] + h["plays_allowed_pg"]) / 2
        r.update({
            "bl_succ_h": succ_h, "bl_succ_a": succ_a, "bl_succ_diff": succ_h - succ_a, "bl_succ_sum": succ_h + succ_a,
            "bl_to_diff": to_a - to_h, "bl_to_sum": to_h + to_a,           # diff: positive = away turns it over more
            "bl_pen_diff": pen_a - pen_h, "bl_pen_sum": pen_h + pen_a,     # diff: positive = away loses more yards
            "bl_plays_sum": plays_h + plays_a,
        })
        hs, as_ = states.get((str(r["game_id"]), r["home_team"])), states.get((str(r["game_id"]), r["away_team"]))
        if hs and as_:
            r["sq_epa_sum"], r["sq_exp_sum"] = hs["epa"] + as_["epa"], hs["exp"] + as_["exp"]
        r.update(weather.get(str(r["game_id"]), {}))


# ------------------------------------------------------------------------------------------ evaluation
def _walk(sample, stages, fit, predict, alt=None):
    """Walk-forward by season. `alt` maps game_id -> substitute features; those rows also get pred_<stage>__alt."""
    pooled, folds = [], []
    for season in sorted({int(r["season"]) for r in sample}):
        train = [r for r in sample if int(r["season"]) < season]
        test = [dict(r) for r in sample if int(r["season"]) == season]
        models = {label: fit(train, features) for label, features in stages}
        if any(m is None for m in models.values()) or not test:
            continue
        for r in test:
            for label, model in models.items():
                r[f"pred_{label}"] = predict(model, r)
                if alt and str(r["game_id"]) in alt:
                    r[f"pred_{label}__alt"] = predict(model, {**r, **alt[str(r["game_id"])]})
        pooled.extend(test)
        folds.append(test)
    return pooled, folds


def _errors(rows, key, target):
    return np.asarray([float(r[key]) - float(r[target]) for r in rows])


def _score(rows, key, target) -> dict[str, Any]:
    e = _errors(rows, key, target)
    return {"mae": round(float(np.abs(e).mean()), 4), "rmse": round(float(math.sqrt((e * e).mean())), 4),
            "bias": round(float(e.mean()), 4)}


def _paired(rows, a, b, target) -> dict[str, Any]:
    """Mean |err| of b minus a (negative = b better) and t."""
    d = np.abs(_errors(rows, b, target)) - np.abs(_errors(rows, a, target))
    se = d.std(ddof=1) / math.sqrt(len(d))
    return {"mean_abs_err_diff": round(float(d.mean()), 4), "t": round(float(d.mean() / se), 2) if se else None}


def _picks(rows, pred_key, line_key, target, min_edge=0.0) -> dict[str, Any]:
    """Backing the model's side of the line: home/over when the model is above it, away/under below. Pushes dropped."""
    n = wins = 0
    for r in rows:
        line, pred = r.get(line_key), r.get(pred_key)
        if line is None or pred is None or abs(pred - line) < min_edge or r[target] == line:
            continue
        n += 1
        wins += (pred > line) == (r[target] > line)
    if not n:
        return {"n": 0}
    return {"n": n, "win_rate": round(wins / n, 4), "z_vs_50": round((wins - n / 2) / math.sqrt(n / 4), 2),
            "units_at_-110": round(wins - 1.1 * (n - wins), 1)}


def _su(rows, key) -> dict[str, Any]:
    played = [r for r in rows if r["actual_margin"] != 0 and r.get(key) not in (None, 0)]
    hits = sum((r[key] > 0) == (r["actual_margin"] > 0) for r in played)
    return {"n": len(played), "accuracy": round(hits / len(played), 4)}


def _side_split(rows, pred_key, line_key, target) -> dict[str, Any]:
    """Over-picks and under-picks scored separately, against how often unders simply win."""
    decided = [r for r in rows if r.get(pred_key) is not None and r[target] != r[line_key]]
    base_under = sum(r[target] < r[line_key] for r in decided) / len(decided)
    out: dict[str, Any] = {"base_rate_under": round(base_under, 4), "games": len(decided)}
    for side, test in (("over", lambda r: r[pred_key] > r[line_key]), ("under", lambda r: r[pred_key] < r[line_key])):
        picked = [r for r in decided if test(r)]
        hits = sum((r[target] > r[line_key]) == (side == "over") for r in picked)
        out[f"{side}_picks"] = {"n": len(picked), "win_rate": round(hits / len(picked), 4) if picked else None}
    return out


def _by_season(rows, pred_key, line_key, target) -> dict[str, Any]:
    result = {}
    for season in sorted({int(r["season"]) for r in rows}):
        stat = _picks([r for r in rows if int(r["season"]) == season], pred_key, line_key, target)
        result[season] = stat.get("win_rate")
    above = sum(v is not None and v > 0.5 for v in result.values())
    return {"win_rate_by_season": result, "seasons_above_50": f"{above}/{len(result)}"}


def _forecast_block(sub) -> dict[str, Any]:
    """Lean total scored with the weather forecast issued a day ahead instead of the realised weather."""
    if not sub:
        return {"n": 0}
    return {
        "n": len(sub), "seasons": sorted({int(r["season"]) for r in sub}),
        "lean_observed_weather": _picks(sub, "pred_lean", "market_total", "actual_total"),
        "lean_issued_forecast_weather": _picks(sub, "pred_lean__alt", "market_total", "actual_total"),
        "without_weather": _picks(sub, "pred_anchor_qb_to_penalties", "market_total", "actual_total"),
        "core_total": _picks(sub, "pred_core_total", "market_total", "actual_total"),
        "mae": {"observed_weather": _score(sub, "pred_lean", "actual_total")["mae"],
                "forecast_weather": _score(sub, "pred_lean__alt", "actual_total")["mae"],
                "without_weather": _score(sub, "pred_anchor_qb_to_penalties", "actual_total")["mae"],
                "core_total": _score(sub, "pred_core_total", "actual_total")["mae"],
                "vegas": _score(sub, "market_total", "actual_total")["mae"]},
    }


def _market(repository: NFLRepository) -> dict[str, tuple[float | None, float | None]]:
    with closing(repository._connect()) as connection:
        return {str(r["game_id"]): (r["spread_line"], r["total_line"]) for r in connection.execute(
            "SELECT game_id,spread_line,total_line FROM games")}


def report(repository: NFLRepository, *, start_season=2013, end_season=2025,
           cache: str | Path = "instance/nflverse_raw"):
    rows = build_rows(repository, start_season, end_season)
    blends = blended_features(repository, start_season, end_season, cache)
    states = _shrunk_states(repository, start_season, end_season)
    observed = load_weather(repository)[0]
    _attach(rows, blends, states, observed)
    market = _market(repository)
    for r in rows:
        r["market_margin"], r["market_total"] = market.get(str(r["game_id"]), (None, None))

    # ---------------------------------------------------------------- margin
    base_margin = ("qb_stack", SHRUNK_CHANGE)
    stages_m = (base_margin,) + MARGIN_STAGES
    need = set().union(*(set(f) for _, f in stages_m))
    msample = [r for r in rows if r.get("actual_margin") is not None and r.get("market_margin") is not None
               and all(r.get(k) is not None for k in need)]
    mpool, mfolds = _walk(msample, stages_m, _fit, _predict_margin)
    labels_m = [l for l, _ in stages_m]
    margin = {
        "n": len(mpool),
        "pooled": {**{l: _score(mpool, f"pred_{l}", "actual_margin") for l in labels_m},
                   "vegas": _score(mpool, "market_margin", "actual_margin")},
        "straight_up": {**{l: _su(mpool, f"pred_{l}") for l in labels_m}, "vegas": _su(mpool, "market_margin")},
        "lean_vs_vegas": _paired(mpool, "market_margin", "pred_lean", "actual_margin"),
        "lean_vs_qb_stack": _paired(mpool, "pred_qb_stack", "pred_lean", "actual_margin"),
        "stage_paired_vs_previous": {
            b: _paired(mpool, f"pred_{a}", f"pred_{b}", "actual_margin")
            for a, b in zip([l for l, _ in MARGIN_STAGES], [l for l, _ in MARGIN_STAGES][1:])},
        "ats_picks_all": {l: _picks(mpool, f"pred_{l}", "market_margin", "actual_margin") for l in ("lean", "qb_stack")},
        "ats_picks_edge_3plus": {l: _picks(mpool, f"pred_{l}", "market_margin", "actual_margin", 3.0) for l in ("lean", "qb_stack")},
        "seasons_lean_beats_qb_stack": f"{sum(_score(f, 'pred_lean', 'actual_margin')['mae'] < _score(f, 'pred_qb_stack', 'actual_margin')['mae'] for f in mfolds)}/{len(mfolds)}",
        "seasons_lean_beats_vegas": f"{sum(_score(f, 'pred_lean', 'actual_margin')['mae'] < _score(f, 'market_margin', 'actual_margin')['mae'] for f in mfolds)}/{len(mfolds)}",
    }

    # ---------------------------------------------------------------- total
    base_total = ("core_total", TOTAL_FEATURES)
    stages_t = (base_total,) + TOTAL_STAGES
    need_t = set().union(*(set(f) for _, f in stages_t))
    tsample = [r for r in rows if r.get("actual_total") is not None and r.get("market_total") is not None
               and all(r.get(k) is not None for k in need_t)]
    forecast_weather = load_weather(repository)[1]
    alt = {gid: feats for gid, feats in forecast_weather[1].items()}      # forecast issued ~1 day ahead (wind: 2024+)
    tpool, tfolds = _walk(tsample, stages_t, lambda t, f: _fit_ridge(t, f, "actual_total"), _predict, alt=alt)
    labels_t = [l for l, _ in stages_t]
    total = {
        "n": len(tpool),
        "pooled": {**{l: _score(tpool, f"pred_{l}", "actual_total") for l in labels_t},
                   "vegas": _score(tpool, "market_total", "actual_total")},
        "lean_vs_vegas": _paired(tpool, "market_total", "pred_lean", "actual_total"),
        "lean_vs_core_total": _paired(tpool, "pred_core_total", "pred_lean", "actual_total"),
        "stage_paired_vs_previous": {
            b: _paired(tpool, f"pred_{a}", f"pred_{b}", "actual_total")
            for a, b in zip([l for l, _ in TOTAL_STAGES], [l for l, _ in TOTAL_STAGES][1:])},
        "over_under_picks_all": {l: _picks(tpool, f"pred_{l}", "market_total", "actual_total")
                                 for l in ("lean", "lean_plus_pace", "core_total")},
        "over_under_picks_edge_3plus": {l: _picks(tpool, f"pred_{l}", "market_total", "actual_total", 3.0)
                                        for l in ("lean", "lean_plus_pace", "core_total")},
        "diagnostics": {
            "side_split_lean": _side_split(tpool, "pred_lean", "market_total", "actual_total"),
            "side_split_without_weather": _side_split(tpool, "pred_anchor_qb_to_penalties", "market_total", "actual_total"),
            "side_split_core_total": _side_split(tpool, "pred_core_total", "market_total", "actual_total"),
            "by_season_lean": _by_season(tpool, "pred_lean", "market_total", "actual_total"),
            "by_season_without_weather": _by_season(tpool, "pred_anchor_qb_to_penalties", "market_total", "actual_total"),
            "without_weather_picks_all": _picks(tpool, "pred_anchor_qb_to_penalties", "market_total", "actual_total"),
            "without_weather_picks_edge_3plus": _picks(tpool, "pred_anchor_qb_to_penalties", "market_total", "actual_total", 3.0),
            "mean_prediction_vs_line": {l: round(float(np.mean([r[f"pred_{l}"] - r["market_total"] for r in tpool])), 3)
                                        for l in ("lean", "anchor_qb_to_penalties", "core_total")},
        },
        "issued_forecast_weather_check": {
            "all_games_with_a_forecast": _forecast_block([r for r in tpool if r.get("pred_lean__alt") is not None]),
            # the case that matters: outdoor games, where weather is genuinely uncertain and wind forecasts exist
            "outdoor_games_2024_plus": _forecast_block([r for r in tpool if r.get("pred_lean__alt") is not None
                                                        and not r.get("wx_dome") and int(r["season"]) >= 2024]),
        },
        "seasons_lean_beats_core_total": f"{sum(_score(f, 'pred_lean', 'actual_total')['mae'] < _score(f, 'pred_core_total', 'actual_total')['mae'] for f in tfolds)}/{len(tfolds)}",
        "seasons_lean_beats_vegas": f"{sum(_score(f, 'pred_lean', 'actual_total')['mae'] < _score(f, 'market_total', 'actual_total')['mae'] for f in tfolds)}/{len(tfolds)}",
    }
    return {"version": MODEL_VERSION, "market_used": False,
            "blend": "each side's expectation = mean(own offense, opponent defense)",
            "margin": margin, "total": total}
