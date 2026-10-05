"""Fit and validate the committee-ranking model used by the playoff simulator.

The committee does not publish a formula, so we learn one: regress each team's
final committee position (the ranking released after conference championship
games) on resume and strength features computed by the same code the simulator
uses. Training data is every final committee ranking in the database; accuracy is
measured leave-one-season-out, which is the honest number to quote.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from sports_aggregator.cfb import playoff_rules as rules
from sports_aggregator.cfb.playoff_sim import FEATURES, CommitteeModel, Prepared
from sports_aggregator.cfb.playoff_state import RatingParams, load_state

POLL = "Playoff Committee Rankings"
RIDGE = 30.0
UNRANKED_POOL = 45   # candidates per season: ranked teams + best-rated others up to this many


def committee_seasons(repository) -> list[int]:
    with repository._reader() as connection:
        return [int(r["season"]) for r in connection.execute(
            "SELECT DISTINCT season FROM rankings WHERE poll=? ORDER BY season", (POLL,))]


def final_ranking(repository, season: int) -> list[str]:
    """Teams in the last committee ranking of `season`, best first."""
    with repository._reader() as connection:
        week = connection.execute(
            "SELECT MAX(week) w FROM rankings WHERE poll=? AND season=? AND season_type='regular'",
            (POLL, int(season))).fetchone()["w"]
        rows = connection.execute(
            """SELECT school FROM rankings WHERE poll=? AND season=? AND season_type='regular'
               AND week=? ORDER BY rank""", (POLL, int(season), week)).fetchall()
    return [r["school"] for r in rows]


def season_dataset(repository, season: int, params: RatingParams | None = None) -> dict[str, Any]:
    state = load_state(repository, season, through_championships=True)
    prep = Prepared(state, params)
    feats = prep.final_features()
    ranking = final_ranking(repository, season)
    rank_of = {team: i + 1 for i, team in enumerate(ranking)}
    X = np.column_stack([feats[name] for name in FEATURES])
    y = np.array([max(0, 26 - rank_of[t]) if t in rank_of and rank_of[t] <= 25 else 0.0
                  for t in prep.teams])
    champs = {prep.teams[i]: prep.conf[i] for i in prep.title_winners}
    # Keep ranked teams plus the best-rated others as negatives.
    keep = set(i for i, t in enumerate(prep.teams) if t in rank_of)
    for i in np.argsort(-prep.r0):
        if len(keep) >= UNRANKED_POOL:
            break
        keep.add(int(i))
    keep = sorted(keep)
    return {"season": season, "teams": [prep.teams[i] for i in keep], "X": X[keep], "y": y[keep],
            "ranking": ranking, "champions": champs, "all_teams": prep.teams, "X_all": X}


def fit(datasets: list[dict[str, Any]], ridge: float = RIDGE) -> CommitteeModel:
    X = np.vstack([d["X"] for d in datasets])
    y = np.concatenate([d["y"] for d in datasets])
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    Z = (X - mu) / sd
    w = np.linalg.solve(Z.T @ Z + ridge * len(y) / 100 * np.eye(Z.shape[1]), Z.T @ (y - y.mean()))
    raw = w / sd
    return CommitteeModel({name: float(v) for name, v in zip(FEATURES, raw)})


def predicted_ranking(model: CommitteeModel, dataset: dict[str, Any]) -> list[str]:
    feats = {name: dataset["X_all"][:, i] for i, name in enumerate(FEATURES)}
    score = model.score(feats)
    return [dataset["all_teams"][i] for i in np.argsort(-score)]


def evaluate(model: CommitteeModel, dataset: dict[str, Any],
             fmt: rules.CFPFormat = rules.DEFAULT_FORMAT) -> dict[str, Any]:
    pred = predicted_ranking(model, dataset)
    actual = dataset["ranking"]
    champs = dataset["champions"]
    nd = [rules.NOTRE_DAME]
    field_pred = [r["team"] for r in rules.select_field(pred, champs, fmt, independents=nd)]
    field_act = [r["team"] for r in rules.select_field(actual, champs, fmt, independents=nd)]
    pred_rank = {t: i + 1 for i, t in enumerate(pred)}
    top12 = actual[:12]
    return {
        "season": dataset["season"],
        "top12_overlap": len(set(pred[:12]) & set(actual[:12])),
        "field_overlap": len(set(field_pred) & set(field_act)),
        "field_missed": sorted(set(field_act) - set(field_pred)),
        "field_extra": sorted(set(field_pred) - set(field_act)),
        "top12_rank_mae": round(float(np.mean([abs(pred_rank[t] - (i + 1))
                                                 for i, t in enumerate(top12)])), 2),
        "seed_exact": sum(1 for a, b in zip(field_pred, field_act) if a == b),
    }


def leave_one_season_out(repository, params: RatingParams | None = None,
                         ridge: float = RIDGE) -> dict[str, Any]:
    seasons = committee_seasons(repository)
    data = {s: season_dataset(repository, s, params) for s in seasons}
    folds = []
    for s in seasons:
        model = fit([data[o] for o in seasons if o != s], ridge)
        folds.append(evaluate(model, data[s]))
    full = fit(list(data.values()), ridge)
    return {"folds": folds, "full_model": dict(full.weights),
            "in_sample": [evaluate(full, data[s]) for s in seasons]}
