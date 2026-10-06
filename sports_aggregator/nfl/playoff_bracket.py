"""The NFL playoff bracket: pure functions over seeds and a win-probability callback.

Each conference sends `teams` clubs (7 since 2020, 6 before). The top seeds get byes
(1 seed with 7 teams, 2 with 6). After every round the bracket re-seeds: the best
remaining seed hosts the worst remaining seed, and so on inward. The Super Bowl is
played at a neutral site.
"""
from __future__ import annotations

from typing import Callable, Sequence

# win_prob(a, b, a_hosts) -> probability a beats b. `a_hosts` is False at a neutral site.
WinProb = Callable[[int, int, bool], float]
Draw = Callable[[float], bool]


def byes_for(teams: int) -> int:
    return 1 if teams == 7 else 2


def _pair_by_seed(alive: Sequence[int], seed_of: dict[int, int]) -> list[tuple[int, int]]:
    """Best remaining seed v worst, second best v second worst, ..."""
    order = sorted(alive, key=seed_of.__getitem__)
    return [(order[i], order[-1 - i]) for i in range(len(order) // 2)]


def play_conference(seeds: Sequence[int], win_prob: WinProb, draw: Draw) -> dict[str, list[int] | int]:
    """Play one conference's bracket. `seeds` lists team ids, seed 1 first.

    Returns who played in each round: wild_card, divisional, championship, and the champion.
    """
    seed_of = {team: i + 1 for i, team in enumerate(seeds)}
    byes = byes_for(len(seeds))
    wild = list(seeds[byes:])

    def play(pairs: Sequence[tuple[int, int]]) -> list[int]:
        # (high, low): the higher seed hosts
        return [a if draw(win_prob(a, b, True)) else b for a, b in pairs]

    wild_pairs = [(wild[i], wild[-1 - i]) for i in range(len(wild) // 2)]
    divisional = list(seeds[:byes]) + play(wild_pairs)
    championship = play(_pair_by_seed(divisional, seed_of))
    final_pair = _pair_by_seed(championship, seed_of)
    champion = play(final_pair)[0]
    return {"wild_card": wild, "divisional": sorted(divisional, key=seed_of.__getitem__),
            "championship": sorted(championship, key=seed_of.__getitem__), "champion": champion}


def play_playoffs(seeds_by_conf: Sequence[Sequence[int]], win_prob: WinProb, draw: Draw) -> dict:
    """Both conferences, then the Super Bowl at a neutral site."""
    conferences = [play_conference(seeds, win_prob, draw) for seeds in seeds_by_conf]
    a, b = conferences[0]["champion"], conferences[1]["champion"]
    champion = a if draw(win_prob(a, b, False)) else b
    return {"conferences": conferences, "super_bowl": [a, b], "champion": champion}
