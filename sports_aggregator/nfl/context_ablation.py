"""NFL play-level context/luck ablation for the calibrated margin.

The core already consumes game-level EPA, success and explosive rates. It does
not see turnovers and fumble luck, special teams, penalties, sacks taken,
starting field position, or non-offensive scores. This derives those from the
cached nflverse play-by-play and tests them as pregame trailing snapshots on top
of the quarterback-aware stack (core + Elo + recent margin + shrunk QB state).

Leak policy: snapshots use only earlier week batches, season-decayed with
STATE_SEASON_DECAY; ratios are sums over sums so heavy shrinkage is left to the
ridge. No market inputs.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from sports_aggregator.nfl.drive_projection import STATE_SEASON_DECAY
from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, _paired, _summary, build_rows
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-context-ablation-v1"
PBP_COLUMNS = (
    "game_id", "week", "season_type", "posteam", "defteam", "play_type", "epa",
    "interception", "fumble", "fumble_lost", "penalty_team", "penalty_yards",
    "sack", "qb_dropback", "pass_attempt", "rush_attempt", "td_team", "touchdown",
    "drive", "yardline_100",
)
KICK_PLAYS = ("kickoff", "punt", "field_goal", "extra_point")

# (feature name, numerator column, denominator column); diff = home - away.
RATIOS = (
    ("to_give", "giveaways", "off_plays"),
    ("takeaway", "takeaways", "def_plays"),
    ("fum_lost", "fumbles_lost", "fumbles"),
    ("sack_taken", "sacks_taken", "dropbacks"),
    ("pen_yds", "penalty_yards", "games"),
    ("st_epa", "net_st_epa", "games"),
    ("nonoff_td", "nonoff_tds", "games"),
    ("start_pos", "start_yardline_sum", "drives"),
)
FEATURES = tuple(f"cx_{name}_diff" for name, _, _ in RATIOS)

FEATURE_SETS = (
    ("qb_stack", SHRUNK_CHANGE),
    ("plus_context", SHRUNK_CHANGE + FEATURES),
    ("plus_luck_only", SHRUNK_CHANGE + tuple(
        f"cx_{n}_diff" for n in ("to_give", "takeaway", "fum_lost", "nonoff_td"))),
)


def team_game_context(pbp) -> list[dict[str, Any]]:
    """Per (game, team) counts from one season's play-by-play frame."""
    pbp = pbp[pbp["season_type"] == "REG"]
    games: dict[tuple[str, str], dict[str, float]] = {}

    def row(game_id: str, team: str, week: int) -> dict[str, float]:
        key = (game_id, canon_team(team))
        if key not in games:
            games[key] = {k: 0.0 for k in (
                "off_plays", "def_plays", "giveaways", "takeaways", "fumbles", "fumbles_lost",
                "sacks_taken", "dropbacks", "penalty_yards", "net_st_epa", "nonoff_tds",
                "start_yardline_sum", "drives", "games")}
            games[key].update(game_id=game_id, team=canon_team(team), week=week, games=1.0)
        return games[key]

    scrimmage = pbp[pbp["play_type"].isin(("pass", "run")) & pbp["posteam"].notna()]
    for (gid, week, off, dfn), g in scrimmage.groupby(["game_id", "week", "posteam", "defteam"]):
        o, d = row(gid, off, int(week)), row(gid, dfn, int(week))
        n = float(len(g))
        o["off_plays"] += n
        d["def_plays"] += n
        giveaways = float(((g["interception"] == 1) | (g["fumble_lost"] == 1)).sum())
        o["giveaways"] += giveaways
        d["takeaways"] += giveaways
        o["fumbles"] += float((g["fumble"] == 1).sum())
        o["fumbles_lost"] += float((g["fumble_lost"] == 1).sum())
        o["sacks_taken"] += float((g["sack"] == 1).sum())
        o["dropbacks"] += float((g["qb_dropback"] == 1).sum())
        firsts = g.sort_values("drive").drop_duplicates("drive")
        o["start_yardline_sum"] += float(firsts["yardline_100"].dropna().sum())
        o["drives"] += float(firsts["yardline_100"].notna().sum())
        scored = g[(g["touchdown"] == 1) & (g["td_team"] == dfn)]
        d["nonoff_tds"] += float(len(scored))

    kicks = pbp[pbp["play_type"].isin(KICK_PLAYS) & pbp["posteam"].notna()]
    for (gid, week, off, dfn), g in kicks.groupby(["game_id", "week", "posteam", "defteam"]):
        epa = float(g["epa"].fillna(0.0).sum())
        row(gid, off, int(week))["net_st_epa"] += epa
        row(gid, dfn, int(week))["net_st_epa"] -= epa
        scored = g[(g["touchdown"] == 1) & (g["td_team"] == dfn)]
        row(gid, dfn, int(week))["nonoff_tds"] += float(len(scored))

    pens = pbp[pbp["penalty_team"].notna()]
    for (gid, week, team), g in pens.groupby(["game_id", "week", "penalty_team"]):
        row(gid, team, int(week))["penalty_yards"] += float(g["penalty_yards"].fillna(0.0).sum())
    return list(games.values())


