"""Monte Carlo of the rest of an NFL season into the playoffs.

One simulated season:

1. Every unplayed game gets a score: the market line when posted, else the rating
   difference (plus a persistent per-team strength error for rating-priced games),
   plus game noise; totals come from the posted total or the league average. Ties
   happen only when overtime fails to produce a winner.
2. Pairwise results (who beat whom, net points) feed the NFL tiebreakers
   (`tiebreakers.conference_seeds`): division titles, seeds 1-4, wild cards 5-7.
3. Ratings are re-solved from the simulated results with the same ridge system
   used for the live ratings, and the bracket is played with them: wild card,
   divisional, conference championship (re-seeded each round), then the Super Bowl.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from sports_aggregator.nfl import playoff_bracket as bracket
from sports_aggregator.nfl import tiebreakers as tb
from sports_aggregator.nfl.playoff_state import RatingParams, SeasonState, ridge_system
from sports_aggregator.normal import ndtr

SIGMA_LINE = 12.7        # sd of (actual margin - closing line), 2015-25
SIGMA_RATING = 13.4      # sd of margin around a rating-only spread (rating tuning, 2015-25)
TEAM_SHOCK = 4.5         # sd of a team's persistent strength error, in points (backtest log-loss
                         # tuned; smaller values leave the 90%+ playoff bin overconfident)
LEAGUE_TOTAL = 45.6      # mean total points, 2015-25
SIGMA_TOTAL = 13.2
OT_TIE = 0.12            # chance a game level after regulation stays tied after overtime
OT_MARGIN = 3            # winning margin of an overtime decision, in points


class Prepared:
    """Everything about a snapshot that does not change between scenarios."""

    def __init__(self, state: SeasonState, params: RatingParams | None = None,
                 teams_per_conf: int = 7):
        self.state = state
        self.params = params or RatingParams()
        self.teams_per_conf = teams_per_conf
        self.teams = state.teams
        self.T = T = len(self.teams)
        self.idx = {t: i for i, t in enumerate(self.teams)}
        self.align = tb.Alignment.from_labels([state.conference[t] for t in self.teams],
                                              [state.division[t] for t in self.teams])
        self.same_conf = self.align.same_conf
        self.same_div = self.align.same_div
        self.conf_members = [np.nonzero(self.align.conf == c)[0] for c in np.unique(self.align.conf)]

        A, b, _ = ridge_system(state, self.params)
        r0 = np.linalg.solve(A, b)
        self.r0 = r0 - r0.mean()
        A2, _, _ = ridge_system(state, self.params, sim_margins=True)
        self.A2_inv = np.linalg.inv(A2)
        self.b0 = b

        played, unplayed = [], []
        for g in state.games:
            h, a = self.idx.get(g["home"]), self.idx.get(g["away"])
            if h is None or a is None:
                continue
            if g["home_pts"] is not None:
                played.append((h, a, g["home_pts"], g["away_pts"]))
            else:
                unplayed.append((h, a, g))
        self.base_H, self.base_N, self.base_ND, self.base_PF, self.base_PA = tb.matrices_from_games(T, played)
        self.base_W, self.base_L, self.base_T = np.zeros(T), np.zeros(T), np.zeros(T)
        for h, a, hp, ap in played:
            if hp == ap:
                self.base_T[h] += 1; self.base_T[a] += 1
            else:
                self.base_W[h if hp > ap else a] += 1
                self.base_L[a if hp > ap else h] += 1
        self.Gu = Gu = len(unplayed)
        self.g_h = np.array([u[0] for u in unplayed], dtype=int)
        self.g_a = np.array([u[1] for u in unplayed], dtype=int)
        hfa = self.params.hfa
        self.g_nn = np.array([0.0 if u[2]["neutral"] else hfa for u in unplayed])
        self.g_lined = np.array([u[2]["line"] is not None for u in unplayed], dtype=bool)
        self.g_mu = np.array([u[2]["line"] if u[2]["line"] is not None
                              else self.r0[u[0]] - self.r0[u[1]] + self.g_nn[i]
                              for i, u in enumerate(unplayed)])
        self.g_total = np.array([u[2]["total"] if u[2]["total"] is not None else LEAGUE_TOTAL
                                 for u in unplayed])
        # Final games-played-between matrix (played + still to play): static across scenarios.
        N = self.base_N.copy()
        for h, a, _ in unplayed:
            N[h, a] += 1; N[a, h] += 1
        self.N_final = N
        self.D = np.zeros((T, Gu))
        for j, (h, a, _) in enumerate(unplayed):
            self.D[h, j] = 1; self.D[a, j] = -1


def _scores(margin: np.ndarray, total: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Integer home margin and total from continuous draws, with NFL overtime resolution."""
    m = np.rint(margin)
    level = m == 0
    tie = level & (rng.random(margin.shape) < OT_TIE)
    decided = level & ~tie
    m = np.where(decided, np.where(margin >= 0, OT_MARGIN, -OT_MARGIN), m)
    m = np.where(tie, 0, m)
    # keep home/away points whole and non-negative: total has the margin's parity
    spread = np.abs(m)
    t = spread + 2 * np.maximum(0, np.rint((total - spread) / 2))
    return m.astype(int), t.astype(int)


