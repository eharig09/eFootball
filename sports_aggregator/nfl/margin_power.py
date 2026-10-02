"""NFL Margin Power: an SRS (simple rating system) on opponent-adjusted scoreboard margins.

The college football engine's "Margin Power" is an iterative SRS on home-field-neutralised margins, re-solved every week
from prior weeks only. The NFL had no equivalent. This is the same construction, in two forms:

  within   exactly the college version: this season's earlier games only; a team needs MIN_GAMES before it is rated.
           In a 17-game season that leaves the first few weeks unrated.
  carry    available from week 1: last season's final SRS, shrunk by PRIOR_SHRINK toward average, enters as PRIOR_K
           pseudo-games that fade as real games arrive.

A rating of +4 means the team is four points better than average on a neutral field; the forecast margin for a game is
home rating - away rating + home field. Nothing here uses the market; the closing line is a benchmark and the thing the
"edge" is measured against.

The report asks three questions: how good is it alone, does it add to the lean model / QB-aware stack, and does the gap
between it and the closing line (the way the college engine uses it) predict against-the-spread results?
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import math
from pathlib import Path
from typing import Any

import numpy as np

from sports_aggregator.nfl.lean_model import QB_MARGIN, _attach, _market, _paired, _score, _su, _walk, blended_features
from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict as _predict_margin
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, _shrunk_states, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.weather_total_ablation import load_weather

MODEL_VERSION = "nfl-margin-power-v1"
HOME_FIELD = 2.0
ITERATIONS = 50
MIN_GAMES = 3
PRIOR_K = 4.0
PRIOR_SHRINK = 0.5


def _neutral_edges(games: list[dict[str, Any]]) -> dict[str, list[tuple[str, float]]]:
    team_games: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for g in games:
        neutral = float(g["home_score"]) - float(g["away_score"]) - HOME_FIELD
        team_games[g["home_team"]].append((g["away_team"], neutral))
        team_games[g["away_team"]].append((g["home_team"], -neutral))
    return team_games


def solve_srs(games: list[dict[str, Any]], prior: dict[str, float] | None = None,
              prior_k: float = 0.0) -> tuple[dict[str, float], dict[str, int]]:
    """Exact SRS: solve (D + K*I - A) r = b rather than iterating.

    A team's rating is the mean of (its neutral margin + its opponent's rating) over its games, with `prior_k`
    pseudo-games at its prior rating when a prior is given. Solved directly because the usual averaging iteration
    oscillates on the sparse graphs an early season produces (two teams that only played each other come out tied at
    zero after an even number of passes). Ratings are centred on zero.
    """
    team_games = _neutral_edges(games)
    teams = sorted(set(team_games) | set(prior or {}))
    if not teams:
        return {}, {}
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    matrix = np.zeros((n, n))
    rhs = np.zeros(n)
    k = prior_k if prior else 0.0
    for t in teams:                       # every team carries its prior pseudo-games, played yet or not
        i = index[t]
        matrix[i, i] += k
        rhs[i] += k * (prior or {}).get(t, 0.0)
    for t, history in team_games.items():
        i = index[t]
        matrix[i, i] += len(history)
        for opponent, margin in history:
            matrix[i, index[opponent]] -= 1.0
            rhs[i] += margin
    for t in teams:                       # a team with no games and no prior pins to average
        i = index[t]
        if matrix[i, i] == 0:
            matrix[i, i] = 1.0
    solution = np.linalg.lstsq(matrix, rhs, rcond=None)[0]
    solution -= solution.mean()
    return {t: float(solution[index[t]]) for t in teams}, {t: len(h) for t, h in team_games.items()}


def solve_srs_iterative(games: list[dict[str, Any]], prior: dict[str, float] | None = None,
                        prior_k: float = 0.0) -> tuple[dict[str, float], dict[str, int]]:
    """The college engine's method (50 averaging passes), kept only to measure how far it is from the exact solution."""
    team_games = _neutral_edges(games)
    teams = set(team_games) | set(prior or {})
    ratings = {t: 0.0 for t in teams}
    for _ in range(ITERATIONS):
        updated = {}
        for t in teams:
            history = team_games.get(t, [])
            weight = len(history) + (prior_k if prior else 0.0)
            if weight == 0:
                updated[t] = 0.0
                continue
            total = sum(m + ratings.get(o, 0.0) for o, m in history)
            if prior:
                total += prior_k * prior.get(t, 0.0)
            updated[t] = total / weight
        center = sum(updated.values()) / len(updated) if updated else 0.0
        ratings = {t: v - center for t, v in updated.items()}
    return ratings, {t: len(h) for t, h in team_games.items()}


