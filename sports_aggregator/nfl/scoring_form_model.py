"""Season offense/defense plus last-three scoring form, as an independent NFL margin and total model.

The idea under test: replace (or add to) the drive/efficiency chain with the simplest transparent rating --
points scored and allowed -- built two ways for every team before every game:

  season   decayed points for / against per game (current season at weight 1, each earlier season at
           STATE_SEASON_DECAY**seasons_ago, shrunk toward the league mean by a few games' worth of prior)
  form     the plain mean of the last three games' points for / against (league mean fills a missing game)

An optional one-pass opponent adjustment subtracts how generous each opponent's defense (or how stingy its offense)
had been at the time. Ratings for a game use only earlier week batches. No market inputs: the closing line appears
only as a benchmark in the report.

Compared on one common set of games with: the QB-aware stack (the live margin model) and its total counterpart.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import math
from typing import Any

import numpy as np

from sports_aggregator.nfl.drive_projection import STATE_SEASON_DECAY
from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, _paired, _summary, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import TOTAL_FEATURES, _fit_ridge, _predict
from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict as _predict_margin

MODEL_VERSION = "nfl-scoring-form-v1"
PRIOR_GAMES = 3.0          # games of league-average points mixed into every season rating
FORM_GAMES = 3

MARGIN_SF = ("sf_margin_season", "sf_margin_form")
TOTAL_SF = ("sf_total_season", "sf_total_form")
MARGIN_ADJ = ("sf_margin_season_adj", "sf_margin_form_adj")
TOTAL_ADJ = ("sf_total_season_adj", "sf_total_form_adj")


def _games(repository: NFLRepository, start: int, end: int) -> list[dict[str, Any]]:
    with closing(repository._connect()) as connection:
        return [dict(r) for r in connection.execute(
            """SELECT game_id,season,week,home_team,away_team,home_score,away_score FROM games
               WHERE season BETWEEN ? AND ? AND season_type='REG' AND completed=1
                 AND home_score IS NOT NULL AND away_score IS NOT NULL
               ORDER BY season,week,game_date,game_id""", (int(start), int(end)))]


def _season_rating(records: list[dict[str, Any]], season: int, league: float, adjust: bool) -> dict[str, float]:
    """Decayed (for, against) per game with a league-mean prior; optionally opponent-adjusted."""
    wf = wa = w = 0.0
    for r in records:
        weight = STATE_SEASON_DECAY ** max(0, season - r["season"])
        pf = r["for_adj"] if adjust else r["for"]
        pa = r["against_adj"] if adjust else r["against"]
        wf += weight * pf
        wa += weight * pa
        w += weight
    return {"off": (wf + PRIOR_GAMES * league) / (w + PRIOR_GAMES),
            "def": (wa + PRIOR_GAMES * league) / (w + PRIOR_GAMES)}


def _form_rating(records: list[dict[str, Any]], league: float, adjust: bool) -> dict[str, float]:
    recent = records[-FORM_GAMES:]
    pf = [r["for_adj"] if adjust else r["for"] for r in recent]
    pa = [r["against_adj"] if adjust else r["against"] for r in recent]
    missing = FORM_GAMES - len(recent)
    return {"off": (sum(pf) + missing * league) / FORM_GAMES, "def": (sum(pa) + missing * league) / FORM_GAMES}


def pregame_features(repository: NFLRepository, start: int, end: int) -> dict[str, dict[str, float]]:
    """game_id -> scoring-form features, built week batch by week batch."""
    games = _games(repository, max(2009, start - 3), end)
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for g in games:
        by_week[(g["season"], g["week"])].append(g)
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    league_points = [23.0]
    out: dict[str, dict[str, float]] = {}

    for season, week in sorted(by_week):
        batch = by_week[(season, week)]
        league = float(np.mean(league_points[-2000:]))
        snapshots: dict[str, dict[str, Any]] = {}
        for g in batch:
            for team in (g["home_team"], g["away_team"]):
                if team in snapshots:
                    continue
                recs = history[team]
                snapshots[team] = {
                    "season": {adj: _season_rating(recs, season, league, adj) for adj in (False, True)},
                    "form": {adj: _form_rating(recs, league, adj) for adj in (False, True)},
                    "n": len(recs),
                }
        for g in batch:
            h, a = snapshots[g["home_team"]], snapshots[g["away_team"]]
            if season >= start and h["n"] and a["n"]:
                feats: dict[str, float] = {}
                for tag, adj in (("", False), ("_adj", True)):
                    for kind in ("season", "form"):
                        exp_home = h[kind][adj]["off"] + a[kind][adj]["def"] - league
                        exp_away = a[kind][adj]["off"] + h[kind][adj]["def"] - league
                        feats[f"sf_margin_{kind}{tag}"] = exp_home - exp_away
                        feats[f"sf_total_{kind}{tag}"] = exp_home + exp_away
                out[str(g["game_id"])] = feats
        # update after the whole batch so no game informs another in its own week
        for g in batch:
            for team, opp, pf, pa in ((g["home_team"], g["away_team"], g["home_score"], g["away_score"]),
                                      (g["away_team"], g["home_team"], g["away_score"], g["home_score"])):
                opp_snapshot = snapshots[opp]["season"][False]
                history[team].append({
                    "season": season, "for": float(pf), "against": float(pa),
                    # the opponent's pregame strength: how many points it normally concedes / scores
                    "for_adj": float(pf) - (opp_snapshot["def"] - league),
                    "against_adj": float(pa) - (opp_snapshot["off"] - league),
                })
            league_points.extend([float(g["home_score"]), float(g["away_score"])])
    return out


def _walk(sample, sets, target, fit, predict):
    pooled, folds = [], []
    for season in sorted({int(r["season"]) for r in sample}):
        train = [r for r in sample if int(r["season"]) < season]
        test = [dict(r) for r in sample if int(r["season"]) == season]
        models = {label: fit(train, features) for label, features in sets}
        if any(m is None for m in models.values()) or not test:
            continue
        for r in test:
            for label, model in models.items():
                r[f"pred_{label}"] = predict(model, r)
        pooled.extend(test)
        folds.append(test)
    return pooled, folds


def _block(pooled, folds, labels, target):
    def mae(rows, key):
        return float(np.mean([abs(r[key] - r[target]) for r in rows]))
    shaped = lambda rows: [dict(r, actual_margin=r[target]) for r in rows]   # noqa: E731  (reuse the paired helper)
    out = {"n": len(pooled), "pooled": {}, "paired_vs_qb_stack": {}, "seasons_better_than_qb_stack": {}}
    for label in labels:
        out["pooled"][label] = _summary(shaped(pooled), f"pred_{label}")
        if label == labels[0]:
            continue
        out["paired_vs_qb_stack"][label] = _paired(shaped(pooled), f"pred_{labels[0]}", f"pred_{label}")
        out["seasons_better_than_qb_stack"][label] = (
            f"{sum(mae(f, f'pred_{label}') < mae(f, f'pred_{labels[0]}') for f in folds)}/{len(folds)}")
    return out


def _straight_up(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """How often the sign of a margin forecast names the winner (ties excluded)."""
    played = [r for r in rows if r["actual_margin"] != 0 and r.get(key) not in (None, 0)]
    hits = sum((r[key] > 0) == (r["actual_margin"] > 0) for r in played)
    return {"n": len(played), "accuracy": round(hits / len(played), 4) if played else None}


def _market(repository: NFLRepository) -> dict[str, tuple[float | None, float | None]]:
    with closing(repository._connect()) as connection:
        return {str(r["game_id"]): (r["spread_line"], r["total_line"]) for r in connection.execute(
            "SELECT game_id,spread_line,total_line FROM games")}


def report(repository: NFLRepository, *, start_season=2013, end_season=2025):
    rows = build_rows(repository, start_season, end_season)
    feats = pregame_features(repository, start_season, end_season)
    market = _market(repository)
    for r in rows:
        r.update(feats.get(str(r["game_id"]), {}))
        spread, total = market.get(str(r["game_id"]), (None, None))
        r["market_margin"], r["market_total"] = spread, total

    # ---- margin
    sets_margin = (
        ("qb_stack", SHRUNK_CHANGE),
        ("scoring_form_only", MARGIN_SF),
        ("scoring_form_adj_only", MARGIN_ADJ),
        ("qb_stack_plus_sf", SHRUNK_CHANGE + MARGIN_SF),
        ("qb_stack_plus_sf_adj", SHRUNK_CHANGE + MARGIN_ADJ),
    )
    needed = set().union(*(set(f) for _, f in sets_margin))
    msample = [r for r in rows if r.get("actual_margin") is not None and all(r.get(k) is not None for k in needed)]
    mpool, mfolds = _walk(msample, sets_margin, "actual_margin", _fit, _predict_margin)
    labels = [l for l, _ in sets_margin]
    margin = _block(mpool, mfolds, labels, "actual_margin")
    bench = [r for r in mpool if r.get("market_margin") is not None]
    margin["market_benchmark"] = _summary(
        [dict(r, actual_margin=r["actual_margin"], pred_market=r["market_margin"]) for r in bench], "pred_market")
    shaped_bench = [dict(r, pred_market=r["market_margin"]) for r in bench]
    margin["straight_up_accuracy"] = {
        **{label: _straight_up(bench, f"pred_{label}") for label in labels},
        "market": _straight_up(shaped_bench, "pred_market"),
    }

    # ---- total
    sets_total = (
        ("core_total", TOTAL_FEATURES),
        ("scoring_form_only", TOTAL_SF),
        ("scoring_form_adj_only", TOTAL_ADJ),
        ("core_plus_sf", TOTAL_FEATURES + TOTAL_SF),
        ("core_plus_sf_adj", TOTAL_FEATURES + TOTAL_ADJ),
    )
    needed_t = set().union(*(set(f) for _, f in sets_total))
    tsample = [r for r in rows if r.get("actual_total") is not None and all(r.get(k) is not None for k in needed_t)]
    tpool, tfolds = _walk(tsample, sets_total, "actual_total",
                          lambda train, f: _fit_ridge(train, f, "actual_total"), _predict)
    tlabels = [l for l, _ in sets_total]
    total = _block(tpool, tfolds, tlabels, "actual_total")
    tbench = [r for r in tpool if r.get("market_total") is not None]
    total["market_benchmark"] = _summary(
        [dict(r, actual_margin=r["actual_total"], pred_market=r["market_total"]) for r in tbench], "pred_market")

    return {"version": MODEL_VERSION, "market_used": False,
            "settings": {"prior_games": PRIOR_GAMES, "form_games": FORM_GAMES, "season_decay": STATE_SEASON_DECAY},
            "margin": margin, "total": total}
