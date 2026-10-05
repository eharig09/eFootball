"""Monte Carlo of the rest of a college football season into the 12-team playoff.

One simulated season:

1. Every unplayed game gets a margin: the market line when one is posted, else the
   rating difference, plus a persistent per-team strength shock (so a team that is
   secretly better wins many games together) plus game noise.
2. Conference standings -> tiebreakers (`playoff_rules.conference_order`) -> the top
   two play the conference title game, in conferences that hold one.
3. Ratings are re-solved from the simulated results (the same ridge system used
   for the live ratings, so a 12-0 team earns the rating bump a real one would).
4. A committee-ranking model (`COMMITTEE`, fit on the 2023-25 final rankings in
   `playoff_committee`) scores every team from rating, strength of record, losses,
   conference championship, and schedule strength.
5. `select_field` applies the 2026-27 rules (Power 4 champions + best Group of 6
   champion + Notre Dame if top 12, rest at-large), seeds them, and the bracket is
   played with the same game model.

Everything is vectorized across scenarios except the tiebreak and bracket steps,
which only run where they are needed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from sports_aggregator.cfb import playoff_rules as rules
from sports_aggregator.cfb.playoff_state import (
    RatingParams, SeasonState, fcs_rating, ridge_system,
)

SIGMA_TOTAL = 15.2      # sd of (actual margin - closing line); fit on 2022-25
TEAM_SHOCK = 4.0        # sd of a team's persistent strength error, in points (backtest-tuned)
SIGMA_GAME = math.sqrt(SIGMA_TOTAL ** 2 - 2 * TEAM_SHOCK ** 2)
SOR_REF_RANK = 20       # strength of record is measured vs the 20th-best team
TOP_N = 40              # ranking depth kept per scenario
FEATURES = ("rating", "sor", "losses", "champion", "sos", "win_pct")


@dataclass(frozen=True)
class CommitteeModel:
    """Linear score over FEATURES; higher = ranked higher. Fit by `playoff_committee`."""
    weights: Mapping[str, float]

    def score(self, features: Mapping[str, np.ndarray]) -> np.ndarray:
        return sum(self.weights.get(name, 0.0) * features[name] for name in FEATURES)

    def score_without_champion(self, features: Mapping[str, np.ndarray]) -> np.ndarray:
        return sum(self.weights.get(name, 0.0) * features[name]
                   for name in FEATURES if name != "champion")


# Fit on the 2023-25 final committee rankings (ridge 30); leave-one-season-out it
# recovers 11 of the 12 playoff teams each year. Refit with
# `python -m sports_aggregator.cfb.playoff_cli committee`.
COMMITTEE = CommitteeModel({
    "rating": 0.402, "sor": 1.141, "losses": -0.680, "champion": 1.843,
    "sos": 0.318, "win_pct": 8.525,
})


def ndtr(x: np.ndarray | float) -> np.ndarray:
    """Standard normal CDF via the Abramowitz-Stegun erf fit (|err| < 1.5e-7); numpy only."""
    z = np.asarray(x, dtype=float) / math.sqrt(2.0)
    sign = np.sign(z)
    a = np.abs(z)
    t = 1.0 / (1.0 + 0.3275911 * a)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    erf = sign * (1.0 - poly * np.exp(-a * a))
    return 0.5 * (1.0 + erf)


class Prepared:
    """Everything about a season snapshot that does not change between scenarios."""

    def __init__(self, state: SeasonState, params: RatingParams | None = None):
        self.state = state
        self.params = params or RatingParams()
        self.teams = state.teams
        self.T = T = len(self.teams)
        self.tidx = {t: i for i, t in enumerate(self.teams)}
        self.conf = [state.conference[t] for t in self.teams]

        A, b, _ = ridge_system(state, self.params)
        r0 = np.linalg.solve(A, b)
        self.r0 = r0 - r0.mean()
        A2, _, _ = ridge_system(state, self.params, sim_margins=True)
        self.A2_inv = np.linalg.inv(A2)
        self.b0 = b

        # Non-FBS opponents become extra "virtual" columns with fixed ratings.
        ratings = {t: float(self.r0[i]) for t, i in self.tidx.items()}
        self.fcs_names: list[str] = []
        ext = dict(self.tidx)
        for g in state.games:
            for side in ("home", "away"):
                name = g[side]
                if name not in ext:
                    ext[name] = T + len(self.fcs_names)
                    self.fcs_names.append(name)
        self.fcs_r = np.array([fcs_rating(state, n, ratings) for n in self.fcs_names])
        self.r_ext = np.concatenate([self.r0, self.fcs_r])
        self.ext = ext
        self.r_ref = float(np.sort(self.r0)[::-1][min(SOR_REF_RANK, T) - 1])

        hfa = self.params.hfa
        base_w = np.zeros(T); base_l = np.zeros(T); base_sor = np.zeros(T)
        base_cw = np.zeros(T); n_conf = np.zeros(T)
        opp_sum = np.zeros(T); n_games = np.zeros(T)
        title_winners: list[int] = []
        played_conf: dict[str, list[tuple[int, int, int | None]]] = {}
        unplayed: list[dict[str, Any]] = []

        for g in state.games:
            h, a = ext[g["home"]], ext[g["away"]]
            if h >= T and a >= T:
                continue
            nn = 0.0 if g["neutral"] else hfa
            for side, opp, adj in ((h, a, nn), (a, h, -nn)):
                if side < T:
                    opp_sum[side] += self.r_ext[opp]
                    n_games[side] += 1
            same_conf = (h < T and a < T and self.conf[h] == self.conf[a])
            conf_game = bool(g["conf_game"]) and same_conf
            if conf_game:
                for side in (h, a):
                    n_conf[side] += 1
            if g["home_pts"] is None:
                mu = g["line"] if g["line"] is not None else self.r_ext[h] - self.r_ext[a] + nn
                unplayed.append({"h": h, "a": a, "nn": nn, "mu": float(mu), "conf": conf_game,
                                 "fbs": h < T and a < T})
                continue
            home_won = g["home_pts"] > g["away_pts"]
            for side, opp, adj, won in ((h, a, nn, home_won), (a, h, -nn, not home_won)):
                if side >= T:
                    continue
                if won:
                    base_w[side] += 1
                else:
                    base_l[side] += 1
                p_ref = float(ndtr((self.r_ref - self.r_ext[opp] + adj) / SIGMA_TOTAL))
                base_sor[side] += (1.0 if won else 0.0) - p_ref
                if conf_game and won:
                    base_cw[side] += 1
            if conf_game:
                played_conf.setdefault(self.conf[h], []).append((h, a, h if home_won else a))
            if g.get("title") and h < T and a < T:
                title_winners.append(h if home_won else a)

        self.base_w, self.base_l, self.base_sor = base_w, base_l, base_sor
        self.base_cw, self.n_conf = base_cw, n_conf
        self.sos = opp_sum / np.maximum(n_games, 1)
        self.title_winners = title_winners
        self.played_conf = played_conf
        self.unplayed = unplayed
        self.Gu = Gu = len(unplayed)

        arr = lambda key, dtype=float: np.array([u[key] for u in unplayed], dtype=dtype)
        self.g_h, self.g_a = arr("h", int), arr("a", int)
        self.g_nn, self.g_mu = arr("nn"), arr("mu")
        self.g_conf, self.g_fbs = arr("conf", bool), arr("fbs", bool)
        # home/away incidence over FBS teams (FCS columns dropped)
        self.Hm = np.zeros((Gu, T)); self.Am = np.zeros((Gu, T))
        self.HCm = np.zeros((Gu, T)); self.ACm = np.zeros((Gu, T))
        self.D = np.zeros((T, Gu))
        for j, u in enumerate(unplayed):
            if u["h"] < T:
                self.Hm[j, u["h"]] = 1
                if u["conf"]:
                    self.HCm[j, u["h"]] = 1
            if u["a"] < T:
                self.Am[j, u["a"]] = 1
                if u["conf"]:
                    self.ACm[j, u["a"]] = 1
            if u["fbs"]:
                self.D[u["h"], j] = 1
                self.D[u["a"], j] = -1
        # Reference-team win probability for each side of every unplayed game.
        self.pref_home = ndtr((self.r_ref - self.r_ext[self.g_a] + self.g_nn) / SIGMA_TOTAL) if Gu else np.zeros(0)
        self.pref_away = ndtr((self.r_ref - self.r_ext[self.g_h] - self.g_nn) / SIGMA_TOTAL) if Gu else np.zeros(0)

        self.conf_cols: dict[str, np.ndarray] = {}
        for i, c in enumerate(self.conf):
            self.conf_cols.setdefault(c, []).append(i)
        self.title_confs = [c for c in self.conf_cols
                            if c in rules.TITLE_GAME_CONFERENCES and len(self.conf_cols[c]) >= 2]
        self.conf_cols = {c: np.array(v) for c, v in self.conf_cols.items()}
        # Conferences whose finalists are the two division winners (Sun Belt).
        self.div_cols: dict[str, list[np.ndarray]] = {}
        for c in rules.DIVISION_TITLE_GAMES & set(self.title_confs):
            groups: dict[str, list[int]] = {}
            for i in self.conf_cols[c]:
                groups.setdefault(state.division.get(self.teams[i], ""), []).append(int(i))
            groups.pop("", None)
            if len(groups) == 2:
                self.div_cols[c] = [np.array(v) for v in groups.values()]
        self.notre_dame = [self.tidx[rules.NOTRE_DAME]] if rules.NOTRE_DAME in self.tidx else []
        self.sim_conf_games: dict[str, list[tuple[int, int, int]]] = {}
        for j, u in enumerate(unplayed):
            if u["conf"]:
                self.sim_conf_games.setdefault(self.conf[u["h"]], []).append((j, u["h"], u["a"]))

    # ------------------------------------------------------------------ features
    def final_features(self) -> dict[str, np.ndarray]:
        """Committee features for a snapshot whose games are all played (training)."""
        champion = np.zeros(self.T)
        champion[self.title_winners] = 1
        games = self.base_w + self.base_l
        return {"rating": self.r0.copy(), "sor": self.base_sor.copy(), "losses": self.base_l.copy(),
                "champion": champion, "sos": self.sos.copy(),
                "win_pct": self.base_w / np.maximum(games, 1)}


def _resolve_ties(prep: Prepared, conf: str, k: int, hw_k: np.ndarray,
                  fallback: np.ndarray, cols: np.ndarray | None = None) -> list[int]:
    cols = prep.conf_cols[conf] if cols is None else cols
    games = list(prep.played_conf.get(conf, []))
    for j, h, a in prep.sim_conf_games.get(conf, []):
        games.append((h, a, h if hw_k[j] else a))
    members = {int(c) for c in cols}
    games = [g for g in games if g[0] in members and g[1] in members]
    strength = {int(c): float(fallback[c]) for c in cols}
    return rules.conference_order(sorted(members), games, strength)


def simulate(prep: Prepared, *, n_sims: int = 5000, seed: int = 7,
             committee: CommitteeModel = COMMITTEE,
             fmt: rules.CFPFormat = rules.DEFAULT_FORMAT, batch: int = 1000) -> dict[str, Any]:
    """Run `n_sims` seasons and return per-team outcome probabilities."""
    T, params = prep.T, prep.params
    rng = np.random.default_rng(seed)
    n_conf_safe = np.maximum(prep.n_conf, 1)
    hfa = params.hfa

    counters = {name: np.zeros(T) for name in (
        "playoff", "auto", "at_large", "bye", "first_round", "quarterfinal", "semifinal",
        "final", "champion", "conf_champion", "title_game", "top_four")}
    seed_counts = np.zeros((T, fmt.field_size))
    rank_sum = np.zeros(T)
    win_hist = np.zeros((T, 17))
    wins_sum = np.zeros(T)
    field_counts: dict[tuple[int, ...], int] = {}
    done = 0

    while done < n_sims:
        K = min(batch, n_sims - done)
        done += K
        e = rng.standard_normal((K, T + len(prep.fcs_names))) * TEAM_SHOCK
        e[:, T:] = 0.0
        if prep.Gu:
            z = rng.standard_normal((K, prep.Gu))
            margin = prep.g_mu + e[:, prep.g_h] - e[:, prep.g_a] + SIGMA_GAME * z
            hw = margin > 0
            hwf = hw.astype(float)
            W = prep.base_w + hwf @ prep.Hm + (1 - hwf) @ prep.Am
            L = prep.base_l + (1 - hwf) @ prep.Hm + hwf @ prep.Am
            S = (prep.base_sor + (hwf - prep.pref_home) @ prep.Hm
                 + ((1 - hwf) - prep.pref_away) @ prep.Am)
            CW = prep.base_cw + hwf @ prep.HCm + (1 - hwf) @ prep.ACm
            cap = params.margin_cap
            m_cap = np.clip(margin - prep.g_nn, -cap, cap)
            B = prep.b0[:, None] + params.margin_weight * (prep.D @ m_cap.T)
        else:
            hw = np.zeros((K, 0), dtype=bool)
            W = np.tile(prep.base_w, (K, 1)); L = np.tile(prep.base_l, (K, 1))
            S = np.tile(prep.base_sor, (K, 1)); CW = np.tile(prep.base_cw, (K, 1))
            B = np.tile(prep.b0[:, None], (1, K))
        R = (prep.A2_inv @ B).T
        R = R - R.mean(axis=1, keepdims=True)

        features = {"rating": R, "sor": S, "losses": L, "champion": np.zeros((K, T)),
                    "sos": np.broadcast_to(prep.sos, (K, T)), "win_pct": W / np.maximum(W + L, 1)}
        pre_champion = committee.score_without_champion(features)
        pct = CW / n_conf_safe
        idx = np.arange(K)
        champion_of: dict[str, np.ndarray] = {}
        title_pair: dict[str, tuple[np.ndarray, np.ndarray]] = {}

        for conf in prep.title_confs:
            cols = prep.conf_cols[conf]
            if conf in prep.div_cols:
                tops = []
                for group in prep.div_cols[conf]:
                    pg = pct[:, group]
                    best = group[np.argmax(pg, axis=1)].copy()
                    if len(group) >= 2:
                        sp = np.sort(pg, axis=1)[:, ::-1]
                        for k in np.nonzero(sp[:, 0] - sp[:, 1] < 1e-9)[0]:
                            best[k] = _resolve_ties(prep, conf, int(k), hw[k], pre_champion[k], group)[0]
                    tops.append(best)
                # the division winner with the better conference record hosts
                swap = pct[idx, tops[1]] > pct[idx, tops[0]]
                ta = np.where(swap, tops[1], tops[0])
                tb = np.where(swap, tops[0], tops[1])
            else:
                pc = pct[:, cols]
                order = np.argsort(-pc, axis=1, kind="stable")
                ta, tb = cols[order[:, 0]].copy(), cols[order[:, 1]].copy()
                if len(cols) >= 3:
                    sp = np.take_along_axis(pc, order, axis=1)
                    for k in np.nonzero(sp[:, 1] - sp[:, 2] < 1e-9)[0]:
                        ranked = _resolve_ties(prep, conf, int(k), hw[k], pre_champion[k])
                        ta[k], tb[k] = ranked[0], ranked[1]
            site = hfa if (conf in rules.CAMPUS_TITLE_GAMES or conf in prep.div_cols) else 0.0
            m = R[idx, ta] - R[idx, tb] + site + SIGMA_TOTAL * rng.standard_normal(K)
            win = np.where(m > 0, ta, tb)
            lose = np.where(m > 0, tb, ta)
            W[idx, win] += 1
            L[idx, lose] += 1
            S[idx, win] += 1 - ndtr((prep.r_ref - prep.r0[lose]) / SIGMA_TOTAL)
            S[idx, lose] -= ndtr((prep.r_ref - prep.r0[win]) / SIGMA_TOTAL)
            features["champion"][idx, win] = 1
            champion_of[conf] = win
            title_pair[conf] = (ta, tb)
            counters["conf_champion"] += np.bincount(win, minlength=T)
            counters["title_game"] += np.bincount(ta, minlength=T) + np.bincount(tb, minlength=T)

        features["win_pct"] = W / np.maximum(W + L, 1)
        score = committee.score(features)
        order_all = np.argsort(-score, axis=1)
        top = order_all[:, :TOP_N]
        rank_of = np.argsort(order_all, axis=1) + 1
        rank_sum += rank_of.sum(axis=0)
        wi = np.clip(W.astype(int), 0, 16)
        np.add.at(win_hist, (np.broadcast_to(np.arange(T), (K, T)), wi), 1)
        wins_sum += W.sum(axis=0)
        champ_cols = (np.stack([champion_of[c] for c in prep.title_confs], axis=1)
                      if prep.title_confs else np.zeros((K, 0), dtype=int))
        conf_names = list(prep.title_confs)
        u = rng.random((K, 11))

        for k in range(K):
            champs = {int(c): conf_names[j] for j, c in enumerate(champ_cols[k])}
            ranking = [int(t) for t in top[k]]
            field = rules.select_field(ranking, champs, fmt, independents=prep.notre_dame)
            for row in field:
                t = row["team"]
                counters["playoff"][t] += 1
                counters["auto" if row["auto_bid"] else "at_large"][t] += 1
                if row["bye"]:
                    counters["bye"][t] += 1
                else:
                    counters["first_round"][t] += 1
                seed_counts[t, row["seed"] - 1] += 1
            for t in ranking[:4]:
                counters["top_four"][t] += 1
            key = tuple(sorted(row["team"] for row in field))
            field_counts[key] = field_counts.get(key, 0) + 1
            Rk = R[k]
            draws = iter(u[k])

            def win_prob(a: int, b: int, neutral: bool, Rk=Rk) -> float:
                return float(ndtr((Rk[a] - Rk[b] + (0.0 if neutral else hfa)) / SIGMA_TOTAL))

            result = rules.play_bracket(field, win_prob, lambda p, d=draws: next(d) < p)
            for t in result["quarterfinalists"]:
                counters["quarterfinal"][t] += 1
            for t in result["semifinalists"]:
                counters["semifinal"][t] += 1
            for t in result["finalists"]:
                counters["final"][t] += 1
            counters["champion"][result["champion"]] += 1

    n = float(n_sims)
    rows = []
    for i, team in enumerate(prep.teams):
        total_games = prep.base_w[i] + prep.base_l[i]
        rows.append({
            "team": team, "conference": prep.conf[i], "rating": round(float(prep.r0[i]), 2),
            "wins": int(prep.base_w[i]), "losses": int(prep.base_l[i]),
            "projected_wins": round(float(wins_sum[i] / n), 2),
            "avg_rank": round(float(rank_sum[i] / n), 1),
            **{name: round(float(counters[name][i] / n), 4) for name in counters},
            "seed_probs": [round(float(x / n), 4) for x in seed_counts[i]],
            "win_dist": [round(float(x / n), 4) for x in win_hist[i]],
        })
    rows.sort(key=lambda r: (-r["playoff"], -r["champion"], r["avg_rank"]))
    top_fields = sorted(field_counts.items(), key=lambda kv: -kv[1])[:5]
    return {
        "season": prep.state.season, "as_of_week": prep.state.as_of_week, "n_sims": n_sims,
        "games_remaining": prep.Gu, "rows": rows,
        "expected_field": [prep.teams[t] for t in sorted(
            range(T), key=lambda i: -counters["playoff"][i])[:fmt.field_size]],
        "common_fields": [{"teams": [prep.teams[t] for t in key], "probability": round(c / n, 4)}
                          for key, c in top_fields],
        "format": {"field_size": fmt.field_size, "auto_bids": fmt.auto_bids, "byes": fmt.byes,
                   "seeding": fmt.seeding},
        "params": {"sigma_total": SIGMA_TOTAL, "team_shock": TEAM_SHOCK, "hfa": hfa,
                   "title_game_conferences": sorted(prep.title_confs)},
    }