def _games(repository: NFLRepository, start: int, end: int) -> list[dict[str, Any]]:
    with closing(repository._connect()) as connection:
        return [{**dict(r), "home_team": canon_team(r["home_team"]), "away_team": canon_team(r["away_team"])}
                for r in connection.execute(
                    """SELECT game_id,season,week,home_team,away_team,home_score,away_score FROM games
                       WHERE season BETWEEN ? AND ? AND season_type='REG' AND completed=1
                         AND home_score IS NOT NULL AND away_score IS NOT NULL
                       ORDER BY season,week,game_date,game_id""", (int(start), int(end)))]


def snapshots(repository: NFLRepository, start: int, end: int, mode: str = "carry",
              solver=None) -> dict[str, dict[str, Any]]:
    """game_id -> pregame ratings and forecast margin, re-solved each week from earlier weeks only."""
    solver = solver or solve_srs
    games = _games(repository, max(2009, start - 1), end)
    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for g in games:
        by_season[g["season"]].append(g)
    out: dict[str, dict[str, Any]] = {}
    previous_final: dict[str, float] = {}
    for season in sorted(by_season):
        season_games = by_season[season]
        prior = {t: PRIOR_SHRINK * v for t, v in previous_final.items()} if mode == "carry" and previous_final else None
        done: list[dict[str, Any]] = []
        for week in sorted({g["week"] for g in season_games}):
            current = [g for g in season_games if g["week"] == week]
            ratings, counts = solver(done, prior, PRIOR_K)
            if season >= start:
                for g in current:
                    h, a = g["home_team"], g["away_team"]
                    if mode == "within" and (counts.get(h, 0) < MIN_GAMES or counts.get(a, 0) < MIN_GAMES):
                        continue
                    if h not in ratings or a not in ratings:
                        continue
                    out[str(g["game_id"])] = {"home_rating": ratings[h], "away_rating": ratings[a],
                                              "srs_diff": ratings[h] - ratings[a],
                                              "srs_margin": ratings[h] - ratings[a] + HOME_FIELD,
                                              "games": min(counts.get(h, 0), counts.get(a, 0))}
            done.extend(current)
        final, _ = solver(season_games)
        previous_final = final
    return out


# ------------------------------------------------------------------------------------------ report
def _ats(rows, edge_key, line_key, target, floor=0.0) -> dict[str, Any]:
    """Back the side the rating favours against the closing line: home if the rating is higher than the line."""
    n = wins = 0
    for r in rows:
        edge = r.get(edge_key)
        if edge is None or abs(edge) < floor or r[target] == r[line_key]:
            continue
        n += 1
        wins += (edge > 0) == (r[target] > r[line_key])
    if not n:
        return {"n": 0}
    return {"n": n, "win_rate": round(wins / n, 4), "z_vs_50": round((wins - n / 2) / math.sqrt(n / 4), 2),
            "units_at_-110": round(wins - 1.1 * (n - wins), 1)}


