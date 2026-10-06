"""Does the NFL simulator's playoff probability mean what it says?

For each past season and as-of week we rebuild the snapshot that was knowable then,
simulate the rest of the regular season, and score the probabilities against what
actually happened: who made the playoffs, won their division, and earned a bye.
Seasons through 2019 had six teams per conference and two byes; 2020 on has seven and one.
"""
from __future__ import annotations

from contextlib import closing
from typing import Any

import numpy as np

from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.playoff_sim import Prepared, simulate
from sports_aggregator.nfl.playoff_state import RatingParams, load_state

BINS = ((0, .02), (.02, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.0001))


def score(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    p = np.array([a for a, _ in pairs]); y = np.array([b for _, b in pairs], float)
    pc = np.clip(p, 1e-3, 1 - 1e-3)
    base = y.mean()
    brier, base_brier = float(((p - y) ** 2).mean()), float(((base - y) ** 2).mean())
    bins = []
    for lo, hi in BINS:
        m = (p >= lo) & (p < hi)
        if m.any():
            bins.append({"bin": f"{lo:.2f}-{min(hi, 1):.2f}", "n": int(m.sum()),
                         "predicted": round(float(p[m].mean()), 3), "actual": round(float(y[m].mean()), 3)})
    return {"n": len(pairs), "brier": round(brier, 4),
            "brier_skill": round(1 - brier / base_brier, 3) if base_brier else None,
            "log_loss": round(float(-(y * np.log(pc) + (1 - y) * np.log(1 - pc)).mean()), 4),
            "calibration": bins}


def season_truth(repository, season: int) -> dict[str, Any]:
    """Playoff teams, byes and division champions as they actually happened."""
    with closing(repository._connect()) as connection:
        post = connection.execute(
            "SELECT season_type, home_team, away_team FROM games WHERE season=? AND season_type IN ('WC','DIV')",
            (season,)).fetchall()
        reg = connection.execute(
            """SELECT home_team, away_team, home_score, away_score FROM games
               WHERE season=? AND season_type='REG' AND completed=1""", (season,)).fetchall()
    wc = {canon_team(t) for r in post if r["season_type"] == "WC" for t in (r["home_team"], r["away_team"])}
    div = {canon_team(t) for r in post if r["season_type"] == "DIV" for t in (r["home_team"], r["away_team"])}
    return {"playoff": wc | div, "bye": div - wc, "teams_per_conf": 7 if len(wc) == 12 else 6, "games": reg}


def division_winners(repository, season: int, state) -> set[str]:
    """Actual division champions: derived by running the tiebreaker engine on the real season."""
    from sports_aggregator.nfl import tiebreakers as tb
    teams = state.teams
    idx = {t: i for i, t in enumerate(teams)}
    align = tb.Alignment.from_labels([state.conference[t] for t in teams], [state.division[t] for t in teams])
    with closing(repository._connect()) as connection:
        rows = connection.execute(
            """SELECT home_team, away_team, home_score, away_score FROM games
               WHERE season=? AND season_type='REG' AND completed=1""", (season,)).fetchall()
    games = [(idx[canon_team(r["home_team"])], idx[canon_team(r["away_team"])], r["home_score"], r["away_score"])
             for r in rows]
    H, N, ND, PF, PA = tb.matrices_from_games(len(teams), games)
    ctx = tb.TieContext(H, N, ND, PF, PA, align, np.random.default_rng(0).random(len(teams)))
    out: set[str] = set()
    for d in np.unique(align.div):
        members = list(np.nonzero(align.div == d)[0])
        out.add(teams[tb.division_order(members, ctx, limit=1)[0]])
    return out


def run(repository, *, seasons: range | list[int] = range(2015, 2026), weeks: tuple[int, ...] = (4, 8, 12),
        n_sims: int = 1500, params: RatingParams | None = None) -> dict[str, Any]:
    pairs = {k: {w: [] for w in weeks} for k in ("playoff", "division_title", "bye")}
    for season in seasons:
        truth = season_truth(repository, season)
        state0 = load_state(repository, season, as_of_week=0)
        champs = division_winners(repository, season, state0)
        for week in weeks:
            state = load_state(repository, season, as_of_week=week)
            out = simulate(Prepared(state, params, truth["teams_per_conf"]), n_sims=n_sims, seed=week)
            for row in out["rows"]:
                t = row["team"]
                pairs["playoff"][week].append((row["playoff"], int(t in truth["playoff"])))
                pairs["division_title"][week].append((row["division_title"], int(t in champs)))
                pairs["bye"][week].append((row["bye"], int(t in truth["bye"])))
    return {
        "by_week": {k: {w: score(v) for w, v in by_week.items()} for k, by_week in pairs.items()},
        "overall": {k: score([x for w in weeks for x in v[w]]) for k, v in pairs.items()},
    }
