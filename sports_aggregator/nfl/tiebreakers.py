"""NFL standings tiebreakers and playoff seeding as pure functions over team-pair matrices.

A season is described by square matrices indexed by team:

    H[i, j]   win credit of i against j (win 1, tie 0.5), summed over their games
    N[i, j]   number of games i and j played each other
    ND[i, j]  points i scored minus points j scored in those games

plus per-team points for/against. Everything below derives from those, so the
simulator can hand over one scenario at a time and the validation can rebuild
actual seasons from the game table.

Procedures follow the NFL's published rules:

Division (two or more clubs tied for a division title): head-to-head, division record,
common games, conference record, strength of victory, strength of schedule, combined
points scored/allowed ranking among conference teams then all teams, net points in
common games, net points in all games.

Wild card / seeding across divisions: head-to-head (sweep only, when three or more clubs
are tied), conference record, common games (minimum four), strength of victory, strength
of schedule, combined ranking among conference then all teams, net points in conference
games, net points in all games. Clubs from one division are first reduced to that
division's best club.

Whenever a step singles out a club (or a smaller group), the remaining clubs restart the
procedure from its first step, as the rules require. Net touchdowns is not modelled (it
needs touchdown counts); the final tiebreaker is a coin toss supplied as `rand`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

DIVISION_STEPS = ("h2h", "div", "common", "conf", "sov", "sos",
                  "rank_conf", "rank_all", "net_common", "net_all")
WILDCARD_STEPS = ("h2h_sweep", "conf", "common4", "sov", "sos",
                  "rank_conf", "rank_all", "net_conf", "net_all")
_EPS = 1e-9


@dataclass(frozen=True)
class Alignment:
    """Static league structure: which conference and division each team index is in."""
    conf: np.ndarray
    div: np.ndarray

    @classmethod
    def from_labels(cls, conference: Sequence[str], division: Sequence[str]) -> "Alignment":
        conf_ids = {c: i for i, c in enumerate(sorted(set(conference)))}
        div_ids = {d: i for i, d in enumerate(sorted(set(division)))}
        return cls(np.array([conf_ids[c] for c in conference]), np.array([div_ids[d] for d in division]))

    @property
    def same_conf(self) -> np.ndarray:
        return self.conf[:, None] == self.conf[None, :]

    @property
    def same_div(self) -> np.ndarray:
        return self.div[:, None] == self.div[None, :]


class TieContext:
    """One finished (or simulated) season's numbers, with the lookups tiebreakers need."""

    def __init__(self, H: np.ndarray, N: np.ndarray, ND: np.ndarray, PF: np.ndarray,
                 PA: np.ndarray, align: Alignment, rand: np.ndarray,
                 same_conf: np.ndarray | None = None, same_div: np.ndarray | None = None):
        self.H, self.N, self.ND, self.PF, self.PA = H, N, ND, PF, PA
        self.align = align
        self.rand = rand
        self.same_conf = align.same_conf if same_conf is None else same_conf
        self.same_div = align.same_div if same_div is None else same_div
        np.fill_diagonal(self.same_conf, False)
        np.fill_diagonal(self.same_div, False)
        games = N.sum(axis=1)
        self.games = games
        self.pct = H.sum(axis=1) / np.maximum(games, 1)
        self._rank_conf: np.ndarray | None = None
        self._rank_all: np.ndarray | None = None

    # combined rank of points scored (high = good) and allowed (low = good); lower sum is better
    def _combined(self, members: np.ndarray) -> np.ndarray:
        pf = self.PF[members]; pa = self.PA[members]
        # competition ranking: tied teams share a rank rather than being split by index
        rank_pf = 1 + (pf[None, :] > pf[:, None]).sum(axis=1)
        rank_pa = 1 + (pa[None, :] < pa[:, None]).sum(axis=1)
        return rank_pf + rank_pa

    def rank_all(self, i: int) -> int:
        if self._rank_all is None:
            self._rank_all = self._combined(np.arange(len(self.PF)))
        return int(self._rank_all[i])

    def rank_conf(self, i: int) -> int:
        if self._rank_conf is None:
            out = np.zeros(len(self.PF), dtype=int)
            for c in np.unique(self.align.conf):
                members = np.nonzero(self.align.conf == c)[0]
                out[members] = self._combined(members)
            self._rank_conf = out
        return int(self._rank_conf[i])


def _common(ctx: TieContext, group: Sequence[int]) -> np.ndarray:
    played = np.all(ctx.N[list(group)] > 0, axis=0)
    played[list(group)] = False
    return np.nonzero(played)[0]


def _metric(step: str, i: int, group: Sequence[int], ctx: TieContext) -> float | None:
    """Higher is better; None means the step cannot separate this group."""
    others = [j for j in group if j != i]
    H, N = ctx.H, ctx.N
    if step in ("h2h", "h2h_sweep"):
        games = N[i, others].sum()
        if not games:
            return None
        if step == "h2h_sweep" and len(group) > 2:
            wins = H[i, others]; g = N[i, others]
            if np.all(g > 0) and np.all(np.abs(wins - g) < _EPS):
                return 1.0
            if np.all(g > 0) and np.all(wins < _EPS):
                return 0.0
            return 0.5
        return float(H[i, others].sum() / games)
    if step in ("div", "conf"):
        mask = ctx.same_div[i] if step == "div" else ctx.same_conf[i]
        games = N[i, mask].sum()
        return float(H[i, mask].sum() / games) if games else None
    if step in ("common", "common4", "net_common"):
        common = _common(ctx, group)
        if not len(common):
            return None
        games = N[i, common].sum()
        minimum = 4 if step == "common4" else 1
        if step != "net_common" and min(N[j, common].sum() for j in group) < minimum:
            return None
        if step == "net_common":
            return float(ctx.ND[i, common].sum())
        return float(H[i, common].sum() / games) if games else None
    if step == "sov":
        wins = H[i]
        return float((wins * ctx.pct).sum() / wins.sum()) if wins.sum() else 0.0
    if step == "sos":
        games = N[i]
        return float((games * ctx.pct).sum() / games.sum()) if games.sum() else 0.0
    if step == "rank_conf":
        return -float(ctx.rank_conf(i))
    if step == "rank_all":
        return -float(ctx.rank_all(i))
    if step == "net_conf":
        return float(ctx.ND[i, ctx.same_conf[i]].sum())
    if step == "net_all":
        return float(ctx.ND[i].sum())
    raise ValueError(step)


