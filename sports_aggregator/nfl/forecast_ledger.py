"""Immutable issuance ledger and grading for NFL experimental forecasts."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository


def freeze_dashboard(repository: NFLRepository, dashboard: dict[str, Any],
                     *, generated_at: str | None = None) -> int:
    """Store only forecasts that changed since the prior issuance."""
    repository.initialize()
    issued = generated_at or datetime.now(timezone.utc).isoformat()
    version = str(dashboard.get("version") or "unknown")
    stored = 0
    with closing(repository._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for game in dashboard.get("games", []):
            model = game.get("football_lab")
            if not model:
                continue
            market = game.get("market_anchor") or {}
            ats_pick = game.get("ats_pick") or ""
            ats_team = ats_pick.split(" ", 1)[0] if ats_pick else None
            values = (
                float(model["away_points"]), float(model["home_points"]),
                float(model["margin"]), float(model["total"]),
                market.get("margin"), market.get("total"),
                game.get("straight_up_pick"), ats_team, game.get("total_pick"),
                json.dumps(game.get("narrative_tags") or [], sort_keys=True),
            )
            previous = connection.execute(
                """SELECT away_points,home_points,model_margin,model_total,
                          spread_at_issue,total_at_issue,straight_up_pick,
                          ats_pick_team,total_pick,tags_json
                   FROM nfl_engine_forecasts WHERE game_id=? AND model_version=?
                   ORDER BY forecast_id DESC LIMIT 1""",
                (game["game_id"], version),
            ).fetchone()
            if previous is not None and tuple(previous) == values:
                continue
            connection.execute(
                """INSERT INTO nfl_engine_forecasts (
                     game_id,season,week,generated_at,model_version,away_team,home_team,
                     away_points,home_points,model_margin,model_total,spread_at_issue,
                     total_at_issue,straight_up_pick,ats_pick_team,total_pick,tags_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (game["game_id"], int(game["season"]), int(game["week"]), issued,
                 version, game["away_team"], game["home_team"], *values),
            )
            stored += 1
        connection.commit()
    return stored


def _grade(value: float) -> str:
    return "win" if value > 0 else "loss" if value < 0 else "push"


def grading_report(repository: NFLRepository, *, season: int | None = None) -> dict[str, Any]:
    """Grade every frozen issuance against its exact line at issue."""
    repository.initialize()
    where = "WHERE f.season=?" if season is not None else ""
    params = (int(season),) if season is not None else ()
    with closing(repository._connect()) as connection:
        rows = [dict(row) for row in connection.execute(
            f"""SELECT f.*,g.away_score,g.home_score,g.completed
                FROM nfl_engine_forecasts f JOIN games g ON g.game_id=f.game_id
                {where} ORDER BY f.generated_at,f.forecast_id""", params)]
    graded = []
    for row in rows:
        if not row["completed"] or row["away_score"] is None or row["home_score"] is None:
            continue
        actual_margin = float(row["home_score"] - row["away_score"])
        actual_total = float(row["home_score"] + row["away_score"])
        if not row["straight_up_pick"]:
            row["straight_up_grade"] = None
        else:
            row["straight_up_grade"] = (
                "push" if row["home_score"] == row["away_score"] else
                "win" if (row["home_score"] > row["away_score"]) == (row["straight_up_pick"] == row["home_team"])
                else "loss"
            )
        if row["spread_at_issue"] is not None and row["ats_pick_team"]:
            direction = 1.0 if row["ats_pick_team"] == row["home_team"] else -1.0
            row["ats_grade"] = _grade((actual_margin - float(row["spread_at_issue"])) * direction)
        else:
            row["ats_grade"] = None
        if row["total_at_issue"] is not None and row["total_pick"]:
            direction = 1.0 if str(row["total_pick"]).startswith("Over") else -1.0
            row["total_grade"] = _grade((actual_total - float(row["total_at_issue"])) * direction)
        else:
            row["total_grade"] = None
        row["tags"] = json.loads(row.pop("tags_json"))
        graded.append(row)

    def summary(key: str) -> dict[str, Any]:
        results = [row[key] for row in graded if row.get(key)]
        counts = {plural: results.count(name) for name, plural in (
            ("win", "wins"), ("loss", "losses"), ("push", "pushes"))}
        decisions = counts["wins"] + counts["losses"]
        return {**counts, "graded": len(results),
                "win_rate_ex_pushes": round(counts["wins"] / decisions, 4) if decisions else None}

    return {
        "season": season, "frozen": len(rows), "completed_issuances": len(graded),
        "straight_up": summary("straight_up_grade"),
        "against_spread": summary("ats_grade"), "totals": summary("total_grade"),
        "issuances": graded,
    }
