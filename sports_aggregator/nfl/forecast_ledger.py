"""Immutable issuance ledger and grading for NFL experimental forecasts."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from typing import Any
from zoneinfo import ZoneInfo

from sports_aggregator import best_lines
from sports_aggregator.nfl.repository import NFLRepository, issue_prices, pick_prices


def _kickoff_passed(row, issued: datetime) -> bool:
    """True when the stored kickoff is at or before the issuance moment.

    The ledger exists to prove a pick predates the game, so a forecast
    generated after kickoff (a late refresh, a manual rerun on a finished
    week) must never be stored. Unknown kickoff times are given the benefit of
    the doubt only when the date is still ahead.
    """
    if row is None or not row["game_date"]:
        return False
    try:
        kickoff = datetime.fromisoformat(
            f"{row['game_date']}T{(row['game_time'] or '12:00').strip()}"
        ).replace(tzinfo=ZoneInfo("America/New_York"))
    except ValueError:
        return False
    return kickoff.astimezone(timezone.utc) <= issued


def _best_line_fields(connection, game: dict[str, Any], ats_team: str | None, issued_at: datetime
                      ) -> tuple[float | None, str | None, float | None, str | None, float | None, str | None]:
    """(best ATS spread, its book, best total, its book, best moneyline for the straight-up pick, its book) at issue.

    The spread is in the ledger's sign convention.

    The ledger's spreads are nflverse's (positive = home favored), so a home pick's own spread is negated and an away
    pick's is kept. Anything that goes wrong leaves the pick to be graded at the consensus line it was issued at.
    """
    try:
        moment = issued_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        live = abs((datetime.now(timezone.utc) - issued_at).total_seconds()) < 3 * 3600
        snapshot = best_lines.snapshot(
            connection, game["game_id"], as_of=moment,
            fallback=best_lines.espn_rows(connection, game["game_id"]) if live else None)
    except Exception:
        return None, None, None, None, None, None
    if not snapshot:
        return None, None, None, None, None, None
    spread = book = None
    if ats_team and ats_team == game.get("home_team"):
        own, book = best_lines.side_spread(snapshot, "home")
        spread = -own if own is not None else None
    elif ats_team and ats_team == game.get("away_team"):
        spread, book = best_lines.side_spread(snapshot, "away")
    total = total_book = None
    pick = str(game.get("total_pick") or "")
    side = "over" if pick.startswith("Over") else "under" if pick.startswith("Under") else None
    if side and isinstance(snapshot.get(side), dict):
        total, total_book = snapshot[side]["total"], snapshot[side]["name"]
    # the straight-up pick is a team code; its best moneyline is the highest price any tracked book pays on that team
    su_team = game.get("straight_up_pick")
    su_side = "home" if su_team and su_team == game.get("home_team") else "away" if su_team and su_team == game.get("away_team") else None
    su_price, su_book = best_lines.side_moneyline(snapshot, su_side)
    return spread, book, total, total_book, su_price, su_book


def freeze_dashboard(repository: NFLRepository, dashboard: dict[str, Any],
                     *, generated_at: str | None = None) -> int:
    """Store only forecasts that changed since the prior issuance."""
    repository.initialize()
    issued = generated_at or datetime.now(timezone.utc).isoformat()
    issued_at = datetime.fromisoformat(issued.replace("Z", "+00:00"))
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    version = str(dashboard.get("version") or "unknown")
    stored = 0
    with closing(repository._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for game in dashboard.get("games", []):
            model = game.get("football_lab")
            if not model:
                continue
            schedule_row = connection.execute(
                "SELECT game_date,game_time FROM games WHERE game_id=?", (game["game_id"],)
            ).fetchone()
            if _kickoff_passed(schedule_row, issued_at):
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
            su_price, total_price = pick_prices(
                {"straight_up_pick": game.get("straight_up_pick"), "total_pick": game.get("total_pick"),
                 "home_team": game["home_team"]},
                issue_prices(connection, game["game_id"], issued))
            best_fields = _best_line_fields(connection, game, ats_team, issued_at)
            connection.execute(
                """INSERT INTO nfl_engine_forecasts (
                     game_id,season,week,generated_at,model_version,away_team,home_team,
                     away_points,home_points,model_margin,model_total,spread_at_issue,
                     total_at_issue,straight_up_pick,ats_pick_team,total_pick,tags_json,
                     su_price_at_issue,total_price_at_issue,
                     best_ats_spread,best_ats_book,best_total,best_total_book,best_su_price,best_su_book
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (game["game_id"], int(game["season"]), int(game["week"]), issued,
                 version, game["away_team"], game["home_team"], *values, su_price, total_price, *best_fields),
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
            f"""SELECT f.*,g.away_score,g.home_score,g.completed,
                       g.spread_line AS close_spread,g.total_line AS close_total
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
        # Judged at the best tracked-book line stored with the pick; a pick issued before line tracking, or one no
        # tracked book priced, is judged at the consensus line it was issued at, as it always was.
        ats_line = row.get("best_ats_spread")
        row["ats_line_basis"] = "best" if ats_line is not None else "consensus"
        if ats_line is None:
            ats_line = row["spread_at_issue"]
        if ats_line is not None and row["ats_pick_team"]:
            direction = 1.0 if row["ats_pick_team"] == row["home_team"] else -1.0
            row["ats_grade"] = _grade((actual_margin - float(ats_line)) * direction)
        else:
            row["ats_grade"] = None
        total_line = row.get("best_total")
        row["total_line_basis"] = "best" if total_line is not None else "consensus"
        if total_line is None:
            total_line = row["total_at_issue"]
        if total_line is not None and row["total_pick"]:
            direction = 1.0 if str(row["total_pick"]).startswith("Over") else -1.0
            row["total_grade"] = _grade((actual_total - float(total_line)) * direction)
        else:
            row["total_grade"] = None
        # Straight-up units are priced at the best moneyline stored with the pick when there is one.
        best_price = row.get("best_su_price")
        row["su_price_basis"] = "best" if best_price is not None else "consensus"
        row["su_price_used"] = best_price if best_price is not None else row.get("su_price_at_issue")
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
