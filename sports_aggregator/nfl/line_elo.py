"""NFL Line Elo: a slow-moving rating of how the market has *priced* each team.

Ported from the college football engine (cfb.narrative_shapes). Every closing spread is treated as a noisy
observation of the gap between the two teams; each rating moves a fraction of the way toward it. The rating is not
a measure of results -- it is the market's own opinion of a team, averaged over its recent pricing.

Two pregame signals fall out of it:

  innovation  today's closing spread minus what the teams' Line Elo ratings imply (positive = the market is more
              bullish on the home team than its own pricing history says it should be)
  momentum    how far the home team's Line Elo has moved over its last three priced games, minus the away team's

NFL teams swap between favourite and underdog far more often than college teams, so this is a cleaner "has the line
drifted away from how this team is normally priced" read than a raw spread.

Leak policy: a game's signals use ratings before any game in its own week is assimilated, and nothing but earlier
closing lines. The outcome (ATS residual = home margin - spread) is never an input. All settings are fixed
before the first look; the sensitivity grid in the report is labelled exploratory.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
import math
from typing import Any

import numpy as np

from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-line-elo-v1"
ELO_PER_POINT = 25.0
HOME_FIELD = 2.0
LEARNING_RATE = 0.35
OFFSEASON_REGRESSION = 1.0 / 3.0
THRESHOLD = 2.0
MIN_PRIOR_LINES = 3
MOMENTUM_GAMES = 3


def _games(repository: NFLRepository, start: int, end: int,
           include_week: tuple[int, int] | None = None) -> list[dict[str, Any]]:
    """Priced regular-season games; `include_week` also admits that week's not-yet-played games (for live flags)."""
    extra = "OR (season=? AND week=?)" if include_week else ""
    parameters: list[Any] = [int(start), int(end)] + ([int(include_week[0]), int(include_week[1])] if include_week else [])
    with closing(repository._connect()) as connection:
        return [dict(r) for r in connection.execute(
            f"""SELECT game_id,season,week,home_team,away_team,home_score,away_score,spread_line,completed,neutral_site
               FROM games WHERE season BETWEEN ? AND ? AND season_type='REG' AND spread_line IS NOT NULL
                 AND ((completed=1 AND home_score IS NOT NULL AND away_score IS NOT NULL) {extra})
               ORDER BY season,week,game_date,game_id""", parameters)]


def build(repository: NFLRepository, start: int = 2010, end: int = 2025, *, learning_rate: float = LEARNING_RATE,
          regression: float = OFFSEASON_REGRESSION, home_field: float = HOME_FIELD,
          include_week: tuple[int, int] | None = None) -> list[dict[str, Any]]:
    """One row per priced game with its pregame Line Elo signals and the ATS outcome (None until played)."""
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for g in _games(repository, start, end, include_week):
        by_week[(g["season"], g["week"])].append(g)
    rating: dict[str, float] = defaultdict(lambda: 1500.0)
    seen: dict[str, int] = defaultdict(int)
    trail: dict[str, deque] = defaultdict(lambda: deque(maxlen=MOMENTUM_GAMES + 1))
    rows: list[dict[str, Any]] = []
    current_season = None

    for season, week in sorted(by_week):
        if season != current_season:
            for team in list(rating):
                rating[team] = 1500.0 + (1.0 - regression) * (rating[team] - 1500.0)
            current_season = season
        batch = by_week[(season, week)]
        for g in batch:
            h, a = g["home_team"], g["away_team"]
            field = 0.0 if g.get("neutral_site") else home_field
            expected = (rating[h] - rating[a]) / ELO_PER_POINT + field
            spread = float(g["spread_line"])
            h_old = trail[h][0] if len(trail[h]) > MOMENTUM_GAMES else None
            a_old = trail[a][0] if len(trail[a]) > MOMENTUM_GAMES else None
            played = g["home_score"] is not None and g["away_score"] is not None
            margin = float(g["home_score"]) - float(g["away_score"]) if played else None
            rows.append({
                "game_id": g["game_id"], "season": season, "week": week, "home_team": h, "away_team": a,
                "spread": spread, "line_elo_expected": expected, "innovation": spread - expected,
                "momentum": ((rating[h] - h_old) - (rating[a] - a_old)) / ELO_PER_POINT
                if h_old is not None and a_old is not None else None,
                "priced_games": min(seen[h], seen[a]), "margin": margin,
                "ats_resid": margin - spread if margin is not None else None,
            })
        for g in batch:                       # assimilate the week's lines only after every game was scored
            if g["home_score"] is None:       # a game still to be played has a line but nothing to learn from yet
                continue
            h, a = g["home_team"], g["away_team"]
            target = ELO_PER_POINT * (float(g["spread_line"]) - (0.0 if g.get("neutral_site") else home_field))
            half = learning_rate * (target - (rating[h] - rating[a])) / 2.0
            rating[h] += half
            rating[a] -= half
            for team in (h, a):
                seen[team] += 1
                trail[team].append(rating[team])
    return rows


def _cover_stats(rows: list[dict[str, Any]], side: str) -> dict[str, Any]:
    """Win rate of backing the home ('home') or away ('away') side against the spread, pushes excluded."""
    decided = [r for r in rows if r["ats_resid"] != 0]
    if not decided:
        return {"n": 0}
    wins = sum((r["ats_resid"] > 0) == (side == "home") for r in decided)
    n = len(decided)
    z = (wins - n / 2) / math.sqrt(n / 4)
    return {"n": n, "win_rate": round(wins / n, 4), "z_vs_50": round(z, 2), "units_at_-110": round(wins - 1.1 * (n - wins), 1)}


