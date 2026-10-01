"""NFL non-quarterback availability ablation for the calibrated margin.

Adds snap-weighted counts of listed-out players by position group on top of
the best quarterback-aware stack (core + Elo + recent margin + shrunk QB
ratings and change features). Quarterbacks are excluded here; qb_player_ablation
already carries them.

Leak policy: the injury report is the pregame report for that week, and a
player's importance is the mean snap share over his previous PRIOR_GAMES games
strictly before the game week. Games where either team has no injury rows that
week are dropped rather than read as "healthy". No market inputs.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import math
from typing import Any

from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.qb_player_ablation import (
    SHRUNK_CHANGE,
    _paired,
    _summary,
    build_rows,
)
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-availability-ablation-v1"
PRIOR_GAMES = 6
STATUS_WEIGHT = {"Out": 1.0, "Doubtful": 0.85, "Questionable": 0.2}

GROUPS = {
    "ol": {"T", "G", "C", "OL", "OT", "OG"},
    "skill": {"WR", "TE", "RB", "FB"},
    "front": {"DE", "DT", "NT", "DL", "EDGE"},
    "back": {"LB", "OLB", "ILB", "MLB", "CB", "S", "SS", "FS", "DB"},
}
POSITION_GROUP = {pos: group for group, positions in GROUPS.items() for pos in positions}

AVAIL_FEATURES = tuple(f"av_{g}_diff" for g in GROUPS)
AVAIL_TOTAL = ("av_total_diff",)

FEATURE_SETS = (
    ("qb_stack", SHRUNK_CHANGE),
    ("plus_total", SHRUNK_CHANGE + AVAIL_TOTAL),
    ("plus_groups", SHRUNK_CHANGE + AVAIL_FEATURES),
)


def _snap_history(repository: NFLRepository, start_season: int, end_season: int):
    """normalized_name -> chronological [(season, week, snap share)]."""
    history: dict[str, list[tuple[int, int, float]]] = defaultdict(list)
    with closing(repository._connect()) as connection:
        for r in connection.execute(
            """SELECT season,week,normalized_name,offense_pct,defense_pct
               FROM snap_counts WHERE season BETWEEN ? AND ?
               ORDER BY season,week""",
            (int(start_season) - 1, int(end_season)),
        ):
            share = max(r["offense_pct"] or 0.0, r["defense_pct"] or 0.0)
            history[str(r["normalized_name"])].append((int(r["season"]), int(r["week"]), float(share)))
    return history


def _importance(history, name: str, season: int, week: int) -> float:
    prior = [s for (ss, ww, s) in history.get(name, ()) if (ss, ww) < (season, week)]
    recent = prior[-PRIOR_GAMES:]
    return sum(recent) / len(recent) if recent else 0.0


def _team_losses(repository: NFLRepository, history, start_season: int, end_season: int):
    """(season, week, team) -> per-group snap-weighted loss; absent when no report."""
    out: dict[tuple[int, int, str], dict[str, float]] = {}
    with closing(repository._connect()) as connection:
        for r in connection.execute(
            """SELECT season,week,team,normalized_name,position,report_status
               FROM nfl_injury_history WHERE season BETWEEN ? AND ?""",
            (int(start_season), int(end_season)),
        ):
            key = (int(r["season"]), int(r["week"]), str(r["team"]))
            bucket = out.setdefault(key, {g: 0.0 for g in GROUPS})
            weight = STATUS_WEIGHT.get(r["report_status"])
            group = POSITION_GROUP.get(r["position"])
            if weight and group:
                bucket[group] += weight * _importance(
                    history, str(r["normalized_name"]), key[0], key[1])
    return out


def _market_lines(repository: NFLRepository, start_season: int, end_season: int):
    """game_id -> home margin implied by the closing spread (positive = home favoured)."""
    with closing(repository._connect()) as connection:
        return {
            str(r["game_id"]): None if r["spread_line"] is None else float(r["spread_line"])
            for r in connection.execute(
                "SELECT game_id,spread_line FROM games WHERE season BETWEEN ? AND ?",
                (int(start_season), int(end_season)),
            )
        }


def add_availability(rows: list[dict[str, Any]], repository: NFLRepository,
                     start_season: int, end_season: int) -> None:
    history = _snap_history(repository, start_season, end_season)
    losses = _team_losses(repository, history, start_season, end_season)
    for r in rows:
        key = (int(r["season"]), int(r["week"]))
        home = losses.get((*key, canon_team(r.get("home_team"))))
        away = losses.get((*key, canon_team(r.get("away_team"))))
        if home is None or away is None:
            continue
        # Positive diff = away team is missing more, which should favour home.
        for g in GROUPS:
            r[f"av_{g}_diff"] = away[g] - home[g]
        r["av_total_diff"] = sum(away.values()) - sum(home.values())


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    rows = build_rows(repository, start_season, end_season)
    add_availability(rows, repository, start_season, end_season)
    needed = set().union(*(set(f) for _, f in FEATURE_SETS))
    sample = [r for r in rows if r.get("actual_margin") is not None
              and all(r.get(k) is not None for k in needed)]

    pooled, folds = [], []
    for season in sorted({int(r["season"]) for r in sample}):
        train = [r for r in sample if int(r["season"]) < season]
        test = [dict(r) for r in sample if int(r["season"]) == season]
        if len(train) < 100 or not test:
            continue
        models = {label: _fit(train, f) for label, f in FEATURE_SETS}
        for r in test:
            for label, m in models.items():
                r[f"pred_{label}"] = _predict(m, r)
        pooled.extend(test)
        folds.append({"season": season, "test_games": len(test), "models": {
            label: _summary(test, f"pred_{label}") for label, _ in FEATURE_SETS}})

    heavy = [r for r in pooled
             if abs(r["av_total_diff"]) >= sorted(abs(x["av_total_diff"]) for x in pooled)[int(len(pooled) * 0.75)]]
    labels = [l for l, _ in FEATURE_SETS]
    lines = _market_lines(repository, start_season, end_season)
    bench = [dict(r, pred_market=lines[str(r["game_id"])]) for r in pooled
             if lines.get(str(r["game_id"])) is not None]
    return {
        "version": MODEL_VERSION,
        "market_used": False,
        "prior_games": PRIOR_GAMES,
        "status_weights": STATUS_WEIGHT,
        "raw_games": len(rows),
        "common_games": len(sample),
        "pooled": {l: _summary(pooled, f"pred_{l}") for l in labels},
        "paired_vs_qb_stack": {l: _paired(pooled, "pred_qb_stack", f"pred_{l}") for l in labels[1:]},
        "top_quartile_availability_gap": {
            "pooled": {l: _summary(heavy, f"pred_{l}") for l in labels},
            "paired_vs_qb_stack": {l: _paired(heavy, "pred_qb_stack", f"pred_{l}") for l in labels[1:]},
        },
        "market_benchmark": {
            "note": "closing spread as a margin forecast; benchmark only, never a feature",
            "pooled": {l: _summary(bench, f"pred_{l}") for l in labels + ["market"]},
            "paired_market_vs_best": _paired(bench, "pred_market", f"pred_{labels[-2]}"),
        },
        "walk_forward": folds,
    }
