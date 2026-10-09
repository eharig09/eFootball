"""The totals-regime table and the test that decides whether it predicts anything.

`regime_table` recomputes the (opening-edge bucket x market-movement state) win rates shown on the game page, with
the same methodology that built the original table (totals_divergence_matrix on the walk-forward calibrated xPoints
total) but on the corrected engine. That table is *in-sample*: with 15 cells, the best few will clear 55% by chance.

`walk_forward` is the honest version. For each test season it picks the cells that cleared the bar on EARLIER
seasons only (win rate >= `min_win_rate`, at least `min_n` games, edge of 3+ points), then follows those cells in
the test season, which is exactly what a tracked totals pick would have done. Break-even at -110 is 52.38%.

Run: python -m sports_aggregator.cfb.totals_regime_audit
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import totals_divergence_matrix as tdm
from sports_aggregator.cfb.repository import CFBRepository

TABLE_BUCKETS = ("3-4.99", "5-7.99", "8+")
MIN_WIN_RATE = 0.55
MIN_N = 30
BREAK_EVEN = 110 / 210          # win rate that breaks even at -110


def _decided(rows: list[dict[str, Any]]) -> tuple[int, int]:
    wins = sum(1 for r in rows if r["closing_result"] == "win")
    losses = sum(1 for r in rows if r["closing_result"] == "loss")
    return wins, losses


def regime_table(rows: list[dict[str, Any]]) -> dict[tuple[str, str], tuple[float, int, float]]:
    """(bucket, movement) -> (win rate %, games, mean aligned residual vs the close)."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["opening_edge_bucket"] in TABLE_BUCKETS:
            grouped[(row["opening_edge_bucket"], row["movement_state"])].append(row)
    table = {}
    for key, items in grouped.items():
        wins, losses = _decided(items)
        if wins + losses:
            table[key] = (round(100.0 * wins / (wins + losses), 2), len(items),
                          round(sum(float(r["close_result_aligned"]) for r in items) / len(items), 3))
    return table


def overall(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins, losses = _decided(rows)
    pushes = len(rows) - wins - losses
    return {"record": f"{wins}-{losses}-{pushes}", "n": len(rows),
            "win_rate": round(100.0 * wins / (wins + losses), 2),
            "mean_residual": round(sum(float(r["close_result_aligned"]) for r in rows) / len(rows), 3)}


def walk_forward(rows: list[dict[str, Any]], *, min_win_rate: float = MIN_WIN_RATE, min_n: int = MIN_N
                 ) -> dict[str, Any]:
    seasons = sorted({int(r["season"]) for r in rows})
    picks_all: list[dict[str, Any]] = []
    by_season = {}
    for test in seasons[1:]:
        earlier: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if int(row["season"]) < test and row["opening_edge_bucket"] in TABLE_BUCKETS:
                earlier[(row["opening_edge_bucket"], row["movement_state"])].append(row)
        chosen = set()
        for key, items in earlier.items():
            wins, losses = _decided(items)
            if wins + losses >= min_n and wins / (wins + losses) >= min_win_rate:
                chosen.add(key)
        picks = [r for r in rows if int(r["season"]) == test and r["opening_edge_bucket"] in TABLE_BUCKETS
                 and (r["opening_edge_bucket"], r["movement_state"]) in chosen]
        wins, losses = _decided(picks)
        by_season[test] = {"cells": len(chosen), "picks": len(picks),
                           "win_rate": round(100.0 * wins / (wins + losses), 1) if wins + losses else None}
        picks_all += picks
    wins, losses = _decided(picks_all)
    return {"picks": len(picks_all), "wins": wins, "losses": losses,
            "win_rate": round(100.0 * wins / (wins + losses), 1) if wins + losses else None,
            "break_even": round(100.0 * BREAK_EVEN, 2), "by_season": by_season,
            "rule": f"cells chosen on earlier seasons: win rate >= {int(min_win_rate * 100)}%, >= {min_n} games, edge 3+ pts"}


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    rows = tdm.build_rows(repository, test_season=int(test_season))
    table = regime_table(rows)
    return {
        "games": len(rows), "seasons": sorted({int(r["season"]) for r in rows}),
        "overall_vs_close": overall(rows),
        "regime_table": {f"{b}|{m}": list(v) for (b, m), v in sorted(table.items())},
        "tracked_cells": sorted(f"{b}|{m}" for (b, m), v in table.items() if v[0] >= 100 * MIN_WIN_RATE),
        "walk_forward": walk_forward(rows),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description="Recompute the totals regime table and its walk-forward check")
    parser.add_argument("--database", default=None)
    parser.add_argument("--test-season", type=int, default=2025)
    args = parser.parse_args(argv)
    repository = CFBRepository(args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    print(json.dumps(report(repository, test_season=args.test_season), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