def _slope(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """OLS slope of the ATS residual on a signal (points of residual per point of signal) with its t-statistic."""
    pairs = [(r[key], r["ats_resid"]) for r in rows if r.get(key) is not None]
    if len(pairs) < 50:
        return {"n": len(pairs)}
    x = np.asarray([p[0] for p in pairs])
    y = np.asarray([p[1] for p in pairs])
    xc = x - x.mean()
    slope = float((xc * (y - y.mean())).sum() / (xc ** 2).sum())
    resid = y - y.mean() - slope * xc
    se = math.sqrt((resid ** 2).sum() / (len(x) - 2) / (xc ** 2).sum())
    return {"n": len(x), "slope": round(slope, 4), "t": round(slope / se, 2) if se else None}


def _signal_block(rows: list[dict[str, Any]], key: str, threshold: float) -> dict[str, Any]:
    high = [r for r in rows if r.get(key) is not None and r[key] >= threshold]     # market more bullish on home than usual
    low = [r for r in rows if r.get(key) is not None and r[key] <= -threshold]
    return {
        "regression": _slope(rows, key),
        f"{key}_high_back_home": _cover_stats(high, "home"), f"{key}_high_back_away": _cover_stats(high, "away"),
        f"{key}_low_back_home": _cover_stats(low, "home"), f"{key}_low_back_away": _cover_stats(low, "away"),
    }


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    rows = [r for r in build(repository, start_season, end_season)
            if r["season"] > start_season and r["priced_games"] >= MIN_PRIOR_LINES]       # first season is warm-up
    seasons = sorted({r["season"] for r in rows})
    confirm_from = seasons[len(seasons) // 2]
    discovery = [r for r in rows if r["season"] < confirm_from]
    confirmation = [r for r in rows if r["season"] >= confirm_from]
    per_season = {}
    for season in seasons:
        sub = [r for r in rows if r["season"] == season]
        per_season[season] = _slope(sub, "innovation").get("slope")
    signs = [v for v in per_season.values() if v is not None]
    grid = {}
    for lr in (0.2, 0.35, 0.5):
        for reg in (0.0, 1 / 3, 2 / 3):
            sub = [r for r in build(repository, start_season, end_season, learning_rate=lr, regression=reg)
                   if r["season"] > start_season and r["priced_games"] >= MIN_PRIOR_LINES]
            grid[f"lr={lr},regress={reg:.2f}"] = _slope(sub, "innovation").get("t")
    return {
        "version": MODEL_VERSION, "market_used": "closing spreads are the signal's input; outcomes never are",
        "settings": {"learning_rate": LEARNING_RATE, "offseason_regression": round(OFFSEASON_REGRESSION, 3),
                     "home_field": HOME_FIELD, "threshold_points": THRESHOLD, "elo_per_point": ELO_PER_POINT},
        "games": len(rows), "seasons": [seasons[0], seasons[-1]], "confirmation_starts": confirm_from,
        "innovation_summary": {"mean": round(float(np.mean([r["innovation"] for r in rows])), 3),
                               "std": round(float(np.std([r["innovation"] for r in rows])), 3),
                               "share_beyond_threshold": round(float(np.mean([abs(r["innovation"]) >= THRESHOLD for r in rows])), 3)},
        "all_seasons": {"innovation": _signal_block(rows, "innovation", THRESHOLD),
                        "momentum": _signal_block(rows, "momentum", THRESHOLD)},
        "discovery_half": {"innovation": _signal_block(discovery, "innovation", THRESHOLD)},
        "confirmation_half": {"innovation": _signal_block(confirmation, "innovation", THRESHOLD)},
        "innovation_slope_by_season": per_season,
        "seasons_with_negative_slope": f"{sum(v < 0 for v in signs)}/{len(signs)}",
        "exploratory_sensitivity_t_of_innovation_slope": grid,
    }


def flags(repository: NFLRepository, season: int, week: int, *, threshold: float = THRESHOLD) -> list[dict[str, Any]]:
    """This week's games whose line has drifted from how the market has priced the teams, from current lines.

    Research output for forward tracking only. In 2011-2025 the one pattern that held in both halves of the sample was
    a line at least `threshold` points MORE bullish on the home team than its own pricing history implies
    (away side covered 53.3%, z=2.05 -- one of eight bucket looks, so unconfirmed); the opposite drift showed nothing.
    """
    rows = [r for r in build(repository, 2010, int(season), include_week=(int(season), int(week)))
            if r["season"] == int(season) and r["week"] == int(week) and r["margin"] is None
            and r["priced_games"] >= MIN_PRIOR_LINES]
    out = []
    for r in rows:
        drift = r["innovation"]
        if abs(drift) < threshold:
            continue
        out.append({**{k: r[k] for k in ("game_id", "home_team", "away_team", "spread", "line_elo_expected", "innovation")},
                    "momentum": r["momentum"],
                    "read": ("line more bullish on home than its history: lean away (the only pattern that held in both halves)"
                             if drift > 0 else "line cooler on home than its history: no pattern found, informational only")})
    return sorted(out, key=lambda item: -abs(item["innovation"]))
