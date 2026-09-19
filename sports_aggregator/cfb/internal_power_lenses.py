"""Internally produced, leak-safe power-rating lenses for composite research.

These are intentionally OUR ratings, not proxies mislabeled as ESPN FPI/CORE.

Margin Power:
- SRS-style opponent-adjusted scoreboard margin.
- Refit by season/week using only prior weeks.
- Home margins are neutralized by a fixed HFA before solving.

Efficiency Power:
- Uses the existing leak-safe xPoints opponent-adjusted PPD snapshot.
- Converts home-vs-away PPD difference to a scoreboard-margin estimate using
  only prior-week league-average meaningful drives.
"""
from __future__ import annotations

from collections import defaultdict
import math
from statistics import median
from typing import Any

from sports_aggregator.cfb import narrative_shapes as v1
from sports_aggregator.cfb import narrative_shapes_v2 as v2
from sports_aggregator.cfb import narrative_composite as nc
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.xpoints import DATASET_VERSION as XPOINTS_VERSION

HFA_POINTS = 2.5
SRS_ITERATIONS = 50
MIN_SRS_GAMES = 3

LENS_KEYS = (
    "football_lab_edge",
    "elo_edge",
    "margin_power_edge",
    "efficiency_power_edge",
    "line_elo_edge",
    "narrative_interaction_edge",
)

BUCKETS = (
    (0.0, 0.50, "<0.50"),
    (0.50, 0.75, "0.50-0.75"),
    (0.75, 1.00, "0.75-1.00"),
    (1.00, 1.25, "1.00-1.25"),
    (1.25, 1.50, "1.25-1.50"),
    (1.50, float("inf"), ">=1.50"),
)


def _mean(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _std(values) -> float:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 1.0
    avg = sum(vals) / len(vals)
    return math.sqrt(sum((v - avg) ** 2 for v in vals) / len(vals)) or 1.0


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x, _ in pairs))
    dy = math.sqrt(sum((y - my) ** 2 for _, y in pairs))
    return num / (dx * dy) if dx and dy else None


def _solve_srs(games: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, int]]:
    """Simple iterative SRS on neutralized scoreboard margins."""
    team_games: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for game in games:
        home, away = str(game["home_team"]), str(game["away_team"])
        margin = float(game["home_points"]) - float(game["away_points"])
        neutral_home_margin = margin - HFA_POINTS
        team_games[home].append((away, neutral_home_margin))
        team_games[away].append((home, -neutral_home_margin))
    ratings = {team: 0.0 for team in team_games}
    for _ in range(SRS_ITERATIONS):
        updated = {}
        for team, history in team_games.items():
            updated[team] = sum(
                margin + ratings.get(opponent, 0.0)
                for opponent, margin in history
            ) / len(history)
        if updated:
            center = sum(updated.values()) / len(updated)
            ratings = {team: value - center for team, value in updated.items()}
    counts = {team: len(history) for team, history in team_games.items()}
    return ratings, counts


def margin_power_snapshots(repository: CFBRepository,
                           *, from_season: int = 2022,
                           to_season: int = 2025
                           ) -> dict[tuple[int, str], float]:
    """Pregame SRS margin estimate for each team-game, using only prior weeks."""
    with repository._reader() as connection:
        games = [dict(r) for r in connection.execute(
            """SELECT game_id,season,week,start_date,home_team,away_team,
                      home_points,away_points
               FROM games
               WHERE season BETWEEN ? AND ?
                 AND home_points IS NOT NULL AND away_points IS NOT NULL
               ORDER BY season,week,start_date,game_id""",
            (int(from_season), int(to_season)),
        )]
    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for game in games:
        by_season[int(game["season"])].append(game)

    snapshots: dict[tuple[int, str], float] = {}
    for season, season_games in by_season.items():
        weeks = sorted({int(g["week"]) for g in season_games if g["week"] is not None})
        prior: list[dict[str, Any]] = []
        for week in weeks:
            current = [g for g in season_games if int(g["week"]) == week]
            ratings, counts = _solve_srs(prior)
            for game in current:
                home, away = str(game["home_team"]), str(game["away_team"])
                if counts.get(home, 0) >= MIN_SRS_GAMES and counts.get(away, 0) >= MIN_SRS_GAMES:
                    home_margin = ratings[home] - ratings[away] + HFA_POINTS
                    snapshots[(int(game["game_id"]), home)] = home_margin
                    snapshots[(int(game["game_id"]), away)] = -home_margin
            prior.extend(current)
    return snapshots


