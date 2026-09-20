"""Leak-safe early-season Margin Power research.

Keeps frozen same-season Margin Power unchanged. Tests cold-start priors for
Weeks 1-4 and a Bayesian transition toward current-season SRS.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import conditional_convergence as cc
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb import narrative_shapes_v2 as v2
from sports_aggregator.cfb.repository import CFBRepository

PRIOR_SRS_RETAIN = 0.70
SRS_ELO_PRIOR_WEIGHT = 0.70
ELO_PRIOR_WEIGHT = 0.30
PRIOR_EFFECTIVE_GAMES = 2.0
EARLY_WEEKS = (1, 2, 3, 4)


def _game_rows(repository: CFBRepository, from_season: int, to_season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        return [dict(r) for r in connection.execute(
            """SELECT game_id,season,week,start_date,home_team,away_team,
                      home_points,away_points,home_pregame_elo,away_pregame_elo
               FROM games
               WHERE season BETWEEN ? AND ?
                 AND home_points IS NOT NULL AND away_points IS NOT NULL
               ORDER BY season,week,start_date,game_id""",
            (int(from_season), int(to_season)),
        )]


def _final_srs_by_season(games: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for game in games:
        by_season[int(game["season"])].append(game)
    return {
        season: ipl._solve_srs(rows)[0]
        for season, rows in by_season.items()
    }


def _elo_margin(game: dict[str, Any]) -> float | None:
    home = game.get("home_pregame_elo")
    away = game.get("away_pregame_elo")
    if home is None or away is None:
        return None
    return (float(home) - float(away)) / 25.0 + ipl.HFA_POINTS


def _prior_srs_margin(game: dict[str, Any], final_srs: dict[int, dict[str, float]]) -> float | None:
    prior = final_srs.get(int(game["season"]) - 1)
    if not prior:
        return None
    home = prior.get(str(game["home_team"]))
    away = prior.get(str(game["away_team"]))
    if home is None or away is None:
        return None
    return PRIOR_SRS_RETAIN * (float(home) - float(away)) + ipl.HFA_POINTS


def _assisted_prior(game: dict[str, Any], final_srs: dict[int, dict[str, float]]) -> float | None:
    srs = _prior_srs_margin(game, final_srs)
    elo = _elo_margin(game)
    values = []
    if srs is not None:
        values.append((SRS_ELO_PRIOR_WEIGHT, srs))
    if elo is not None:
        values.append((ELO_PRIOR_WEIGHT, elo))
    if not values:
        return None
    total_weight = sum(w for w, _ in values)
    return sum(w * value for w, value in values) / total_weight


def _early_rows(repository: CFBRepository, from_season: int, to_season: int) -> list[dict[str, Any]]:
    games = _game_rows(repository, from_season - 1, to_season)
    final_srs = _final_srs_by_season(games)
    target = [g for g in games if int(from_season) <= int(g["season"]) <= int(to_season)]
    narrative = {
        int(r["game_id"]): r
        for r in v2._load_rows(repository)
        if r["side"] == "home"
    }

    out: list[dict[str, Any]] = []
    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for game in target:
        by_season[int(game["season"])].append(game)

    for season in sorted(by_season):
        prior_games: list[dict[str, Any]] = []
        counts: dict[str, int] = defaultdict(int)
        for week in sorted({int(g["week"]) for g in by_season[season] if g.get("week") is not None}):
            current = [g for g in by_season[season] if int(g["week"]) == week]
            current_ratings, _ = ipl._solve_srs(prior_games)
            for game in current:
                if week not in EARLY_WEEKS:
                    continue
                nrow = narrative.get(int(game["game_id"]))
                if not nrow or nrow.get("market_expected_margin") is None:
                    continue
                home, away = str(game["home_team"]), str(game["away_team"])
                n = min(counts.get(home, 0), counts.get(away, 0))
                current_margin = None
                if home in current_ratings and away in current_ratings:
                    current_margin = (
                        float(current_ratings[home]) - float(current_ratings[away])
                        + ipl.HFA_POINTS
                    )
                prior_srs = _prior_srs_margin(game, final_srs)
                assisted = _assisted_prior(game, final_srs)
                bayesian = assisted
                if assisted is not None and current_margin is not None and n > 0:
                    current_weight = float(n) / (float(n) + PRIOR_EFFECTIVE_GAMES)
                    bayesian = current_weight * current_margin + (1.0 - current_weight) * assisted

                actual = float(game["home_points"]) - float(game["away_points"])
                market = float(nrow["market_expected_margin"])
                out.append({
                    "game_id": int(game["game_id"]),
                    "season": season,
                    "week": week,
                    "home_team": home,
                    "away_team": away,
                    "prior_games_min": n,
                    "actual_home_margin": actual,
                    "market_home_margin": market,
                    "standard_current_srs_margin": (
                        current_margin if n >= ipl.MIN_SRS_GAMES else None
                    ),
                    "prior_srs_margin": prior_srs,
                    "elo_margin": _elo_margin(game),
                    "assisted_prior_margin": assisted,
                    "bayesian_margin": bayesian,
                })
            for game in current:
                counts[str(game["home_team"])] += 1
                counts[str(game["away_team"])] += 1
            prior_games.extend(current)
    return out


def _forecast_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    available = [r for r in rows if r.get(key) is not None]
    if not available:
        return {"n": 0}
    errors = [float(r[key]) - float(r["actual_home_margin"]) for r in available]
    edges = [float(r[key]) - float(r["market_home_margin"]) for r in available]
    aligned = [
        (float(r["actual_home_margin"]) - float(r["market_home_margin"]))
        * (1 if edge > 0 else -1)
        for r, edge in zip(available, edges)
        if edge != 0
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


def _convergence_summary(repository: CFBRepository, early: list[dict[str, Any]],
                         key: str, target_season: int) -> dict[str, Any]:
    lens_rows = ipl.build_lens_rows(repository, test_season=int(target_season))
    lens = {int(r["game_id"]): r for r in lens_rows}
    train = [r for r in lens_rows if int(r["season"]) < int(target_season)]
    scales = cc._lens_scales(train)
    historical_mp = float(scales.get("margin_power_edge", 1.0))
    selected = []
    for row in early:
        if int(row["season"]) != int(target_season) or row.get(key) is None:
            continue
        base = lens.get(int(row["game_id"]))
        if not base:
            continue
        edge = float(row[key]) - float(row["market_home_margin"])
        direction = 1 if edge > 0 else -1 if edge < 0 else 0
        if not direction or abs(edge / historical_mp) < 1.0:
            continue
        structural = []
        for skey in cc.STRUCTURAL_KEYS:
            if skey in scales and base.get(skey) is not None:
                structural.append(float(base[skey]) / float(scales[skey]))
        if len(structural) < cc.MIN_STRUCTURAL_COMPONENTS:
            continue
        structural_z = sum(structural) / len(structural)
        line_z = (
            float(base["line_elo_edge"]) / float(scales["line_elo_edge"])
            if "line_elo_edge" in scales and base.get("line_elo_edge") is not None
            else None
        )
        if not (structural_z * direction > 0 and line_z is not None and line_z * direction > 0):
            continue
        residual = (
            float(row["actual_home_margin"]) - float(row["market_home_margin"])
        ) * direction
        selected.append(residual)
    if not selected:
        return {"n": 0}
    wins = sum(v > 0 for v in selected)
    losses = sum(v < 0 for v in selected)
    pushes = len(selected) - wins - losses
    return {
        "n": len(selected),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_ex_pushes": round(wins / (wins + losses), 4) if wins + losses else None,
        "mean_aligned_residual": round(sum(selected) / len(selected), 3),
    }


def report(repository: CFBRepository, *, from_season: int = 2022,
           to_season: int = 2025) -> dict[str, Any]:
    rows = _early_rows(repository, int(from_season), int(to_season))
    variants = (
        "standard_current_srs_margin",
        "prior_srs_margin",
        "assisted_prior_margin",
        "bayesian_margin",
    )
    by_week = {}
    for week in EARLY_WEEKS:
        subset = [r for r in rows if int(r["week"]) == week]
        by_week[str(week)] = {
            key: _forecast_summary(subset, key) for key in variants
        }
    convergence = {}
    for season in range(int(from_season), int(to_season) + 1):
        convergence[str(season)] = {
            key: _convergence_summary(repository, rows, key, season)
            for key in ("prior_srs_margin", "assisted_prior_margin", "bayesian_margin")
        }
    return {
        "version": "early-margin-power-v1",
        "window": [int(from_season), int(to_season)],
        "weeks": list(EARLY_WEEKS),
        "parameters": {
            "prior_srs_retain": PRIOR_SRS_RETAIN,
            "srs_elo_prior_weight": SRS_ELO_PRIOR_WEIGHT,
            "elo_prior_weight": ELO_PRIOR_WEIGHT,
            "prior_effective_games": PRIOR_EFFECTIVE_GAMES,
            "standard_margin_power_unchanged": True,
        },
        "overall": {key: _forecast_summary(rows, key) for key in variants},
        "by_week": by_week,
        "early_convergence_by_season": convergence,
        "notes": [
            "All target-game features are pregame and use only prior completed games.",
            "Prior-year SRS is regressed 30% toward zero before carryover.",
            "Elo-assisted prior blends 70% regressed prior-year SRS with 30% pregame Elo margin.",
            "Bayesian variant uses a two-game effective prior and transitions toward current-season SRS.",
            "Frozen same-season Margin Power and Full Convergence definitions are not modified.",
        ],
    }