def simulate(prep: Prepared, *, n_sims: int = 4000, seed: int = 11, batch: int = 400) -> dict[str, Any]:
    T = prep.T
    params, rng = prep.params, np.random.default_rng(seed)
    per = prep.teams_per_conf
    byes = bracket.byes_for(per)
    names = ("division_title", "playoff", "bye", "divisional", "championship", "super_bowl", "champion")
    counters = {n: np.zeros(T) for n in names}
    seed_counts = np.zeros((T, per))
    wins_sum = np.zeros(T)
    win_hist = np.zeros((T, 19))
    sigma = SIGMA_LINE
    done = 0

    while done < n_sims:
        K = min(batch, n_sims - done)
        done += K
        Gu = prep.Gu
        if Gu:
            e = rng.standard_normal((K, T)) * TEAM_SHOCK
            shock = np.where(prep.g_lined, 0.0, 1.0) * (e[:, prep.g_h] - e[:, prep.g_a])
            cont = prep.g_mu + shock + sigma * rng.standard_normal((K, Gu))
            total = prep.g_total + SIGMA_TOTAL * rng.standard_normal((K, Gu))
            M, TOT = _scores(cont, total, rng)
            hp = (TOT + M) // 2
            ap = TOT - hp
            win_home = np.where(M > 0, 1.0, np.where(M == 0, 0.5, 0.0))
            kk = np.repeat(np.arange(K), Gu)
            h = np.tile(prep.g_h, K); a = np.tile(prep.g_a, K)
            flat_ha = kk * T * T + h * T + a
            flat_ah = kk * T * T + a * T + h
            wh, md = win_home.ravel(), M.ravel().astype(float)
            Hb = np.tile(prep.base_H, (K, 1, 1)).ravel()
            Hb += np.bincount(flat_ha, weights=wh, minlength=K * T * T)
            Hb += np.bincount(flat_ah, weights=1 - wh, minlength=K * T * T)
            Hb = Hb.reshape(K, T, T)
            NDb = np.tile(prep.base_ND, (K, 1, 1)).ravel()
            NDb += np.bincount(flat_ha, weights=md, minlength=K * T * T)
            NDb += np.bincount(flat_ah, weights=-md, minlength=K * T * T)
            NDb = NDb.reshape(K, T, T)
            PF = prep.base_PF + np.stack([np.bincount(prep.g_h, weights=hp[k], minlength=T)
                                          + np.bincount(prep.g_a, weights=ap[k], minlength=T) for k in range(K)])
            PA = prep.base_PA + np.stack([np.bincount(prep.g_h, weights=ap[k], minlength=T)
                                          + np.bincount(prep.g_a, weights=hp[k], minlength=T) for k in range(K)])
            m_cap = np.clip(M - prep.g_nn, -params.margin_cap, params.margin_cap)
            B = prep.b0[:, None] + params.margin_weight * (prep.D @ m_cap.T)
        else:
            Hb = np.tile(prep.base_H, (K, 1, 1)); NDb = np.tile(prep.base_ND, (K, 1, 1))
            PF = np.tile(prep.base_PF, (K, 1)); PA = np.tile(prep.base_PA, (K, 1))
            B = np.tile(prep.b0[:, None], (1, K))
        R = (prep.A2_inv @ B).T
        R = R - R.mean(axis=1, keepdims=True)
        coin = rng.random((K, T))
        u = rng.random((K, 15))

        for k in range(K):
            ctx = tb.TieContext(Hb[k], prep.N_final, NDb[k], PF[k], PA[k], prep.align, coin[k],
                                prep.same_conf, prep.same_div)
            seeds_by_conf = []
            for members in prep.conf_members:
                result = tb.conference_seeds(members, ctx, per)
                seeds = [int(t) for t in result["seeds"]]
                seeds_by_conf.append(seeds)
                for t in result["division_winners"]:
                    counters["division_title"][t] += 1
                for s, t in enumerate(seeds):
                    counters["playoff"][t] += 1
                    seed_counts[t, s] += 1
                for t in seeds[:byes]:
                    counters["bye"][t] += 1
            Rk = R[k]
            draws = iter(u[k])

            def win_prob(x: int, y: int, x_hosts: bool, Rk=Rk) -> float:
                return float(ndtr((Rk[x] - Rk[y] + (params.hfa if x_hosts else 0.0)) / SIGMA_RATING))

            out = bracket.play_playoffs(seeds_by_conf, win_prob, lambda p, d=draws: next(d) < p)
            for conf in out["conferences"]:
                for t in conf["divisional"]:
                    counters["divisional"][t] += 1
                for t in conf["championship"]:
                    counters["championship"][t] += 1
            for t in out["super_bowl"]:
                counters["super_bowl"][t] += 1
            counters["champion"][out["champion"]] += 1

        wins = Hb.sum(axis=2)
        wins_sum += wins.sum(axis=0)
        np.add.at(win_hist, (np.broadcast_to(np.arange(T), (K, T)), np.clip(np.rint(wins).astype(int), 0, 18)), 1)

    n = float(n_sims)
    base_g = prep.base_N.sum(axis=1)
    rows = []
    for i, team in enumerate(prep.teams):
        rows.append({
            "team": team, "conference": prep.state.conference[team], "division": prep.state.division[team],
            "rating": round(float(prep.r0[i]), 2),
            "wins": int(prep.base_W[i]), "losses": int(prep.base_L[i]), "ties": int(prep.base_T[i]),
            "games": int(base_g[i]),
            "projected_wins": round(float(wins_sum[i] / n), 2),
            **{name: round(float(counters[name][i] / n), 4) for name in names},
            "seed_probs": [round(float(x / n), 4) for x in seed_counts[i]],
            "win_dist": [round(float(x / n), 4) for x in win_hist[i]],
        })
    rows.sort(key=lambda r: (-r["playoff"], -r["champion"], -r["rating"]))
    return {"season": prep.state.season, "as_of_week": prep.state.as_of_week, "n_sims": n_sims,
            "games_remaining": prep.Gu, "rows": rows,
            "format": {"teams_per_conference": per, "byes_per_conference": byes},
            "params": {"sigma_line": SIGMA_LINE, "sigma_rating": SIGMA_RATING, "team_shock": TEAM_SHOCK,
                       "hfa": params.hfa, "ot_tie": OT_TIE}}