def _load_context(cache: Path, start_season: int, end_season: int):
    import pandas as pd
    out = []
    for season in range(int(start_season), int(end_season) + 1):
        path = cache / f"pbp_{season}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(path, columns=list(PBP_COLUMNS))
        for r in team_game_context(frame):
            r["season"] = season
            out.append(r)
    return out


def pregame_snapshots(context_rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    """(game_id, team) -> decayed trailing ratio per RATIOS, whole-week batches."""
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for r in context_rows:
        by_week[(int(r["season"]), int(r["week"]))].append(r)
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    out: dict[tuple[str, str], dict[str, float]] = {}
    for season, week in sorted(by_week):
        current = by_week[(season, week)]
        for r in current:
            recs = history[r["team"]]
            if not recs:
                continue
            snap = {}
            for name, num, den in RATIOS:
                n = d = 0.0
                for h in recs:
                    w = STATE_SEASON_DECAY ** max(0, season - int(h["season"]))
                    n += w * h[num]
                    d += w * h[den]
                if d <= 0:
                    break
                snap[name] = n / d
            else:
                out[(str(r["game_id"]), r["team"])] = snap
        for r in current:
            history[r["team"]].append(r)
    return out


def add_context(rows: list[dict[str, Any]], context_rows: list[dict[str, Any]]) -> None:
    snaps = pregame_snapshots(context_rows)
    for r in rows:
        home = snaps.get((str(r["game_id"]), canon_team(r.get("home_team"))))
        away = snaps.get((str(r["game_id"]), canon_team(r.get("away_team"))))
        if not home or not away:
            continue
        for name, _, _ in RATIOS:
            r[f"cx_{name}_diff"] = home[name] - away[name]


def report(repository: NFLRepository, *, start_season=2010, end_season=2025,
           pbp_cache: str | Path = "instance/nflverse_raw"):
    rows = build_rows(repository, start_season, end_season)
    add_context(rows, _load_context(Path(pbp_cache), start_season, end_season))
    needed = set().union(*(set(f) for _, f in FEATURE_SETS))
    sample = [r for r in rows if r.get("actual_margin") is not None
              and all(r.get(k) is not None for k in needed)]
    labels = [l for l, _ in FEATURE_SETS]

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
        folds.append({
            "season": season, "test_games": len(test),
            "models": {l: _summary(test, f"pred_{l}") for l in labels},
            "plus_context_vs_stack_mae_diff": round(
                _summary(test, "pred_plus_context")["mae"] - _summary(test, "pred_qb_stack")["mae"], 4),
        })
    return {
        "version": MODEL_VERSION,
        "market_used": False,
        "features": list(FEATURES),
        "raw_games": len(rows),
        "common_games": len(sample),
        "pooled": {l: _summary(pooled, f"pred_{l}") for l in labels},
        "paired_vs_qb_stack": {l: _paired(pooled, "pred_qb_stack", f"pred_{l}") for l in labels[1:]},
        "seasons_improved": sum(f["plus_context_vs_stack_mae_diff"] < 0 for f in folds),
        "seasons_tested": len(folds),
        "walk_forward": folds,
    }
