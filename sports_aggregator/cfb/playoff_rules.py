"""Pure rules for the 12-team College Football Playoff: no database, no ratings.

Everything here takes plain names/numbers so the simulator and the tests exercise
the same code. Format assumptions are in `CFPFormat` so a rule change is a data
change rather than a code change. Defaults follow the 2026-27 CFP guide:

* 12 teams. Automatic bids: the ACC, Big 12, Big Ten and SEC champions (whatever
  their ranking) plus the highest-ranked champion of the six other conferences
  ("Group of 6", which now includes the Pac-12). Notre Dame also gets an automatic
  bid if it is ranked in the top 12; each automatic bid takes one at-large spot.
  (`auto_rule="five_best_champions"` is the 2024-25 / 2025-26 rule.)
* Seeding: the four highest-ranked teams are seeds 1-4 and get byes, seeds 5-12 follow
  the ranking, and an automatic qualifier ranked outside the top 12 goes to the
  bottom of the seeding. No re-seeding, no rematch avoidance.
  (`seeding="champion_byes"` is the 2024-25 rule.)
* Round one is played at the higher seed's campus; everything after is neutral.
* Bracket: 5v12, 6v11, 7v10, 8v9 -> quarterfinals 1 v 8/9, 2 v 7/10, 3 v 6/11,
  4 v 5/12 -> semifinals (1 side) v (4 side) and (2 side) v (3 side).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

# Conferences that hold a championship game.
TITLE_GAME_CONFERENCES = frozenset({
    "ACC", "American Athletic", "Big 12", "Big Ten", "Conference USA",
    "Mid-American", "Mountain West", "Pac-12", "SEC", "Sun Belt",
})
POWER_CONFERENCES = frozenset({"ACC", "Big 12", "Big Ten", "SEC"})
GROUP_OF_6 = frozenset({"American Athletic", "Conference USA", "Mid-American",
                        "Mountain West", "Pac-12", "Sun Belt"})
# Title game is played on the higher seed's campus rather than at a neutral site.
CAMPUS_TITLE_GAMES = frozenset({"American Athletic", "Conference USA", "Mountain West", "Pac-12"})
# Finalists are the two division winners rather than the top two overall; the
# game is hosted by the division winner with the better conference record.
DIVISION_TITLE_GAMES = frozenset({"Sun Belt"})
NON_CHAMPION_CONFERENCES = frozenset({"FBS Independents"})
NOTRE_DAME = "Notre Dame"
_UNRANKED = 10 ** 6


@dataclass(frozen=True)
class CFPFormat:
    field_size: int = 12
    byes: int = 4
    auto_rule: str = "p4_plus_g6"       # or "five_best_champions"
    auto_bids: int = 5                  # only used by "five_best_champions"
    seeding: str = "straight"           # or "champion_byes"
    independent_top: int = 12           # an independent auto-qualifies when ranked this high


DEFAULT_FORMAT = CFPFormat()


def conference_order(teams: Sequence[str],
                     games: Iterable[tuple[str, str, str | None]],
                     fallback: Mapping[str, float]) -> list[str]:
    """Order one conference's teams by conference record with tiebreakers.

    `games` are played conference games as (team_a, team_b, winner); winner None
    is a tie (half a win each). Tiebreakers, in order: conference win percentage,
    record in games among only the tied teams (head-to-head for two, a mini-league
    for three or more), then `fallback` (the committee-strength score) standing in
    for the common-opponent / ranking steps no conference can resolve without
    full game-by-game data. Ties still left after that fall to team name so the
    order is deterministic.
    """
    wins = {team: 0.0 for team in teams}
    played = {team: 0 for team in teams}
    pair_wins: dict[tuple[str, str], float] = {}
    for a, b, winner in games:
        if a not in wins or b not in wins:
            continue
        for team in (a, b):
            played[team] += 1
        credit = {a: 0.5, b: 0.5} if winner is None else {winner: 1.0}
        for team, value in credit.items():
            wins[team] += value
            other = b if team == a else a
            pair_wins[(team, other)] = pair_wins.get((team, other), 0.0) + value

    def pct(team: str) -> float:
        return wins[team] / played[team] if played[team] else 0.0

    ordered: list[str] = []
    remaining = sorted(teams, key=lambda t: (-pct(t), t))
    while remaining:
        head = pct(remaining[0])
        tied = [t for t in remaining if abs(pct(t) - head) < 1e-9]
        if len(tied) > 1:
            def mini(team: str) -> float:
                got = sum(pair_wins.get((team, o), 0.0) for o in tied if o != team)
                lost = sum(pair_wins.get((o, team), 0.0) for o in tied if o != team)
                return got / (got + lost) if got + lost else 0.5
            tied.sort(key=lambda t: (-mini(t), -fallback.get(t, 0.0), t))
        ordered.extend(tied)
        remaining = [t for t in remaining if t not in tied]
    return ordered


def title_game_pair(order: Sequence[str]) -> tuple[str, str] | None:
    """The two finalists of a conference championship game, or None."""
    return (order[0], order[1]) if len(order) >= 2 else None


def select_field(ranking: Sequence[Any], champions: Iterable[Any] | Mapping[Any, str],
                 fmt: CFPFormat = DEFAULT_FORMAT, *, independents: Iterable[Any] = ()) -> list[dict]:
    """Pick and seed the field from a final committee ranking.

    `champions` maps each conference champion to its conference name (needed for
    the Power 4 / Group of 6 split); a plain iterable of teams is enough for the
    older `five_best_champions` rule. `independents` are teams (Notre Dame) with a
    conditional automatic bid. Returns seed-ordered dicts {seed, team, auto_bid, bye}.
    Teams missing from `ranking` count as ranked last.
    """
    rank_of = {team: i for i, team in enumerate(ranking)}
    rank = lambda team: rank_of.get(team, _UNRANKED)
    if fmt.auto_rule == "five_best_champions":
        autos = sorted(set(champions), key=rank)[:fmt.auto_bids]
    else:
        conf_of = dict(champions) if isinstance(champions, Mapping) else {}
        autos = [t for t, c in conf_of.items() if c in POWER_CONFERENCES]
        group = [t for t, c in conf_of.items() if c in GROUP_OF_6]
        if group:
            autos.append(min(group, key=rank))
        autos += [t for t in independents if rank(t) < fmt.independent_top]
    auto_set = set(autos)
    at_large = [t for t in ranking if t not in auto_set][:max(0, fmt.field_size - len(autos))]
    field = list(autos) + at_large
    if fmt.seeding == "champion_byes":
        bye_teams = sorted(autos, key=rank)[:fmt.byes]
        rest = sorted((t for t in field if t not in bye_teams), key=rank)
        order = bye_teams + rest
    else:
        # An automatic qualifier outside the top 12 sits below everyone else.
        order = sorted(field, key=lambda t: (t in auto_set and rank(t) >= fmt.field_size, rank(t)))
    return [{"seed": i + 1, "team": team, "auto_bid": team in auto_set, "bye": i < fmt.byes}
            for i, team in enumerate(order)]


def bracket_pairings(field_size: int = 12, byes: int = 4) -> dict[str, list[tuple]]:
    """Static bracket shape in seed numbers. `("W", k)` points at winner of first-round game k."""
    if (field_size, byes) != (12, 4):
        raise ValueError("only the 12-team, 4-bye bracket is defined")
    first_round = [(5, 12), (6, 11), (7, 10), (8, 9)]
    quarterfinals = [(1, ("W", 3)), (4, ("W", 0)), (2, ("W", 2)), (3, ("W", 1))]
    return {"first_round": first_round, "quarterfinals": quarterfinals}


WinProb = Callable[[str, str, bool], float]


def play_bracket(field: Sequence[Mapping], win_prob: WinProb,
                 draw: Callable[[float], bool]) -> dict[str, list[str] | str]:
    """Play the whole bracket once.

    `win_prob(a, b, neutral)` is the probability `a` beats `b`; `draw(p)` returns
    True with probability p (injected so the simulator controls the random stream).
    Returns the teams that reached each round plus the champion.
    """
    by_seed = {row["seed"]: row["team"] for row in field}
    shape = bracket_pairings()
    first_winners: list[str] = []
    for high, low in shape["first_round"]:
        a, b = by_seed[high], by_seed[low]
        first_winners.append(a if draw(win_prob(a, b, False)) else b)
    quarter_winners: list[str] = []
    quarter_teams: list[str] = []
    for top, other in shape["quarterfinals"]:
        a = by_seed[top]
        b = first_winners[other[1]] if isinstance(other, tuple) else by_seed[other]
        quarter_teams += [a, b]
        quarter_winners.append(a if draw(win_prob(a, b, True)) else b)
    # quarterfinal order is [1-side, 4-side, 2-side, 3-side]
    semi_teams = list(quarter_winners)
    finalists: list[str] = []
    for a, b in ((quarter_winners[0], quarter_winners[1]), (quarter_winners[2], quarter_winners[3])):
        finalists.append(a if draw(win_prob(a, b, True)) else b)
    champion = finalists[0] if draw(win_prob(finalists[0], finalists[1], True)) else finalists[1]
    return {"quarterfinalists": quarter_teams, "semifinalists": semi_teams,
            "finalists": finalists, "champion": champion}
