"""Travel, time-zone and body-clock ablation for the calibrated margin.

The market gap is largest on Thursday/Monday games (market_gap), where short
weeks, travel and kickoff time matter most. This adds, for the visiting team only
(the home team never travels):

  tr_log_miles   log(1 + great-circle miles from its home stadium to the game)
  tr_tz_shift    hours east it is travelling (game longitude - home longitude)/15
  tr_early_clock 1 if kickoff is before noon on the visitor's body clock
  tr_altitude    1 if the game is at Denver (high altitude)

Coordinates come from weather_history's curated stadium map; a team's home base
for a season is the stadium it most often hosted at that season. Everything is
known before kickoff. No market inputs; walk-forward by season on the QB stack.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import math
from typing import Any

from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict
from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, _paired, _summary, build_rows
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.weather import kickoff_utc
from sports_aggregator.nfl.weather_history import venue

MODEL_VERSION = "nfl-travel-ablation-v1"
TRAVEL = ("tr_log_miles", "tr_tz_shift", "tr_early_clock", "tr_altitude")
HIGH_ALTITUDE = frozenset({"Empower Field at Mile High", "Sports Authority Field at Mile High"})
FEATURE_SETS = (
    ("qb_stack", SHRUNK_CHANGE),
    ("plus_travel", SHRUNK_CHANGE + TRAVEL),
)


def _miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi, dlmb = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _home_bases(games: list[dict[str, Any]]) -> dict[tuple[int, str], tuple[float, float]]:
    hosted: dict[tuple[int, str], Counter] = defaultdict(Counter)
    for g in games:
        v = venue(g["stadium"])
        if v:
            hosted[(int(g["season"]), g["home_team"])][(v[0], v[1])] += 1
    return {k: c.most_common(1)[0][0] for k, c in hosted.items()}


def add_travel(rows: list[dict[str, Any]], repository: NFLRepository) -> None:
    with closing(repository._connect()) as connection:
        games = [dict(g) for g in connection.execute(
            """SELECT game_id,season,game_date,game_time,home_team,away_team,stadium
               FROM games WHERE stadium IS NOT NULL""")]
    bases = _home_bases(games)
    by_id = {str(g["game_id"]): g for g in games}
    for r in rows:
        g = by_id.get(str(r["game_id"]))
        if g is None:
            continue
        site, base = venue(g["stadium"]), bases.get((int(g["season"]), g["away_team"]))
        kick = kickoff_utc(g)
        if not site or not base or not kick:
            continue
        hour_utc = int(kick[11:13]) + int(kick[14:16]) / 60.0
        body_clock = (hour_utc + base[1] / 15.0) % 24.0
        r["tr_log_miles"] = math.log1p(_miles(base, (site[0], site[1])))
        r["tr_tz_shift"] = (site[1] - base[1]) / 15.0
        r["tr_early_clock"] = 1.0 if body_clock < 12.0 else 0.0
        r["tr_altitude"] = 1.0 if g["stadium"] in HIGH_ALTITUDE and g["away_team"] != "DEN" else 0.0
        r["short_week_slot"] = g.get("game_date")  # kept for slicing below


def _weekday(repository: NFLRepository) -> dict[str, str]:
    with closing(repository._connect()) as connection:
        return {str(r["game_id"]): r["weekday"] for r in connection.execute("SELECT game_id,weekday FROM games")}


def report(repository: NFLRepository, *, start_season=2013, end_season=2025):
    rows = build_rows(repository, start_season, end_season)
    add_travel(rows, repository)
    days = _weekday(repository)
    needed = set().union(*(set(f) for _, f in FEATURE_SETS))
    sample = [r for r in rows if r.get("actual_margin") is not None
              and all(r.get(k) is not None for k in needed)]
    labels = [l for l, _ in FEATURE_SETS]
    pooled, folds = [], []
    for season in sorted({int(r["season"]) for r in sample}):
        train = [r for r in sample if int(r["season"]) < season]
        test = [dict(r) for r in sample if int(r["season"]) == season]
        models = {l: _fit(train, f) for l, f in FEATURE_SETS}
        if any(m is None for m in models.values()) or not test:
            continue
        for r in test:
            for l, m in models.items():
                r[f"pred_{l}"] = _predict(m, r)
        pooled.extend(test)
        folds.append({"season": season,
                      "mae_diff": round(_summary(test, "pred_plus_travel")["mae"] - _summary(test, "pred_qb_stack")["mae"], 4)})
    slots = {"thu_mon": [r for r in pooled if days.get(str(r["game_id"])) in ("Thursday", "Monday")],
             "sunday_other": [r for r in pooled if days.get(str(r["game_id"])) not in ("Thursday", "Monday")],
             "travelling_3tz": [r for r in pooled if abs(r["tr_tz_shift"]) >= 2.5],
             "early_body_clock": [r for r in pooled if r["tr_early_clock"]]}
    return {
        "version": MODEL_VERSION, "market_used": False, "features": list(TRAVEL),
        "games": len(pooled),
        "pooled": {l: _summary(pooled, f"pred_{l}") for l in labels},
        "paired_vs_qb_stack": _paired(pooled, "pred_qb_stack", "pred_plus_travel"),
        "seasons_improved": f"{sum(f['mae_diff'] < 0 for f in folds)}/{len(folds)}",
        "slices": {k: {"n": len(v), "qb_stack": _summary(v, "pred_qb_stack").get("mae"),
                       "plus_travel": _summary(v, "pred_plus_travel").get("mae"),
                       "paired": _paired(v, "pred_qb_stack", "pred_plus_travel")} for k, v in slots.items()},
        "walk_forward": folds,
    }