def efficiency_power_snapshots(repository: CFBRepository,
                               *, from_season: int = 2022,
                               to_season: int = 2025
                               ) -> dict[tuple[int, str], float]:
    """Pregame efficiency margin from opponent-adjusted PPD and prior drive environment."""
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT x.game_id,x.team,x.opponent,x.season,x.week,x.home_away,
                      x.actual_drives,x.opponent_adjusted_points_per_drive,
                      g.home_team,g.away_team
               FROM cfb_xpoints_dataset x
               JOIN games g USING(game_id)
               WHERE x.dataset_version=? AND x.season BETWEEN ? AND ?
               ORDER BY x.season,x.week,g.start_date,x.game_id,x.home_away""",
            (XPOINTS_VERSION, int(from_season), int(to_season)),
        )]
    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_season[int(row["season"])].append(row)

    snapshots: dict[tuple[int, str], float] = {}
    for season, season_rows in by_season.items():
        weeks = sorted({int(r["week"]) for r in season_rows if r["week"] is not None})
        prior_drives: list[float] = []
        for week in weeks:
            current = [r for r in season_rows if int(r["week"]) == week]
            league_drives = (
                sum(prior_drives) / len(prior_drives) if prior_drives else None
            )
            by_game: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
            for row in current:
                by_game[int(row["game_id"])][str(row["home_away"])] = row
            if league_drives is not None:
                for gid, sides in by_game.items():
                    home, away = sides.get("home"), sides.get("away")
                    if not home or not away:
                        continue
                    hppd = home.get("opponent_adjusted_points_per_drive")
                    appd = away.get("opponent_adjusted_points_per_drive")
                    if hppd is None or appd is None:
                        continue
                    home_margin = (
                        (float(hppd) - float(appd)) * float(league_drives)
                        + HFA_POINTS
                    )
                    snapshots[(gid, str(home["team"]))] = home_margin
                    snapshots[(gid, str(away["team"]))] = -home_margin
            prior_drives.extend(
                float(r["actual_drives"]) for r in current
                if r.get("actual_drives") is not None
            )
    return snapshots


def _scoreboard_calibration(repository: CFBRepository,
                            target_season: int) -> dict[str, float] | None:
    """Map Football Lab offense-margin to final scoreboard margin using prior seasons."""
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT game_id,side,season,projected_offensive_points,actual_score_points
               FROM cfb_projection_backtest
               WHERE backtest_version=? AND season<?""",
            (BACKTEST_VERSION, int(target_season)),
        )]
    games: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        games[int(row["game_id"])][str(row["side"])] = row
    pairs = []
    for sides in games.values():
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        vals = (
            home.get("projected_offensive_points"), away.get("projected_offensive_points"),
            home.get("actual_score_points"), away.get("actual_score_points"),
        )
        if any(v is None for v in vals):
            continue
        x = float(home["projected_offensive_points"]) - float(away["projected_offensive_points"])
        y = float(home["actual_score_points"]) - float(away["actual_score_points"])
        pairs.append((x, y))
    if len(pairs) < 50:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    denom = sum((x - mx) ** 2 for x, _ in pairs)
    slope = sum((x - mx) * (y - my) for x, y in pairs) / denom if denom else 0.0
    return {"intercept": my - slope * mx, "slope": slope, "n": len(pairs)}


