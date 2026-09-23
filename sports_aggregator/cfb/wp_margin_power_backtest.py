"""Does time-weighted win-probability "game control" improve on Margin
Power's raw final-score-margin SRS input?

Margin Power (internal_power_lenses.margin_power_snapshots) rates teams by
solving an SRS on each game's final scoreboard margin, refit week by week
using only completed prior weeks. A final margin is one snapshot of an
entire game's story: a backdoor garbage-time score can shrink a blowout
win's margin, and a last-second give-away can turn a game led wire-to-wire
into a "loss" by a field goal. win_probability_v2.py's
time_weighted_home_win_probability() already turns the play-by-play wp-v2
model into a single number per game -- the fraction of the game's actual
clock time the home team spent favored -- which should be less sensitive to
exactly that kind of late-game noise.

This module tests, leak-safe and walk-forward against the closing market
line using the same evaluation convention internal_power_lenses.py's
margin_power_edge is judged by, whether an SRS built from a WP-derived
margin -- in place of, or blended with, the real scoreboard margin --
predicts FUTURE games better than the production Margin Power does. Nothing
here changes the frozen production Margin Power; it only measures whether
there's a case for it.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.win_probability_v2 import bulk_time_weighted_home_win_probability

#: wp-v2 has essentially full play-by-play coverage from this season on;
#: seasons before it have none (see the 2015-2018 gap in cfb_play_win_probability).
WP_COVERAGE_FROM_SEASON = 2019

#: 0.0 keeps Margin Power's real scoreboard margin untouched (the production
#: baseline); 1.0 replaces it outright with the WP-derived margin. The values
#: between are blends, so any market-beat improvement can be attributed to
#: how much weight the WP signal actually earns, not an all-or-nothing swap.
BLEND_WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)

#: Below this many prior-season calibration games, don't trust the fitted
#: logit-to-margin slope -- matches the spirit of ipl.MIN_SRS_GAMES's own
#: "don't trust the machinery on too little evidence" guard.
MIN_CALIBRATION_GAMES = 30


def _logit(p: float) -> float:
    p = min(0.999, max(0.001, float(p)))
    return math.log(p / (1.0 - p))


def _game_rows(repository: CFBRepository, from_season: int, to_season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        return [dict(r) for r in connection.execute(
            """SELECT game_id,season,week,start_date,home_team,away_team,
                      home_points,away_points
               FROM games
               WHERE season BETWEEN ? AND ?
                 AND home_points IS NOT NULL AND away_points IS NOT NULL
               ORDER BY season,week,start_date,game_id""",
            (int(from_season), int(to_season)))]


def _market_by_game(repository: CFBRepository) -> dict[int, float]:
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT game_id, market_expected_margin FROM cfb_narrative_state
               WHERE side='home' AND market_expected_margin IS NOT NULL""").fetchall()
    return {int(r["game_id"]): float(r["market_expected_margin"]) for r in rows}


def _fit_wp_scale(games: list[dict[str, Any]], avg_wp: dict[int, float]) -> tuple[float, float]:
    """OLS fit of actual home margin on logit(time-weighted avg home WP):
    actual = intercept + slope*logit(wp).

    Called with `games` already filtered to seasons strictly before the one
    being evaluated, so the calibration itself can't leak information about
    the season it's about to be applied to.
    """
    pairs = []
    for game in games:
        wp = avg_wp.get(int(game["game_id"]))
        if wp is None:
            continue
        x = _logit(wp)
        y = float(game["home_points"]) - float(game["away_points"])
        pairs.append((x, y))
    if len(pairs) < MIN_CALIBRATION_GAMES:
        return 0.0, 0.0
    n = len(pairs)
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    variance = sum((x - mean_x) ** 2 for x, _ in pairs)
    slope = covariance / variance if variance else 0.0
    intercept = mean_y - slope * mean_x
    return slope, intercept


def _srs_from_margins(games: list[dict[str, Any]], margins: dict[int, float]
                      ) -> tuple[dict[str, float], dict[str, int]]:
    """Run the same SRS solver Margin Power uses in production, fed a
    caller-chosen per-game margin instead of raw home_points-away_points.
    ipl._solve_srs only ever looks at that difference, so a synthetic
    (margin, 0) pair reuses its exact iteration and HFA-neutralization logic
    unchanged, rather than re-deriving a second solver that could drift out
    of sync with the real one."""
    synthetic = [
        {**game, "home_points": margins[int(game["game_id"])], "away_points": 0.0}
        for game in games if int(game["game_id"]) in margins
    ]
    return ipl._solve_srs(synthetic)


def _weekly_snapshots(games: list[dict[str, Any]], margins: dict[int, float]) -> dict[int, float]:
    """Pregame home-margin projection for each game, refit week by week from
    only completed prior weeks -- mirrors margin_power_snapshots() exactly,
    just against a caller-supplied margin definition instead of the raw
    scoreboard margin."""
    weeks = sorted({int(g["week"]) for g in games if g.get("week") is not None})
    prior: list[dict[str, Any]] = []
    out: dict[int, float] = {}
    for week in weeks:
        current = [g for g in games if int(g["week"]) == week]
        ratings, counts = _srs_from_margins(prior, margins)
        for game in current:
            gid = int(game["game_id"])
            home, away = str(game["home_team"]), str(game["away_team"])
            if counts.get(home, 0) >= ipl.MIN_SRS_GAMES and counts.get(away, 0) >= ipl.MIN_SRS_GAMES:
                out[gid] = ratings[home] - ratings[away] + ipl.HFA_POINTS
        prior.extend(current)
    return out


