"""Where one value stands relative to a peer group.

Shared by the player page, team page, staff/coach tendencies, and the game
page's matchup cards so "how good is this number" has one answer computed
one way, rather than each surface inventing its own ranking.
"""

from __future__ import annotations

from typing import Any, Iterable


def rank_within(rows: Iterable[dict[str, Any]], *, id_key: str, value_key: str,
                lower_is_better: bool = False) -> dict[Any, dict[str, int]]:
    """Standard competition ranking (ties share a rank; the next distinct
    value's rank skips ahead by the tie count) over one metric across a peer
    group. Rows missing the metric are excluded from both the ranking and
    the "of" count -- they were never really in contention for it."""
    pairs = [(row[id_key], row[value_key]) for row in rows if row.get(value_key) is not None]
    pairs.sort(key=lambda item: item[1], reverse=not lower_is_better)
    ranked: dict[Any, dict[str, int]] = {}
    rank = 0
    previous = None
    total = len(pairs)
    for index, (item_id, value) in enumerate(pairs, start=1):
        if value != previous:
            rank = index
            previous = value
        ranked[item_id] = {"rank": rank, "of": total}
    return ranked


def rank_lookup(rows: Iterable[dict[str, Any]], *, id_key: str, metrics: Iterable[str],
                lower_is_better: frozenset = frozenset()) -> dict[str, dict[Any, dict[str, int]]]:
    """rank_within for several metrics against the same peer group at once,
    reusing one already-fetched rows list instead of one query per metric."""
    rows = list(rows)
    return {
        metric: rank_within(rows, id_key=id_key, value_key=metric,
                            lower_is_better=metric in lower_is_better)
        for metric in metrics
    }
