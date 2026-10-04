"""Past games that were shaped like this one.

A game's shape is what the two teams' opponent-adjusted ratings say it will look like before kickoff: the expected
total, how close it should be, how fast and how pass-heavy it should play, how many big plays, and how much more
efficient the air game should be than the ground game. Every regular-season game since 2010 gets that vector from the
ratings as they stood the week it was played (team_styles, earlier games only). The comparables for a game are its
nearest neighbours among earlier seasons, by standardised distance.

This is context for the matchup: how games like this actually played out against the number. Whether a neighbourhood's
record predicts anything is a separate question, answered by `evaluate`; the panel says what that found.
"""
from __future__ import annotations

from contextlib import closing
import math
from typing import Any

import numpy as np

from sports_aggregator.nfl import team_styles
from sports_aggregator.nfl.model_cache import history_cached
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-comparable-games-v1"
FEATURES = ("total", "closeness", "pace", "pass_rate", "explosive", "air_edge")
LABELS = {"total": "Expected total", "closeness": "Expected margin", "pace": "Seconds / play",
          "pass_rate": "Neutral pass rate", "explosive": "Explosive rate", "air_edge": "Pass over rush EPA"}
DEFAULT_K = 8


def shape(snap: dict[str, dict[str, Any]], home: str, away: str, neutral: bool) -> dict[str, float] | None:
    """The pregame shape of one game from a ratings snapshot, or None if either team is unrated."""
    if not snap or any(t not in snap["points"]["off"] for t in (home, away)):
        return None

    def expect(metric: str, off: str, dfn: str, col: float = 0.0) -> float:
        r = snap[metric]
        return r["mu"] + r["home"] * col + r["off"][off] + r["def"][dfn]

    h_col = 0.0 if neutral else 0.5
    home_pts, away_pts = expect("points", home, away, h_col), expect("points", away, home, -h_col)
    both = lambda metric: (expect(metric, home, away) + expect(metric, away, home)) / 2.0
    return {
        "total": home_pts + away_pts, "margin": home_pts - away_pts, "closeness": abs(home_pts - away_pts),
        "pace": both("pace"), "pass_rate": both("pass_rate"), "explosive": both("explosive"),
        "air_edge": both("pass_epa") - both("rush_epa"),
        "home_points": home_pts, "away_points": away_pts,
    }


@history_cached("comparable-vectors", roots=("team_styles",))
def history_vectors(repository: NFLRepository, start_season: int, end_season: int) -> list[dict[str, Any]]:
    """One row per played, rated regular-season game: its pregame shape and what happened."""
    rows = team_styles.load_observations(repository, max(2010, start_season - 1), end_season)
    snaps = team_styles.snapshots(rows, start_season)
    with closing(repository._connect()) as connection:
        games = [dict(r) for r in connection.execute(
            """SELECT game_id,season,week,away_team,home_team,home_score,away_score,spread_line,total_line,neutral_site
               FROM games WHERE season BETWEEN ? AND ? AND season_type='REG' AND completed=1
                 AND home_score IS NOT NULL AND away_score IS NOT NULL ORDER BY season,week,game_id""",
            (int(start_season), int(end_season)))]
    out = []
    for g in games:
        from sports_aggregator.nfl.naming import canon_team
        home, away = canon_team(g["home_team"]), canon_team(g["away_team"])
        s = shape(snaps.get((g["season"], g["week"])), home, away, bool(g["neutral_site"]))
        if s is None:
            continue
        out.append({"game_id": g["game_id"], "season": g["season"], "week": g["week"], "home": home, "away": away,
                    "home_score": g["home_score"], "away_score": g["away_score"],
                    "spread": g["spread_line"], "total_line": g["total_line"], "shape": s})
    return out


def _matrix(rows: list[dict[str, Any]]) -> np.ndarray:
    return np.array([[r["shape"][f] for f in FEATURES] for r in rows], dtype=float)


def nearest(target: dict[str, float], pool: list[dict[str, Any]], k: int = DEFAULT_K) -> list[dict[str, Any]]:
    """The k pool games closest to `target` in standardised shape space, each with its distance."""
    if not pool:
        return []
    matrix = _matrix(pool)
    spread = matrix.std(axis=0)
    spread[spread == 0] = 1.0
    point = np.array([target[f] for f in FEATURES], dtype=float)
    distance = np.sqrt((((matrix - point) / spread) ** 2).sum(axis=1))
    order = np.argsort(distance)[:k]
    return [{**pool[i], "distance": float(distance[i])} for i in order]