def _forecast_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    available = [r for r in rows if r.get(key) is not None]
    if not available:
        return {"n": 0}
    errors = [float(r[key]) - float(r["actual_home_margin"]) for r in available]
    edges = [float(r[key]) - float(r["market_home_margin"]) for r in available]
    aligned = [
        (float(r["actual_home_margin"]) - float(r["market_home_margin"])) * (1 if edge > 0 else -1)
        for r, edge in zip(available, edges) if edge != 0
    ]
    return {
        "n": len(available),
        "coverage_rate": round(len(available) / len(rows), 4) if rows else None,
        "mae": round(sum(abs(x) for x in errors) / len(errors), 3),
        "bias": round(sum(errors) / len(errors), 3),
        "market_direction_n": len(aligned),
        "market_direction_hit_rate": (
            round(sum(x > 0 for x in aligned) / len(aligned), 4) if aligned else None
        ),
        "mean_aligned_market_residual": (
            round(sum(aligned) / len(aligned), 3) if aligned else None
        ),
    }


def report(repository: CFBRepository, *, from_season: int = 2020, to_season: int = 2025
          ) -> dict[str, Any]:
    from_season, to_season = int(from_season), int(to_season)
    calib_from = max(WP_COVERAGE_FROM_SEASON, from_season - 5)
    all_games = _game_rows(repository, calib_from, to_season)
    avg_wp = bulk_time_weighted_home_win_probability(
        repository, from_season=calib_from, to_season=to_season)
    market = _market_by_game(repository)

    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for game in all_games:
        by_season[int(game["season"])].append(game)

    rows: list[dict[str, Any]] = []
    per_season: dict[str, Any] = {}
    for season in range(from_season, to_season + 1):
        if season not in by_season:
            continue
        train_games = [g for g in all_games if int(g["season"]) < season]
        slope, intercept = _fit_wp_scale(train_games, avg_wp)
        season_games = by_season[season]

        actual_margin = {
            int(g["game_id"]): float(g["home_points"]) - float(g["away_points"])
            for g in season_games
        }
        wp_margin = {
            int(g["game_id"]): slope * _logit(avg_wp[int(g["game_id"])]) + intercept
            for g in season_games if int(g["game_id"]) in avg_wp
        }

        variant_snapshots = {}
        for weight in BLEND_WEIGHTS:
            if weight == 0.0:
                blended = dict(actual_margin)
            else:
                blended = {
                    gid: (1.0 - weight) * actual_margin[gid] + weight * wp_margin[gid]
                    for gid in actual_margin if gid in wp_margin
                }
            variant_snapshots[weight] = _weekly_snapshots(season_games, blended)

        season_rows = []
        for game in season_games:
            gid = int(game["game_id"])
            m = market.get(gid)
            if m is None:
                continue
            row = {
                "game_id": gid,
                "season": season,
                "week": game.get("week"),
                "actual_home_margin": actual_margin[gid],
                "market_home_margin": m,
                "wp_coverage": gid in wp_margin,
            }
            for weight in BLEND_WEIGHTS:
                row[f"blend_{weight}"] = variant_snapshots[weight].get(gid)
            season_rows.append(row)
        rows.extend(season_rows)

        per_season[str(season)] = {
            "games": len(season_rows),
            "wp_calibration": {
                "slope": round(slope, 3),
                "intercept": round(intercept, 3),
                "train_games": sum(1 for g in train_games if int(g["game_id"]) in avg_wp),
            },
            **{
                f"blend_{weight}": _forecast_summary(season_rows, f"blend_{weight}")
                for weight in BLEND_WEIGHTS
            },
        }

    overall = {
        f"blend_{weight}": _forecast_summary(rows, f"blend_{weight}")
        for weight in BLEND_WEIGHTS
    }
    return {
        "version": "wp-margin-power-backtest-v1",
        "window": [from_season, to_season],
        "blend_weights": list(BLEND_WEIGHTS),
        "overall": overall,
        "by_season": per_season,
        "notes": [
            "blend_0.0 is the production Margin Power baseline: raw scoreboard "
            "margin, HFA-neutralized, week-by-week SRS -- unchanged from "
            "internal_power_lenses.margin_power_snapshots().",
            "blend_1.0 replaces the SRS input entirely with a WP-derived margin: "
            "OLS-calibrated logit(time-weighted average home win probability), "
            "fit on strictly prior seasons only, then run through the identical "
            "SRS solver.",
            "Intermediate weights average the two margins before solving the SRS.",
            "market_direction_hit_rate is the rate the variant's edge over the "
            "closing market line pointed the same way the actual outcome's edge "
            "over that line did -- the same metric internal_power_lenses.py "
            "already uses to judge margin_power_edge and its sibling lenses.",
            "Games without wp-v2 play-by-play coverage (seasons before 2019, or "
            "any game missing charted plays) are excluded from every blend_* "
            "variant above 0.0, both as SRS training evidence and as test rows.",
        ],
    }