def report(repository: NFLRepository, *, start_season=2013, end_season=2025, cache: str | Path = "instance/nflverse_raw"):
    rows = build_rows(repository, start_season, end_season)
    _attach(rows, blended_features(repository, start_season, end_season, cache),
            _shrunk_states(repository, start_season, end_season), load_weather(repository)[0])
    market = _market(repository)
    within, carry = snapshots(repository, start_season, end_season, "within"), snapshots(repository, start_season, end_season, "carry")
    within_cfb = snapshots(repository, start_season, end_season, "within", solve_srs_iterative)      # the college method
    for r in rows:
        gid = str(r["game_id"])
        r["market_margin"] = market.get(gid, (None, None))[0]
        for tag, snap in (("within", within), ("carry", carry), ("within_cfb", within_cfb)):
            s = snap.get(gid)
            if s:
                r[f"srs_{tag}_diff"], r[f"srs_{tag}_margin"] = s["srs_diff"], s["srs_margin"]

    lean = ("bl_succ_diff", "bl_to_diff", "bl_pen_diff") + QB_MARGIN
    out: dict[str, Any] = {"version": MODEL_VERSION, "market_used": False,
                           "settings": {"home_field": HOME_FIELD, "min_games_within": MIN_GAMES, "prior_k": PRIOR_K,
                                        "prior_shrink": PRIOR_SHRINK, "iterations": ITERATIONS}}

    # ---- 1. alone: the raw SRS forecast (no fitting at all) against the other models and the line, on common games
    stages = (("qb_stack", SHRUNK_CHANGE), ("lean", lean),
              ("srs_carry_fitted", ("srs_carry_diff",)), ("srs_within_fitted", ("srs_within_diff",)),
              ("lean_plus_srs", lean + ("srs_carry_diff",)), ("qb_stack_plus_srs", SHRUNK_CHANGE + ("srs_carry_diff",)),
              ("lean_plus_srs_within", lean + ("srs_within_diff",)))
    need = set().union(*(set(f) for _, f in stages))
    sample = [r for r in rows if r.get("actual_margin") is not None and r.get("market_margin") is not None
              and all(r.get(k) is not None for k in need)]
    pooled, folds = _walk(sample, stages, _fit, _predict_margin)
    labels = [l for l, _ in stages]
    for r in pooled:
        r["pred_srs_carry_raw"], r["pred_srs_within_raw"] = r["srs_carry_margin"], r["srs_within_margin"]
        r["pred_srs_within_cfb_raw"] = r.get("srs_within_cfb_margin", r["srs_within_margin"])
    all_labels = labels + ["srs_carry_raw", "srs_within_raw", "srs_within_cfb_raw"]
    out["common_games"] = {
        "n": len(pooled),
        "note": "within needs 3 prior games each, so these are games from about week 4 on in every season",
        "pooled": {**{l: _score(pooled, f"pred_{l}", "actual_margin") for l in all_labels},
                   "vegas": _score(pooled, "market_margin", "actual_margin")},
        "straight_up": {**{l: _su(pooled, f"pred_{l}")["accuracy"] for l in all_labels}, "vegas": _su(pooled, "market_margin")["accuracy"]},
        "paired_vs_qb_stack": {l: _paired(pooled, "pred_qb_stack", f"pred_{l}", "actual_margin") for l in all_labels if l != "qb_stack"},
        "paired_vs_lean": {l: _paired(pooled, "pred_lean", f"pred_{l}", "actual_margin") for l in ("lean_plus_srs", "lean_plus_srs_within", "srs_carry_raw", "srs_carry_fitted")},
        "paired_vs_vegas": {l: _paired(pooled, "market_margin", f"pred_{l}", "actual_margin") for l in all_labels},
        "seasons_lean_plus_srs_beats_lean": f"{sum(_score(f, 'pred_lean_plus_srs', 'actual_margin')['mae'] < _score(f, 'pred_lean', 'actual_margin')['mae'] for f in folds)}/{len(folds)}",
    }

    # ---- 2. carry version on every game (week 1 included), standalone
    carry_only = [r for r in rows if r.get("actual_margin") is not None and r.get("market_margin") is not None
                  and r.get("srs_carry_margin") is not None]
    out["carry_all_games"] = {"n": len(carry_only), "srs_carry_raw": _score(carry_only, "srs_carry_margin", "actual_margin"),
                              "vegas": _score(carry_only, "market_margin", "actual_margin"),
                              "week_1_to_3": _score([r for r in carry_only if int(r["week"]) <= 3], "srs_carry_margin", "actual_margin"),
                              "week_1_to_3_vegas": _score([r for r in carry_only if int(r["week"]) <= 3], "market_margin", "actual_margin")}

    # ---- 3. the college engine's use: SRS minus the closing line, as a standardised edge
    for tag in ("within", "carry"):
        edge_rows = [dict(r, edge=r[f"srs_{tag}_margin"] - r["market_margin"]) for r in rows
                     if r.get(f"srs_{tag}_margin") is not None and r.get("market_margin") is not None
                     and r.get("actual_margin") is not None]
        seasons = sorted({int(r["season"]) for r in edge_rows})
        for r in edge_rows:                      # standardise with the PREVIOUS seasons' spread of the edge (leak-safe)
            earlier = [x["edge"] for x in edge_rows if int(x["season"]) < int(r["season"])]
            r["edge_z"] = r["edge"] / float(np.std(earlier)) if len(earlier) > 300 else None
        scored = [r for r in edge_rows if r.get("edge_z") is not None]
        mid = seasons[len(seasons) // 2]
        out[f"edge_vs_closing_line_{tag}"] = {
            "games": len(scored), "edge_sd_points": round(float(np.std([r["edge"] for r in edge_rows])), 2),
            "all_games": _ats(scored, "edge_z", "market_margin", "actual_margin"),
            "abs_z_ge_0.5": _ats(scored, "edge_z", "market_margin", "actual_margin", 0.5),
            "abs_z_ge_1.0": _ats(scored, "edge_z", "market_margin", "actual_margin", 1.0),
            "abs_z_ge_1.5": _ats(scored, "edge_z", "market_margin", "actual_margin", 1.5),
            "z_ge_1_first_half": _ats([r for r in scored if int(r["season"]) < mid], "edge_z", "market_margin", "actual_margin", 1.0),
            "z_ge_1_second_half": _ats([r for r in scored if int(r["season"]) >= mid], "edge_z", "market_margin", "actual_margin", 1.0),
            "second_half_starts": mid}
    return out
