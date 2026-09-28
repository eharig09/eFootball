"""Leak-safe NFL narrative/perception proxy research.

These tags describe observable pregame stories; they do not claim to measure
public betting. Ticket and money percentages require a separate licensed feed.
Weeks are snapshotted before any result from that week enters team state.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-perception-challenger-v1"


def _average(values: Iterable[float]) -> float | None:
    rows = list(values)
    return sum(rows) / len(rows) if rows else None


def _team_summary(games: list[dict[str, Any]], team: str) -> dict[str, float] | None:
    rows = [game for game in games if team in (game["home_team"], game["away_team"])]
    if not rows:
        return None
    wins = points = allowed = margin = 0.0
    for game in rows:
        home = team == game["home_team"]
        scored = float(game["home_score"] if home else game["away_score"])
        conceded = float(game["away_score"] if home else game["home_score"])
        wins += 1.0 if scored > conceded else 0.5 if scored == conceded else 0.0
        points += scored
        allowed += conceded
        margin += scored - conceded
    return {
        "games": float(len(rows)), "win_pct": wins / len(rows),
        "ppg": points / len(rows), "papg": allowed / len(rows),
        "margin": margin / len(rows),
    }


def _result(value: float) -> str:
    return "win" if value > 0 else "loss" if value < 0 else "push"


def _tag(key: str, label: str, *, market_side: str, fade_side: str,
         family: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": key, "label": label, "family": family,
        "market_narrative": market_side, "contrarian_fade": fade_side,
        "evidence": evidence,
    }


def narrative_tags(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Create fixed-threshold, human-readable tags from pregame-only values."""
    tags: list[dict[str, Any]] = []
    total = row.get("total_line")
    market_avg = row.get("pregame_market_total_avg")
    home = row["home_team"]
    away = row["away_team"]
    home_ppg = row.get("home_season_ppg")
    away_ppg = row.get("away_season_ppg")
    league_ppg = row.get("pregame_league_team_ppg")

    if None not in (total, market_avg, home_ppg, away_ppg, league_ppg):
        if home_ppg >= league_ppg + 2 and away_ppg >= league_ppg + 2 and total <= market_avg + 1:
            tags.append(_tag(
                "two_hot_offenses_average_total",
                "Two high-scoring offenses, ordinary total", family="total_perception",
                market_side="over", fade_side="under",
                evidence={"home_ppg": home_ppg, "away_ppg": away_ppg,
                          "league_ppg": league_ppg, "total": total,
                          "market_total_avg": market_avg},
            ))
        if home_ppg <= league_ppg - 2 and away_ppg <= league_ppg - 2 and total >= market_avg - 1:
            tags.append(_tag(
                "two_cold_offenses_average_total",
                "Two low-scoring offenses, ordinary total", family="total_perception",
                market_side="under", fade_side="over",
                evidence={"home_ppg": home_ppg, "away_ppg": away_ppg,
                          "league_ppg": league_ppg, "total": total,
                          "market_total_avg": market_avg},
            ))

    home_last = row.get("home_last_points")
    away_last = row.get("away_last_points")
    if None not in (total, market_avg, home_last, away_last):
        if home_last >= 30 and away_last >= 30 and total <= market_avg + 1:
            tags.append(_tag(
                "double_fireworks_average_total",
                "Both teams scored 30+, ordinary total", family="recency_total",
                market_side="over", fade_side="under",
                evidence={"home_last_points": home_last, "away_last_points": away_last,
                          "total": total, "market_total_avg": market_avg},
            ))

    spread = row.get("spread_line")
    if spread is not None:
        favorite = home if spread >= 3 else away if spread <= -3 else None
        if favorite:
            prefix = "home" if favorite == home else "away"
            other = away if favorite == home else home
            last_margin = row.get(f"{prefix}_last_margin")
            if last_margin is not None and last_margin >= 14:
                tags.append(_tag(
                    "post_blowout_favorite", "Favorite off a 14+ point win",
                    family="recency_side", market_side=favorite, fade_side=other,
                    evidence={"favorite": favorite, "last_margin": last_margin,
                              "home_margin_line": spread},
                ))
            current = row.get(f"{prefix}_season_win_pct")
            preseason = row.get(f"{prefix}_preseason_win_pct_proxy")
            if current is not None and preseason is not None and current - preseason >= .25:
                tags.append(_tag(
                    "overachiever_favorite", "Fast-starting favorite above preseason baseline",
                    family="preseason_side", market_side=favorite, fade_side=other,
                    evidence={"favorite": favorite, "season_win_pct": current,
                              "preseason_win_pct_proxy": preseason,
                              "home_margin_line": spread},
                ))
    return tags


