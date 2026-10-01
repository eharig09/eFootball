"""Volume-adjusted EPA/play edges for the passing and run charting grids (NFL and college football).

A zone's EPA per play from a handful of plays is mostly noise: +1.80 from two attempts should not outrank +0.35 from
sixty. Each side's EPA is therefore shrunk toward the league average (zero, since EPA is already measured against
expectation) in proportion to its volume::

    adjusted = epa * n / (n + PRIOR_ATTEMPTS)

so a zone with `PRIOR_ATTEMPTS` plays keeps half its signal and a deep sample keeps nearly all of it. The matchup edge
combines the two adjusted sides, and the overall edge for a whole grid is the average of its zone edges weighted by how
much of each side's volume the zone carries (the geometric mean of the two shares, the same "overlap" the grids already
show) -- so the busy zones decide the headline and the empty ones cannot.
"""

from __future__ import annotations

from math import sqrt
from typing import Any, Iterable

#: Plays at which a side keeps half of its raw EPA/play.
PRIOR_ATTEMPTS = 12.0
#: |edge| (EPA per play) below which a grid is called even.
EVEN_BAND = 0.025


def shrink(epa: float | None, volume: float | int | None, prior: float = PRIOR_ATTEMPTS) -> float | None:
    """EPA/play pulled toward zero by how little it rests on."""
    if epa is None or not volume or volume <= 0:
        return None
    return float(epa) * float(volume) / (float(volume) + prior)


def zone_edge(off_epa: float | None, off_n: float | int | None, def_epa: float | None, def_n: float | int | None, *,
              combine: str = "sum", prior: float = PRIOR_ATTEMPTS) -> dict[str, Any]:
    """The edge for one zone from both sides' EPA/play and plays.

    `combine="sum"` adds the two adjusted values (each is measured against the league average, so they stack);
    `"mean"` averages them. Both are in EPA per play; `raw` is the same combination without the volume adjustment.
    """
    comparable = bool(off_n and def_n and off_epa is not None and def_epa is not None)
    if not comparable:
        return {"edge": None, "raw": None, "offense_adjusted": None, "defense_adjusted": None, "reliability": 0.0}
    off_adj, def_adj = shrink(off_epa, off_n, prior), shrink(def_epa, def_n, prior)
    divisor = 2.0 if combine == "mean" else 1.0
    return {
        "edge": (off_adj + def_adj) / divisor, "raw": (off_epa + def_epa) / divisor,
        "offense_adjusted": off_adj, "defense_adjusted": def_adj,
        # 0-1: how much of the raw signal survived the adjustment (the weaker side limits it)
        "reliability": min(off_n / (off_n + prior), def_n / (def_n + prior)),
    }


def interaction_share(off_n: float | int | None, off_total: float | int | None,
                      def_n: float | int | None, def_total: float | int | None) -> float | None:
    """How much of both sides' play volume a zone carries (geometric mean of the two shares)."""
    if not (off_total and def_total):
        return None
    return sqrt(((off_n or 0) / off_total) * ((def_n or 0) / def_total))


def lean(edge: float | None) -> str:
    if edge is None:
        return "neutral"
    return "offense" if edge > EVEN_BAND else "defense" if edge < -EVEN_BAND else "even"


def overall_edge(zones: Iterable[tuple[float | None, float | None]]) -> dict[str, Any]:
    """Volume-weighted average of zone edges. `zones` are (adjusted edge, weight) pairs; empty zones are skipped."""
    pairs = [(edge, weight) for edge, weight in zones if edge is not None and weight]
    total = sum(weight for _, weight in pairs)
    if not total:
        return {"edge": None, "lean": "neutral", "zones": 0, "coverage": 0.0}
    value = sum(edge * weight for edge, weight in pairs) / total
    return {"edge": value, "lean": lean(value), "zones": len(pairs), "coverage": total}
