"""Does the simulator's playoff probability mean what it says?

For each past season and each as-of week we rebuild the snapshot that was knowable
then (results, Elo, posted lines), simulate the rest of the year with a committee
model that was NOT trained on that season, and score the probabilities against what
happened. "What happened" is the 12-team field the final committee ranking and the
actual conference champions would have produced under today's rules.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from sports_aggregator.cfb import playoff_committee as pc
from sports_aggregator.cfb import playoff_rules as rules
from sports_aggregator.cfb.playoff_sim import Prepared, simulate
from sports_aggregator.cfb.playoff_state import RatingParams, load_state

BINS = ((0, .02), (.02, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.0001))


def _scores(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    p = np.array([a for a, _ in pairs]); y = np.array([b for _, b in pairs], float)
    eps = 1e-3
    pc_ = np.clip(p, eps, 1 - eps)
    base = y.mean()
    brier, base_brier = float(((p - y) ** 2).mean()), float(((base - y) ** 2).mean())
    logloss = float(-(y * np.log(pc_) + (1 - y) * np.log(1 - pc_)).mean())
    bins = []
    for lo, hi in BINS:
        m = (p >= lo) & (p < hi)
        if m.any():
            bins.append({"bin": f"{lo:.2f}-{min(hi, 1):.2f}", "n": int(m.sum()),
                         "predicted": round(float(p[m].mean()), 3), "actual": round(float(y[m].mean()), 3)})
    return {"n": len(pairs), "brier": round(brier, 4), "brier_skill": round(1 - brier / base_brier, 3),
            "log_loss": round(logloss, 4), "calibration": bins}


def run(repository, *, weeks: tuple[int, ...] = (4, 7, 10, 12), n_sims: int = 2000,
        params: RatingParams | None = None, seasons: list[int] | None = None) -> dict[str, Any]:
    seasons = seasons or pc.committee_seasons(repository)
    data = {s: pc.season_dataset(repository, s, params) for s in seasons}
    playoff_pairs: dict[int, list[tuple[float, int]]] = {w: [] for w in weeks}
    champ_pairs: dict[int, list[tuple[float, int]]] = {w: [] for w in weeks}
    detail = []
    for season in seasons:
        model = pc.fit([data[o] for o in seasons if o != season])
        truth_field = {r["team"] for r in rules.select_field(
            data[season]["ranking"], data[season]["champions"], independents=[rules.NOTRE_DAME])}
        truth_champs = set(data[season]["champions"])
        for week in weeks:
            prep = Prepared(load_state(repository, season, as_of_week=week), params)
            out = simulate(prep, n_sims=n_sims, committee=model, seed=week)
            for row in out["rows"]:
                playoff_pairs[week].append((row["playoff"], int(row["team"] in truth_field)))
                if row["title_game"] > 0:
                    champ_pairs[week].append((row["conf_champion"], int(row["team"] in truth_champs)))
            expected = {r["team"] for r in out["rows"][:12]}
            detail.append({"season": season, "week": week,
                           "top12_by_probability_hit": len(expected & truth_field),
                           "expected_playoff_teams_missed": sorted(truth_field - expected)})
    allp = [x for w in weeks for x in playoff_pairs[w]]
    allc = [x for w in weeks for x in champ_pairs[w]]
    return {"by_week_playoff": {w: _scores(playoff_pairs[w]) for w in weeks},
            "overall_playoff": _scores(allp), "overall_conf_champion": _scores(allc),
            "detail": detail}
