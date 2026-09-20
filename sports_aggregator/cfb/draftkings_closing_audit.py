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


GRADE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cfb_draftkings_convergence_grade (
  season INTEGER NOT NULL,
  game_id INTEGER NOT NULL,
  week INTEGER,
  kickoff TEXT,
  home_team TEXT NOT NULL,
  away_team TEXT NOT NULL,
  selected_side TEXT NOT NULL,
  selected_team TEXT NOT NULL,
  draftkings_spread REAL NOT NULL,
  draftkings_price INTEGER NOT NULL,
  snapshot_timestamp TEXT,
  book_last_update TEXT,
  result TEXT NOT NULL,
  profit_units REAL NOT NULL,
  applicability_points INTEGER,
  applicability_band TEXT,
  graded_at TEXT NOT NULL,
  PRIMARY KEY(season, game_id)
);
"""


def _persist_grades(repository: CFBRepository, season: int,
                    rows: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with repository.transaction() as connection:
        connection.executescript(GRADE_SCHEMA)
        connection.execute(
            "DELETE FROM cfb_draftkings_convergence_grade WHERE season=?",
            (int(season),),
        )
        connection.executemany(
            """INSERT INTO cfb_draftkings_convergence_grade(
                 season,game_id,week,kickoff,home_team,away_team,
                 selected_side,selected_team,draftkings_spread,draftkings_price,
                 snapshot_timestamp,book_last_update,result,profit_units,
                 applicability_points,applicability_band,graded_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    int(season), int(r["game_id"]), r.get("week"), r.get("kickoff"),
                    r["home_team"], r["away_team"], r["selected_side"], r["selected_team"],
                    float(r["draftkings_spread"]), int(r["draftkings_price"]),
                    r.get("draftkings_snapshot_timestamp"), r.get("draftkings_last_update"),
                    r["result"], float(r["profit_units"]),
                    r.get("applicability_points_at_dk_close"),
                    r.get("applicability_band_at_dk_close"), now,
                )
                for r in rows
            ],
        )

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
        xpoints = connection.execute(
            """SELECT COUNT(*) AS n FROM cfb_xpoints_dataset
               WHERE season=?""",
            (int(season),),
        ).fetchone()["n"]
        drive_outcomes = connection.execute(
            """SELECT COUNT(*) AS n
               FROM cfb_team_game_drive_outcomes o
               JOIN games g USING(game_id)
               WHERE g.season=?""",
            (int(season),),
        ).fetchone()["n"]
    lens_rows = [
        r for r in ipl.build_lens_rows(repository, test_season=int(season))
        if int(r["season"]) == int(season)
    ]
    with repository._reader() as connection:
        season_games = [
            dict(r) for r in connection.execute(
                """SELECT game_id,week,start_date,home_team,away_team
                   FROM games
                   WHERE season=? AND home_points IS NOT NULL AND away_points IS NOT NULL
                   ORDER BY week,start_date,game_id""",
                (int(season),),
            )
        ]
    prior_counts: dict[str, int] = {}
    margin_power_eligibility_by_week: dict[str, dict[str, int]] = {}
    for game in season_games:
        week = str(game.get("week"))
        bucket = margin_power_eligibility_by_week.setdefault(
            week, {"completed_games": 0, "both_teams_3_plus_prior": 0}
        )
        bucket["completed_games"] += 1
        home = str(game["home_team"])
        away = str(game["away_team"])
        if (
            prior_counts.get(home, 0) >= ipl.MIN_SRS_GAMES
            and prior_counts.get(away, 0) >= ipl.MIN_SRS_GAMES
        ):
            bucket["both_teams_3_plus_prior"] += 1
        prior_counts[home] = prior_counts.get(home, 0) + 1
        prior_counts[away] = prior_counts.get(away, 0) + 1
    classified = [
        r for r in cap.cr._classified_with_context(repository, test_season=int(season))
        if int(r["season"]) == int(season)
    ]
    lens_keys = (
        "margin_power_edge",
        "football_lab_edge",
        "elo_edge",
        "efficiency_power_edge",
        "line_elo_edge",
    )
    lens_coverage = {
        key: sum(r.get(key) is not None for r in lens_rows)
        for key in lens_keys
    }
    structural_two_plus = sum(
        sum(
            r.get(key) is not None
            for key in ("football_lab_edge", "elo_edge", "efficiency_power_edge")
        ) >= 2
        for r in lens_rows
    )
    both_confirmation_inputs = sum(
        r.get("margin_power_edge") is not None
        and r.get("line_elo_edge") is not None
        and sum(
            r.get(key) is not None
            for key in ("football_lab_edge", "elo_edge", "efficiency_power_edge")
        ) >= 2
        for r in lens_rows
    )
    return {
        "completed_games": int(completed),
        "narrative_rows": int(narrative),
        "projection_backtest_rows": int(projections),
        "xpoints_rows": int(xpoints),
        "drive_outcome_rows": int(drive_outcomes),
        "lens_rows": len(lens_rows),
        "margin_power_min_prior_games": int(ipl.MIN_SRS_GAMES),
        "margin_power_eligibility_by_week": margin_power_eligibility_by_week,
        "margin_power_eligible_completed_games": sum(
            item["both_teams_3_plus_prior"]
            for item in margin_power_eligibility_by_week.values()
        ),
        "lens_non_null": lens_coverage,
        "rows_with_two_plus_structural_components": structural_two_plus,
        "rows_with_primary_market_and_structural_inputs": both_confirmation_inputs,
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

    _persist_grades(repository, int(season), graded)

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
