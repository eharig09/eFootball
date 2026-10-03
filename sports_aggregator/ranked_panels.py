"""Shared building blocks for percentile panels (NFL and college football team pages).

A panel is `{"season", "groups": [{"label", "rows": [row, ...]}], "has_data"}` where each row carries the value, its
1-N rank, the pool size and a 0-100 percentile (higher is always better, whatever the raw direction of the stat).
`templates/_ui_kit.html` renders it.
"""

from __future__ import annotations

from typing import Any, Iterable

from sports_aggregator.nfl.ranking import rank_within


def percentile(rank: int | None, total: int | None) -> int | None:
    """0-100, higher is better; the best of N is 100 and the worst is 0."""
    if not rank or not total:
        return None
    if total == 1:
        return 100
    return round(100 * (total - rank) / (total - 1))


def percentile_in(value: float | None, population: list[float]) -> int | None:
    """Percentile of `value` among `population` by the same rule as the ranked panels.

    Rank is standard competition ranking (ties share the better rank), so a value
    equal to the league's best is 100 and one equal to its worst is 0.
    """
    if value is None or not population:
        return None
    rank = 1 + sum(1 for other in population if other > value)
    # A value outside the field (a current-roster line graded below every team's season
    # grade) would rank past last place; it is simply the bottom of the field.
    pct = percentile(rank, len(population))
    return None if pct is None else max(0, min(100, pct))


def tone(pctl: int | None, neutral: bool = False) -> str:
    if neutral or pctl is None:
        return "neutral"
    return "good" if pctl >= 67 else ("mid" if pctl >= 34 else "poor")


def panel_row(label: str, key: str, fmt: str, value: Any, rank: int | None, total: int | None,
              neutral: bool) -> dict[str, Any]:
    pctl = percentile(rank, total)
    return {"label": label, "key": key, "format": fmt, "value": value, "rank": rank,
            "of": total, "pctl": pctl, "tone": tone(pctl, neutral)}


def rows_for_team(pool: list[dict[str, Any]], team: str, id_key: str,
                  definitions: Iterable[tuple[str, str, str, bool, bool]]) -> list[dict[str, Any]]:
    """Rows for one team from `(label, key, format, lower_is_better, neutral)` definitions against a pool of dicts."""
    current = next((row for row in pool if row.get(id_key) == team), {})
    output = []
    for label, key, fmt, lower, neutral in definitions:
        value = current.get(key)
        if value is None:
            continue
        ranked = rank_within(pool, id_key=id_key, value_key=key, lower_is_better=lower).get(team, {})
        output.append(panel_row(label, key, fmt, value, ranked.get("rank"), ranked.get("of"), neutral))
    return output
