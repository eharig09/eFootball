"""DraftKings closing-line grading for Full Convergence.

Historical DraftKings spread prices are fetched from The Odds API at the
closest archived snapshot at or before kickoff. Full Convergence qualification
itself remains unchanged; DraftKings is used only for closing-market context,
ATS grading, and realized return.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from sports_aggregator.cfb import convergence_action_policy as cap
from sports_aggregator.cfb import convergence_quality_audit as cqa
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.models import normalize_alias
from sports_aggregator.cfb.repository import CFBRepository

SPORT_KEY = "americanfootball_ncaaf"
BOOKMAKER = "draftkings"


def _american_profit(price: int | float, stake: float = 1.0) -> float:
    p = float(price)
    if p == 0:
        raise ValueError("American odds cannot be zero")
    return stake * (p / 100.0 if p > 0 else 100.0 / abs(p))


def _grade(selected_point: float, actual_margin_for_team: float) -> str:
    value = float(actual_margin_for_team) + float(selected_point)
    return "win" if value > 0 else "loss" if value < 0 else "push"


def _roi_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [r for r in rows if r.get("result") in {"win", "loss", "push"}]
    wins = sum(r["result"] == "win" for r in graded)
    losses = sum(r["result"] == "loss" for r in graded)
    pushes = sum(r["result"] == "push" for r in graded)
    risked = float(len(graded))
    net = round(sum(float(r.get("profit_units") or 0.0) for r in graded), 4)
    decisions = wins + losses
    return {
        "n": len(graded),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_ex_pushes": round(wins / decisions, 4) if decisions else None,
        "risked_units": risked,
        "net_units": net,
        "roi": round(net / risked, 4) if risked else None,
    }


def _snapshot(api_key: str, kickoff: str) -> dict[str, Any]:
    dt = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    params = urlencode({
        "apiKey": api_key,
        "bookmakers": BOOKMAKER,
        "markets": "spreads",
        "oddsFormat": "american",
        "dateFormat": "iso",
        "date": dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    url = (
        f"https://api.the-odds-api.com/v4/historical/sports/{SPORT_KEY}/odds"
        f"?{params}"
    )
    with urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _dk_market(snapshot: dict[str, Any], home: str, away: str) -> dict[str, Any] | None:
    h, a = normalize_alias(home), normalize_alias(away)
    for event in snapshot.get("data") or []:
        if normalize_alias(str(event.get("home_team") or "")) != h:
            continue
        if normalize_alias(str(event.get("away_team") or "")) != a:
            continue
        for book in event.get("bookmakers") or []:
            if str(book.get("key") or "").casefold() != BOOKMAKER:
                continue
            for market in book.get("markets") or []:
                if market.get("key") != "spreads":
                    continue
                outcomes = {}
                for outcome in market.get("outcomes") or []:
                    outcomes[normalize_alias(str(outcome.get("name") or ""))] = outcome
                ho, ao = outcomes.get(h), outcomes.get(a)
                if not ho or not ao:
                    continue
                return {
                    "snapshot_timestamp": snapshot.get("timestamp"),
                    "book_last_update": book.get("last_update"),
                    "home_point": float(ho["point"]),
                    "home_price": int(ho["price"]),
                    "away_point": float(ao["point"]),
                    "away_price": int(ao["price"]),
                }
    return None


def _games(repository: CFBRepository, season: int) -> dict[int, dict[str, Any]]:
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT game_id,season,week,start_date,home_team,away_team,
                      home_points,away_points
               FROM games
               WHERE season=? AND home_points IS NOT NULL
                 AND away_points IS NOT NULL""",
            (int(season),),
        )
        return {int(r["game_id"]): dict(r) for r in rows}


def _decorate_context(repository: CFBRepository, rows: list[dict[str, Any]],
                      *, season: int) -> list[dict[str, Any]]:
    lens_rows = ipl.build_lens_rows(repository, test_season=int(season))
    lens = {int(r["game_id"]): r for r in lens_rows}
    out = []
    for source in rows:
        row = dict(source)
        lr = lens.get(int(row["game_id"]), {})
        consensus = float(row["market_home_margin"])
        mp_edge = lr.get("margin_power_edge")
        mp_margin = consensus + float(mp_edge) if mp_edge is not None else None
        row["_margin_power_implied_margin"] = mp_margin
        row["_all_structural"] = all(
            lr.get(k) is not None
            for k in ("football_lab_edge", "elo_edge", "efficiency_power_edge")
        )
        out.append(row)
    return out