def _football_lab_margin_lookup(repository: CFBRepository,
                                target_season: int) -> dict[int, float]:
    calibration = _scoreboard_calibration(repository, target_season)
    if not calibration:
        return {}
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT game_id,side,season,projected_offensive_points
               FROM cfb_projection_backtest
               WHERE backtest_version=? AND season=?""",
            (BACKTEST_VERSION, int(target_season)),
        )]
    games: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        games[int(row["game_id"])][str(row["side"])] = row
    out = {}
    for gid, sides in games.items():
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        if home.get("projected_offensive_points") is None or away.get("projected_offensive_points") is None:
            continue
        offense_margin = (
            float(home["projected_offensive_points"])
            - float(away["projected_offensive_points"])
        )
        out[gid] = (
            float(calibration["intercept"])
            + float(calibration["slope"]) * offense_margin
        )
    return out


def build_lens_rows(repository: CFBRepository,
                    *, test_season: int = 2025) -> list[dict[str, Any]]:
    narrative_rows = v2._load_rows(repository)
    home_narrative = {
        int(r["game_id"]): r for r in narrative_rows if r["side"] == "home"
    }
    line_rate, _ = v2._choose_line_rate(
        narrative_rows, validation_season=int(test_season) - 1)
    base = nc._composite_games(
        repository, test_season=int(test_season), line_rate=line_rate)
    min_season = min(int(r["season"]) for r in base)
    margin_power = margin_power_snapshots(
        repository, from_season=min_season, to_season=int(test_season))
    efficiency_power = efficiency_power_snapshots(
        repository, from_season=min_season, to_season=int(test_season))

    fl_by_season = {
        season: _football_lab_margin_lookup(repository, season)
        for season in range(min_season, int(test_season) + 1)
    }

    out = []
    for row in base:
        gid, season = int(row["game_id"]), int(row["season"])
        market_residual = float(row["market_margin_residual"])
        # Recover the market expected margin from actual margin - residual.
        nrow = home_narrative.get(gid)
        if not nrow or nrow.get("market_expected_margin") is None:
            continue
        market = float(nrow["market_expected_margin"])
        mp_margin = margin_power.get((gid, str(row["team"])))
        ep_margin = efficiency_power.get((gid, str(row["team"])))
        fl_margin = fl_by_season.get(season, {}).get(gid)
        out.append({
            "game_id": gid,
            "season": season,
            "team": row["team"],
            "opponent": row["opponent"],
            "market_margin_residual": market_residual,
            "football_lab_edge": (
                float(fl_margin) - market if fl_margin is not None else None),
            "elo_edge": row.get("elo_edge"),
            "margin_power_edge": (
                float(mp_margin) - market if mp_margin is not None else None),
            "efficiency_power_edge": (
                float(ep_margin) - market if ep_margin is not None else None),
            "line_elo_edge": row.get("line_elo_edge"),
            "narrative_interaction_edge": row.get("narrative_interaction_edge"),
        })
    return out


def _correlations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for a in LENS_KEYS:
        out[a] = {}
        for b in LENS_KEYS:
            pairs = [
                (float(r[a]), float(r[b]))
                for r in rows if r.get(a) is not None and r.get(b) is not None
            ]
            corr = _pearson(pairs)
            out[a][b] = {
                "n": len(pairs),
                "correlation": round(corr, 4) if corr is not None else None,
            }
    return out


def _score(row: dict[str, Any], scales: dict[str, float]) -> dict[str, Any]:
    zs = {}
    for key in LENS_KEYS:
        if key not in scales or row.get(key) is None:
            continue
        zs[key] = float(row[key]) / float(scales[key])
    vals = list(zs.values())
    if not vals:
        return {"available": 0, "score": None, "direction": 0, "agreement": None, "zs": zs}
    score = sum(vals) / len(vals)
    direction = 1 if score > 0 else -1 if score < 0 else 0
    agree = sum(1 for z in vals if direction and ((z > 0) == (direction > 0)))
    return {
        "available": len(vals),
        "score": score,
        "direction": direction,
        "agreement": agree / len(vals) if direction else 0.0,
        "zs": zs,
    }


def _bucket(rows: list[dict[str, Any]], scales: dict[str, float],
            low: float, high: float) -> dict[str, Any]:
    chosen = []
    for row in rows:
        s = _score(row, scales)
        if s["available"] < 4 or s["score"] is None:
            continue
        mag = abs(float(s["score"]))
        if not (low <= mag < high):
            continue
        aligned = float(row["market_margin_residual"]) * int(s["direction"])
        chosen.append((aligned, s))
    if not chosen:
        return {"n": 0}
    vals = [v for v, _ in chosen]
    return {
        "n": len(vals),
        "directional_hit_rate": round(sum(v > 0 for v in vals) / len(vals), 4),
        "mean_aligned_residual": round(sum(vals) / len(vals), 3),
        "median_aligned_residual": round(float(median(vals)), 3),
        "mean_agreement": round(
            sum(float(s["agreement"]) for _, s in chosen) / len(chosen), 4),
        "mean_available_lenses": round(
            sum(int(s["available"]) for _, s in chosen) / len(chosen), 3),
        "worst_aligned_miss": round(min(vals), 3),
        "best_aligned_result": round(max(vals), 3),
    }


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    rows = build_lens_rows(repository, test_season=int(test_season))
    seasons = sorted({int(r["season"]) for r in rows})
    walk = []
    for season in [s for s in seasons if s > min(seasons) and s <= int(test_season)]:
        train = [r for r in rows if int(r["season"]) < season]
        test = [r for r in rows if int(r["season"]) == season]
        counts = {
            key: sum(1 for r in train if r.get(key) is not None)
            for key in LENS_KEYS
        }
        active = tuple(key for key in LENS_KEYS if counts[key] >= 50)
        scales = {
            key: _std(r.get(key) for r in train if r.get(key) is not None)
            for key in active
        }
        coverage = {
            key: {
                "rows": counts[key],
                "coverage_rate": round(counts[key] / len(train), 4) if train else 0.0,
                "active": key in active,
                "std": round(scales[key], 4) if key in scales else None,
            }
            for key in LENS_KEYS
        }
        buckets = [
            {"bucket": label, "metrics": _bucket(test, scales, low, high)}
            for low, high, label in BUCKETS
        ]
        single_lens = {}
        for key in active:
            vals = []
            for r in test:
                if r.get(key) is None:
                    continue
                direction = 1 if float(r[key]) > 0 else -1 if float(r[key]) < 0 else 0
                if not direction:
                    continue
                vals.append(float(r["market_margin_residual"]) * direction)
            single_lens[key] = {
                "n": len(vals),
                "directional_hit_rate": round(sum(v > 0 for v in vals) / len(vals), 4)
                if vals else None,
                "mean_aligned_residual": round(sum(vals) / len(vals), 3)
                if vals else None,
            }
        walk.append({
            "season": season,
            "train_rows": len(train),
            "test_rows": len(test),
            "active_lenses": list(active),
            "coverage": coverage,
            "training_correlations": _correlations(train),
            "single_lens_test": single_lens,
            "magnitude_buckets": buckets,
        })
    return {
        "version": "internal-power-lenses-v1",
        "test_season": int(test_season),
        "lenses": {
            "football_lab_edge": "prior-season calibrated production offense margin vs closing market",
            "elo_edge": "results-based Elo expected margin vs closing market",
            "margin_power_edge": "SRS-style prior-week opponent-adjusted scoreboard margin vs closing market",
            "efficiency_power_edge": "prior-week opponent-adjusted PPD margin vs closing market",
            "line_elo_edge": "pre-line Line Elo margin vs closing market",
            "narrative_interaction_edge": "prior-history narrative interaction edge",
        },
        "walk_forward_years": walk,
        "notes": [
            "No FPI or CORE inputs are used or checked.",
            "Margin Power uses only completed prior weeks in the same season.",
            "Efficiency Power uses only leak-safe pregame xPoints features and prior-week league drives.",
            "Football Lab offense margin is calibrated to final-score margin using prior seasons only.",
            "A lens activates only after at least 50 prior observations.",
            "Composite buckets require at least four active, populated lenses on a game.",
        ],
    }
