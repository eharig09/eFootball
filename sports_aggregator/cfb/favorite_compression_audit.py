"""Audit whether Football Lab systematically compresses market favorites.

Uses stored leak-safe projection backtest rows and consensus market spread.
This is diagnostic only; it does not alter any projection or signal rule.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    favorite_cover = sum(r["model_relation"] == "favorite_more_favored" for r in rows)
    dog_closer = sum(r["model_relation"] == "underdog_closer" for r in rows)
    flipped = sum(r["model_relation"] == "favorite_flipped" for r in rows)
    equal = sum(r["model_relation"] == "same_margin" for r in rows)
    compression = [float(r["favorite_margin_change"]) for r in rows]
    return {
        "n": len(rows),
        "favorite_more_favored": favorite_cover,
        "favorite_more_favored_rate": round(favorite_cover / len(rows), 4),
        "underdog_closer": dog_closer,
        "underdog_closer_rate": round(dog_closer / len(rows), 4),
        "favorite_flipped": flipped,
        "favorite_flipped_rate": round(flipped / len(rows), 4),
        "same_margin": equal,
        "mean_favorite_margin_change": round(sum(compression) / len(compression), 3),
        "median_favorite_margin_change": round(
            sorted(compression)[len(compression)//2]
            if len(compression) % 2
            else (sorted(compression)[len(compression)//2-1] + sorted(compression)[len(compression)//2]) / 2,
            3,
        ),
        "mean_market_abs_spread": round(sum(r["market_abs_spread"] for r in rows) / len(rows), 3),
        "mean_model_favorite_margin": round(sum(r["model_margin_for_market_favorite"] for r in rows) / len(rows), 3),
    }


def _bucket(value: float) -> str:
    if value < 3:
        return "<3"
    if value < 7:
        return "3-6.5"
    if value < 14:
        return "7-13.5"
    return "14+"


def report(repository, *, from_season: int = 2021, to_season: int = 2026) -> dict[str, Any]:
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT p.game_id,p.season,p.side,p.projected_offensive_points,
                      g.week,g.home_team,g.away_team,g.home_points,g.away_points,
                      (SELECT AVG(gl.spread) FROM game_lines gl
                       WHERE gl.game_id=p.game_id AND gl.spread IS NOT NULL) AS market_spread
               FROM cfb_projection_backtest p
               JOIN games g USING(game_id)
               WHERE p.backtest_version=?
                 AND p.season BETWEEN ? AND ?
                 AND p.projected_offensive_points IS NOT NULL
               ORDER BY p.season,g.week,p.game_id,p.side""",
            (BACKTEST_VERSION, int(from_season), int(to_season)),
        )]

    grouped: dict[int, dict[str, Any]] = defaultdict(dict)
    meta: dict[int, dict[str, Any]] = {}
    for row in rows:
        gid = int(row["game_id"])
        grouped[gid][str(row["side"])] = float(row["projected_offensive_points"])
        meta[gid] = row

    games = []
    for gid, sides in grouped.items():
        if "home" not in sides or "away" not in sides:
            continue
        row = meta[gid]
        if row.get("market_spread") is None:
            continue
        market_spread = float(row["market_spread"])
        if market_spread == 0:
            continue

        # game_lines.spread is home-team spread: negative = home favorite.
        market_home_margin = -market_spread
        model_home_margin = float(sides["home"]) - float(sides["away"])
        market_favorite_side = "home" if market_home_margin > 0 else "away"
        market_abs = abs(market_home_margin)
        model_for_favorite = (
            model_home_margin if market_favorite_side == "home" else -model_home_margin
        )
        change = model_for_favorite - market_abs

        if model_for_favorite < 0:
            relation = "favorite_flipped"
        elif change > 0.05:
            relation = "favorite_more_favored"
        elif change < -0.05:
            relation = "underdog_closer"
        else:
            relation = "same_margin"

        games.append({
            "game_id": gid,
            "season": int(row["season"]),
            "week": int(row["week"]) if row.get("week") is not None else None,
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "market_favorite_side": market_favorite_side,
            "market_favorite": row["home_team"] if market_favorite_side == "home" else row["away_team"],
            "market_abs_spread": market_abs,
            "model_home_margin": round(model_home_margin, 3),
            "model_margin_for_market_favorite": round(model_for_favorite, 3),
            "favorite_margin_change": round(change, 3),
            "model_relation": relation,
            "spread_bucket": _bucket(market_abs),
        })

    by_season = []
    for season in sorted({r["season"] for r in games}):
        by_season.append({"season": season, **_summary([r for r in games if r["season"] == season])})

    by_bucket = []
    for bucket in ("<3", "3-6.5", "7-13.5", "14+"):
        by_bucket.append({"bucket": bucket, **_summary([r for r in games if r["spread_bucket"] == bucket])})

    # Keep a compact set of the most severe compressions and rare favorite-cover calls.
    most_compressed = sorted(games, key=lambda r: r["favorite_margin_change"])[:30]
    favorite_cover_examples = sorted(
        [r for r in games if r["model_relation"] == "favorite_more_favored"],
        key=lambda r: r["favorite_margin_change"],
        reverse=True,
    )[:30]

    return {
        "version": "football-lab-favorite-compression-audit-v1",
        "projection_version": BACKTEST_VERSION,
        "from_season": int(from_season),
        "to_season": int(to_season),
        "definition": {
            "favorite_more_favored": "Football Lab margin for the market favorite exceeds the market margin.",
            "underdog_closer": "Football Lab keeps the same favorite but by a smaller margin.",
            "favorite_flipped": "Football Lab makes the market underdog the projected winner.",
        },
        "overall": _summary(games),
        "by_season": by_season,
        "by_market_spread_bucket": by_bucket,
        "most_compressed_examples": most_compressed,
        "favorite_cover_examples": favorite_cover_examples,
    }