def _pipeline_counts(repository: CFBRepository, season: int) -> dict[str, int]:
    with repository._reader() as connection:
        completed = connection.execute(
            """SELECT COUNT(*) AS n FROM games
               WHERE season=? AND home_points IS NOT NULL AND away_points IS NOT NULL""",
            (int(season),),
        ).fetchone()["n"]
        narrative = connection.execute(
            """SELECT COUNT(*) AS n FROM cfb_narrative_state
               WHERE season=?""",
            (int(season),),
        ).fetchone()["n"]
        projections = connection.execute(
            """SELECT COUNT(*) AS n FROM cfb_projection_backtest
               WHERE season=?""",
            (int(season),),
        ).fetchone()["n"]
    lens_rows = [
        r for r in ipl.build_lens_rows(repository, test_season=int(season))
        if int(r["season"]) == int(season)
    ]
    classified = [
        r for r in cap.cr._classified_with_context(repository, test_season=int(season))
        if int(r["season"]) == int(season)
    ]
    return {
        "completed_games": int(completed),
        "narrative_rows": int(narrative),
        "projection_backtest_rows": int(projections),
        "lens_rows": len(lens_rows),
        "classified_rows_with_two_available_confirmations": len(classified),
    }


def report(repository: CFBRepository, *, season: int = 2026,
           api_key: str | None = None) -> dict[str, Any]:
    key = api_key or os.getenv("THE_ODDS_API_KEY")
    if not key:
        return {
            "version": "draftkings-full-convergence-roi-v1",
            "season": int(season),
            "status": "missing_api_key",
            "required_env": "THE_ODDS_API_KEY",
            "note": "Historical DraftKings spread prices are not stored in game_lines.",
        }

    pipeline = _pipeline_counts(repository, int(season))
    games = _games(repository, int(season))
    full = [
        r for r in cap._full_rows(repository, test_season=int(season))
        if int(r["season"]) == int(season) and int(r["game_id"]) in games
    ]
    full = _decorate_context(repository, full, season=int(season))
    snapshot_cache: dict[str, dict[str, Any]] = {}
    graded = []
    missing = []

    for row in full:
        game = games[int(row["game_id"])]
        kickoff = str(game["start_date"])
        if kickoff not in snapshot_cache:
            snapshot_cache[kickoff] = _snapshot(key, kickoff)
        dk = _dk_market(
            snapshot_cache[kickoff],
            str(game["home_team"]),
            str(game["away_team"]),
        )
        if not dk:
            missing.append({
                "game_id": row["game_id"],
                "home_team": game["home_team"],
                "away_team": game["away_team"],
                "kickoff": kickoff,
                "reason": "draftkings_spread_not_found",
            })
            continue

        direction = int(row["primary_direction"])
        selected_side = "home" if direction > 0 else "away"
        selected_point = (
            float(dk["home_point"]) if selected_side == "home"
            else float(dk["away_point"])
        )
        selected_price = (
            int(dk["home_price"]) if selected_side == "home"
            else int(dk["away_price"])
        )
        actual_home = float(game["home_points"]) - float(game["away_points"])
        actual_selected = actual_home if selected_side == "home" else -actual_home
        result = _grade(selected_point, actual_selected)
        profit = (
            _american_profit(selected_price)
            if result == "win"
            else -1.0 if result == "loss"
            else 0.0
        )

        dk_home_margin = -float(dk["home_point"])
        mp_margin = row.get("_margin_power_implied_margin")
        region = cqa._key_number_region(abs(dk_home_margin))
        edge_raw = (
            float(mp_margin) - dk_home_margin if mp_margin is not None else None
        )
        crossing = cqa._crossing_label(
            cqa._crossed_keys(dk_home_margin, mp_margin)
        )
        context = {
            "key_number_region": region,
            "margin_power_edge_bucket": cqa._edge_magnitude_bucket(edge_raw),
            "margin_power_key_crossing": crossing,
            "all_three_structural_available": bool(row["_all_structural"]),
        }
        points, reasons = cqa._applicability_points(context)
        band = cqa._applicability_band(points)

        graded.append({
            "game_id": int(row["game_id"]),
            "week": game.get("week"),
            "kickoff": kickoff,
            "home_team": game["home_team"],
            "away_team": game["away_team"],
            "selected_side": selected_side,
            "selected_team": (
                game["home_team"] if selected_side == "home" else game["away_team"]
            ),
            "draftkings_spread": selected_point,
            "draftkings_price": selected_price,
            "draftkings_snapshot_timestamp": dk["snapshot_timestamp"],
            "draftkings_last_update": dk["book_last_update"],
            "result": result,
            "profit_units": round(profit, 4),
            "applicability_points_at_dk_close": points,
            "applicability_band_at_dk_close": band,
            "applicability_reasons_at_dk_close": reasons,
        })

    by_band = {}
    for band in ("high", "medium", "low"):
        by_band[band] = _roi_summary([
            r for r in graded if r["applicability_band_at_dk_close"] == band
        ])

    return {
        "version": "draftkings-full-convergence-roi-v1",
        "season": int(season),
        "bookmaker": "DraftKings",
        "grading": "closest archived DraftKings spread snapshot at or before kickoff",
        "selection_pipeline": pipeline,
        "qualified_full_convergence_games": len(full),
        "graded_games": len(graded),
        "missing_draftkings_games": missing,
        "overall": _roi_summary(graded),
        "by_applicability_band": by_band,
        "games": graded,
    }