def break_tie(group: Sequence[int], steps: Sequence[str], ctx: TieContext) -> list[int]:
    """Order tied clubs best to worst, restarting the procedure after each step that separates."""
    group = list(group)
    if len(group) <= 1:
        return group
    for step in steps:
        values = {i: _metric(step, i, group, ctx) for i in group}
        if any(v is None for v in values.values()):
            continue
        keyed = {i: round(v, 9) for i, v in values.items()}
        if len(set(keyed.values())) == 1:
            continue
        best = max(keyed.values())
        top = [i for i in group if keyed[i] == best]
        rest = [i for i in group if keyed[i] != best]
        return break_tie(top, steps, ctx) + break_tie(rest, steps, ctx)
    return sorted(group, key=lambda i: ctx.rand[i])


def division_order(members: Sequence[int], ctx: TieContext, limit: int | None = None) -> list[int]:
    """Teams of one division, best first (division-title tiebreakers within each tied group).

    `limit` stops once that many places are filled; ties below that are never resolved.
    """
    return _order_by_pct(members, ctx, lambda g, n: break_tie(g, DIVISION_STEPS, ctx)[:n], limit)


def _order_by_pct(pool: Sequence[int], ctx: TieContext, tiebreak, limit: int | None = None) -> list[int]:
    """Order by record; `tiebreak(group, need)` orders a tied group (at least `need` places)."""
    out: list[int] = []
    ordered = sorted(pool, key=lambda i: -ctx.pct[i])
    pos = 0
    while pos < len(ordered) and (limit is None or len(out) < limit):
        tied = [i for i in ordered if abs(ctx.pct[i] - ctx.pct[ordered[pos]]) < _EPS]
        need = len(tied) if limit is None else min(len(tied), limit - len(out))
        out.extend(tiebreak(tied, need) if len(tied) > 1 else tied)
        pos += len(tied)
    return out if limit is None else out[:limit]


def _wildcard_group_order(group: Sequence[int], ctx: TieContext, need: int | None = None) -> list[int]:
    """Order clubs tied on record who may share divisions: reduce each division to its best
    club, pick the winner of the wild-card procedure, and repeat for the rest."""
    remaining = list(group)
    out: list[int] = []
    need = len(remaining) if need is None else need
    while remaining and len(out) < need:
        reps = []
        for d in {int(ctx.align.div[i]) for i in remaining}:
            members = [i for i in remaining if ctx.align.div[i] == d]
            reps.append(members[0] if len(members) == 1 else break_tie(members, DIVISION_STEPS, ctx)[0])
        winner = reps[0] if len(reps) == 1 else break_tie(reps, WILDCARD_STEPS, ctx)[0]
        out.append(winner)
        remaining.remove(winner)
    return out


def conference_seeds(members: Sequence[int], ctx: TieContext, teams: int = 7) -> dict[str, list[int]]:
    """Playoff seeds for one conference.

    Returns {'seeds': [seed 1..teams], 'division_winners': [...], 'wild_cards': [...]}.
    Division winners take seeds 1-4 in record order; the best remaining clubs by record take
    the rest. Ties are resolved only where they decide a division title or a playoff seed.
    """
    members = list(members)
    winners: list[int] = []
    for d in sorted({int(ctx.align.div[i]) for i in members}):
        group = [i for i in members if ctx.align.div[i] == d]
        winners.append(division_order(group, ctx, limit=1)[0])
    ordered_winners = _order_by_pct(winners, ctx, lambda g, n: _wildcard_group_order(g, ctx, n))
    won = set(winners)
    pool = [i for i in members if i not in won]
    spots = max(0, teams - len(winners))
    wild = _order_by_pct(pool, ctx, lambda g, n: _wildcard_group_order(g, ctx, n), limit=spots)
    return {"seeds": ordered_winners + wild, "division_winners": ordered_winners, "wild_cards": wild}


def matrices_from_games(n_teams: int, games: Sequence[tuple[int, int, int, int]]
                        ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Build (H, N, ND, PF, PA) from (home, away, home_points, away_points) tuples."""
    H = np.zeros((n_teams, n_teams)); N = np.zeros((n_teams, n_teams)); ND = np.zeros((n_teams, n_teams))
    PF = np.zeros(n_teams); PA = np.zeros(n_teams)
    for h, a, hp, ap in games:
        N[h, a] += 1; N[a, h] += 1
        ND[h, a] += hp - ap; ND[a, h] += ap - hp
        PF[h] += hp; PA[h] += ap; PF[a] += ap; PA[a] += hp
        if hp > ap:
            H[h, a] += 1
        elif ap > hp:
            H[a, h] += 1
        else:
            H[h, a] += 0.5; H[a, h] += 0.5
    return H, N, ND, PF, PA