def outcome(row: dict[str, Any]) -> dict[str, Any]:
    """What happened in a past game against its own number, from the favourite's side."""
    margin = row["home_score"] - row["away_score"]
    total = row["home_score"] + row["away_score"]
    spread, line = row.get("spread"), row.get("total_line")
    favourite_home = spread is not None and spread > 0
    fav_margin = None if spread is None or spread == 0 else (margin if favourite_home else -margin)
    fav_cover = None
    if spread not in (None, 0) and margin != spread:
        fav_cover = (margin > spread) if favourite_home else (margin < spread)
    return {"margin": margin, "total": total, "fav_margin": fav_margin, "fav_cover": fav_cover,
            "over": None if line is None or total == line else total > line,
            "total_miss": None if line is None else total - line}


def summary(comps: list[dict[str, Any]], expected_total: float | None) -> dict[str, Any]:
    results = [outcome(c) for c in comps]
    totals = [r["total"] for r in results]
    covers = [r["fav_cover"] for r in results if r["fav_cover"] is not None]
    overs = [r["over"] for r in results if r["over"] is not None]
    misses = [r["total_miss"] for r in results if r["total_miss"] is not None]
    return {
        "n": len(comps), "avg_total": float(np.mean(totals)) if totals else None,
        "avg_expected_total": float(np.mean([c["shape"]["total"] for c in comps])) if comps else None,
        "avg_total_miss": float(np.mean(misses)) if misses else None,
        "overs": f"{sum(overs)}-{len(overs) - sum(overs)}" if overs else None,
        "fav_covers": f"{sum(covers)}-{len(covers) - sum(covers)}" if covers else None,
        "fav_cover_n": len(covers),
        "avg_margin": float(np.mean([abs(r["margin"]) for r in results])) if results else None,
        "one_score": sum(1 for r in results if abs(r["margin"]) <= 8), "target_total": expected_total,
    }


def packet(repository: NFLRepository, game: dict[str, Any], k: int = DEFAULT_K) -> dict[str, Any]:
    """Panel data for one game: its shape and the closest earlier-season games."""
    from sports_aggregator.nfl.naming import canon_team
    season = int(game["season"])
    snap = team_styles.snapshot_for(repository, season, int(game["week"]))
    target = shape(snap, canon_team(game["home_team"]), canon_team(game["away_team"]), bool(game.get("neutral_site")))
    if target is None:
        return {"available": False, "reason": "Comparable games need style ratings for both teams."}
    pool = history_vectors(repository, 2011, season - 1) if season - 1 >= 2011 else []
    if len(pool) < 50:
        return {"available": False, "reason": "Comparable games need at least a few earlier seasons of rated games."}
    comps = nearest(target, pool, k)
    return {"available": True, "target": target, "labels": LABELS, "features": FEATURES, "pool": len(pool),
            "comps": [{**c, "outcome": outcome(c)} for c in comps], "summary": summary(comps, target["total"])}


def evaluate(repository: NFLRepository, first_target: int = 2016, last_target: int = 2025, k: int = 25) -> dict[str, Any]:
    """Does a neighbourhood's record predict the target game against its own number? Strictly earlier seasons only."""
    history = history_vectors(repository, 2011, last_target)
    xs_total, ys_total, xs_cover, ys_cover = [], [], [], []
    by_season: dict[int, list[dict[str, Any]]] = {}
    for r in history:
        by_season.setdefault(r["season"], []).append(r)
    for season in range(first_target, last_target + 1):
        pool = [r for r in history if r["season"] < season]
        for target in by_season.get(season, []):
            if target["total_line"] is None or target["spread"] in (None, 0):
                continue
            comps = [c for c in nearest(target["shape"], pool, k) if c["total_line"] is not None]
            if not comps:
                continue
            xs_total.append(float(np.mean([outcome(c)["total_miss"] for c in comps])))
            ys_total.append(outcome(target)["total_miss"])
            covers = [outcome(c)["fav_cover"] for c in comps if outcome(c)["fav_cover"] is not None]
            tgt = outcome(target)["fav_cover"]
            if covers and tgt is not None:
                xs_cover.append(float(np.mean(covers)) - 0.5)
                ys_cover.append(1.0 if tgt else 0.0)

    def slope(x, y):
        x, y = np.array(x), np.array(y)
        xc = x - x.mean()
        b = float(xc @ (y - y.mean()) / (xc @ xc))
        resid = y - y.mean() - b * xc
        se = float(resid.std(ddof=2) / math.sqrt(xc @ xc))
        return {"n": len(x), "slope": round(b, 3), "se": round(se, 3), "t": round(b / se, 2)}
    return {"k": k, "total_miss": slope(xs_total, ys_total), "fav_cover": slope(xs_cover, ys_cover)}
