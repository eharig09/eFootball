"""How a pre-draft consensus board's order compared to the draft that happened.

`prospects.py` imports and reconciles a consensus board against this app's own
production profile. Once a draft class's picks are complete, the same imported
board can also be checked against the one thing production percentiles cannot
see yet: what teams actually did. This asks a narrower question than "was the
board right about a player" -- it asks "did the board's order predict draft
order," using only rows this app could confidently resolve to a real prospect
(see `prospects.import_board`'s identity rules); an unresolved row cannot be
checked against anything and is reported separately, not silently dropped.
"""

from __future__ import annotations

from contextlib import closing
from typing import Any

from sports_aggregator.cfb.prospects import consensus_board
from sports_aggregator.cfb.repository import CFBRepository


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x * var_y) ** 0.5


def consensus_backtest(repository: CFBRepository, *, draft_year: int,
                       source: str | None = None, top_n: int = 32) -> dict[str, Any]:
    """Compare an imported consensus board's rank order to the real draft.

    Board rank and actual pick number are both already integer ranks (1..N),
    so their Pearson correlation *is* a rank correlation; no separate
    Spearman transform is needed. `rank_error` is `board_rank - overall_pick`:
    positive means the board rated him below where he actually went
    (undersold, in hindsight), negative means the board rated him above where
    he actually went (oversold). Neither word is used for the player himself
    -- this grades the board's order, not the prospect.
    """
    board = consensus_board(repository, draft_year=draft_year, source=source, limit=5000)
    with closing(repository._connect()) as connection:
        actual = {
            row["college_athlete_id"]: dict(row)
            for row in connection.execute(
                """SELECT college_athlete_id, overall_pick, round, nfl_team
                   FROM draft_picks WHERE draft_year=? AND college_athlete_id IS NOT NULL""",
                (draft_year,))
        }

    matched, unresolved, undrafted = [], [], []
    for entry in board:
        if entry["link_status"] not in {"CONFIRMED", "SCHOOL_MISMATCH"} or not entry.get("cfbd_player_id"):
            unresolved.append(entry)
            continue
        pick = actual.get(entry["cfbd_player_id"])
        if pick is None:
            undrafted.append(entry)
            continue
        matched.append({
            "rank": entry["rank"], "player_name": entry["player_name"],
            "school": entry["school"], "position": entry["draft_position"],
            "overall_pick": pick["overall_pick"], "round": pick["round"],
            "nfl_team": pick["nfl_team"], "rank_error": entry["rank"] - pick["overall_pick"],
        })

    correlation = _pearson([m["rank"] for m in matched], [m["overall_pick"] for m in matched])
    top_board = [m for m in matched if m["rank"] <= top_n]
    round_one_from_top = sum(1 for m in top_board if m["round"] == 1)

    ordered = sorted(matched, key=lambda m: m["rank_error"])
    return {
        "draft_year": draft_year, "source": source, "top_n": top_n,
        "board_size": len(board),
        "matched": len(matched),
        "unresolved": len(unresolved),
        "undrafted_or_unmatched": len(undrafted),
        "rank_correlation": round(correlation, 3) if correlation is not None else None,
        "top_n_hit_rate": {
            "n": len(top_board), "round_one": round_one_from_top,
            "rate": round(round_one_from_top / len(top_board), 3) if top_board else None,
        },
        # Most negative first: the board's biggest overestimates relative to
        # where the player actually went.
        "board_oversold": ordered[:15],
        # Most positive first: the board's biggest underestimates.
        "board_undersold": list(reversed(ordered[-15:])),
    }