def build_rows(repository: NFLRepository, start_season: int, end_season: int) -> list[dict[str, Any]]:
    repository.initialize()
    with repository._connect() as connection:
        games = [dict(row) for row in connection.execute(
            """SELECT game_id,season,season_type,week,game_date,away_team,home_team,
                      away_score,home_score,completed,spread_line,total_line
               FROM games WHERE season BETWEEN ? AND ?
               ORDER BY season,week,game_date,game_id""",
            (int(start_season) - 1, int(end_season)),
        )]

    completed_by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for game in games:
        if game["completed"] and game["home_score"] is not None and game["away_score"] is not None:
            completed_by_season[int(game["season"])].append(game)

    output: list[dict[str, Any]] = []
    for season in range(int(start_season), int(end_season) + 1):
        season_games = [game for game in games if int(game["season"]) == season]
        prior = [game for game in completed_by_season.get(season - 1, [])
                 if game["season_type"] == "REG"]
        preseason = {
            team: _team_summary(prior, team)
            for team in {g["home_team"] for g in season_games} | {g["away_team"] for g in season_games}
        }
        history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        observed_lines: list[float] = []
        for week in sorted({int(game["week"]) for game in season_games}):
            week_games = [game for game in season_games if int(game["week"]) == week]
            league_games = list({
                str(item["game_id"]): item
                for rows in history.values() for item in rows
            }.values())
            league_ppg = _average(
                float(g["home_score"] + g["away_score"]) / 2 for g in league_games
            ) if league_games else None
            market_avg = _average(observed_lines)
            for game in week_games:
                row = dict(game)
                for side in ("home", "away"):
                    team = str(game[f"{side}_team"])
                    team_history = history.get(team, [])
                    summary = _team_summary(team_history, team)
                    base = preseason.get(team)
                    row[f"{side}_season_games"] = int(summary["games"]) if summary else 0
                    row[f"{side}_season_ppg"] = summary["ppg"] if summary else None
                    row[f"{side}_season_win_pct"] = summary["win_pct"] if summary else None
                    row[f"{side}_preseason_win_pct_proxy"] = base["win_pct"] if base else None
                    row[f"{side}_preseason_margin_proxy"] = base["margin"] if base else None
                    if team_history:
                        last = team_history[-1]
                        is_home = last["home_team"] == team
                        scored = last["home_score"] if is_home else last["away_score"]
                        allowed = last["away_score"] if is_home else last["home_score"]
                        row[f"{side}_last_points"] = float(scored)
                        row[f"{side}_last_margin"] = float(scored - allowed)
                    else:
                        row[f"{side}_last_points"] = None
                        row[f"{side}_last_margin"] = None
                row["pregame_league_team_ppg"] = league_ppg
                row["pregame_market_total_avg"] = market_avg
                row["tags"] = narrative_tags(row)
                if game["completed"]:
                    actual_margin = float(game["home_score"] - game["away_score"])
                    actual_total = float(game["home_score"] + game["away_score"])
                    row["actual_margin"] = actual_margin
                    row["actual_total"] = actual_total
                    for tag in row["tags"]:
                        fade = tag["contrarian_fade"]
                        if fade in (home := row["home_team"], away := row["away_team"]):
                            direction = 1.0 if fade == home else -1.0
                            tag["grade"] = _result((actual_margin - float(row["spread_line"])) * direction)
                        elif fade in ("over", "under"):
                            direction = 1.0 if fade == "over" else -1.0
                            tag["grade"] = _result((actual_total - float(row["total_line"])) * direction)
                output.append(row)
            # Update only after the whole week's pregame snapshots are built.
            for game in week_games:
                if game["completed"]:
                    history[str(game["home_team"])].append(game)
                    history[str(game["away_team"])].append(game)
                if game["total_line"] is not None:
                    observed_lines.append(float(game["total_line"]))
    return output


def report(repository: NFLRepository, *, start_season: int = 2010,
           end_season: int = 2026) -> dict[str, Any]:
    rows = build_rows(repository, start_season, end_season)
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        for tag in row["tags"]:
            bucket = buckets.setdefault(tag["key"], {
                "label": tag["label"], "family": tag["family"],
                "market_narrative_role": (
                    "favorite" if tag["family"].endswith("side") else tag["market_narrative"]
                ),
                "contrarian_fade_role": (
                    "opponent" if tag["family"].endswith("side") else tag["contrarian_fade"]
                ),
                "wins": 0, "losses": 0, "pushes": 0, "by_season": {},
            })
            grade = tag.get("grade")
            if grade:
                grade_key = {"win": "wins", "loss": "losses", "push": "pushes"}[grade]
                bucket[grade_key] += 1
                season = str(row["season"])
                season_bucket = bucket["by_season"].setdefault(
                    season, {"wins": 0, "losses": 0, "pushes": 0})
                season_bucket[grade_key] += 1
    for bucket in buckets.values():
        decisions = bucket["wins"] + bucket["losses"]
        bucket["graded"] = decisions + bucket["pushes"]
        bucket["win_rate_ex_pushes"] = round(bucket["wins"] / decisions, 4) if decisions else None
        bucket["units_at_minus_110"] = round(bucket["wins"] - 1.1 * bucket["losses"], 2)
        for season_bucket in bucket["by_season"].values():
            season_decisions = season_bucket["wins"] + season_bucket["losses"]
            season_bucket["graded"] = season_decisions + season_bucket["pushes"]
            season_bucket["win_rate_ex_pushes"] = (
                round(season_bucket["wins"] / season_decisions, 4)
                if season_decisions else None
            )
    current = [row for row in rows if int(row["season"]) == int(end_season) and not row["completed"]]
    return {
        "version": MODEL_VERSION,
        "status": "research_only",
        "public_betting_data_present": False,
        "preseason_field_is_proxy": True,
        "leakage_control": "week-batched pregame snapshots",
        "rows": len(rows),
        "tag_results": buckets,
        "current_tagged_games": [row for row in current if row["tags"]],
    }
