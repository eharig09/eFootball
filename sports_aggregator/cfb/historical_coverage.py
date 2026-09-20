"""Year-by-year historical coverage audit for CFB projection research.

The purpose is to decide how far the walk-forward backtest can be extended
without quietly mixing seasons with materially different source coverage.
"""
from __future__ import annotations

import csv
import json
import os
from contextlib import closing
from typing import Any

from sports_aggregator.cfb.repository import CFBRepository


CORE_TEAM_TABLES = (
    ("cfb_team_game_pace", "pace_team_rows"),
    ("cfb_team_game_scoring", "scoring_team_rows"),
    ("cfb_team_game_special_teams", "special_teams_team_rows"),
    ("cfb_team_game_drive_outcomes", "drive_outcomes_team_rows"),
)


def _tables(repository: CFBRepository) -> set[str]:
    with repository._reader() as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def _columns(repository: CFBRepository, table: str) -> set[str]:
    with repository._reader() as connection:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }


def _scalar(repository: CFBRepository, sql: str, params=()) -> int:
    with repository._reader() as connection:
        return int(connection.execute(sql, params).fetchone()[0] or 0)


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _market_game_counts(
    repository: CFBRepository, season: int, tables: set[str]
) -> dict[str, int]:
    if "game_lines" not in tables:
        return {
            "market_games": 0,
            "spread_games": 0,
            "spread_open_games": 0,
            "total_games": 0,
            "total_open_games": 0,
            "spread_and_total_games": 0,
            "full_open_close_market_games": 0,
        }
    cols = _columns(repository, "game_lines")
    fields = {
        "spread": "spread",
        "spread_open": "spread_open",
        "total": "over_under",
        "total_open": "over_under_open",
    }
    available = {k: v for k, v in fields.items() if v in cols}

    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                """SELECT game_id,
                          MAX(CASE WHEN spread IS NOT NULL THEN 1 ELSE 0 END) AS has_spread,
                          MAX(CASE WHEN spread_open IS NOT NULL THEN 1 ELSE 0 END) AS has_spread_open,
                          MAX(CASE WHEN over_under IS NOT NULL THEN 1 ELSE 0 END) AS has_total,
                          MAX(CASE WHEN over_under_open IS NOT NULL THEN 1 ELSE 0 END) AS has_total_open
                   FROM game_lines
                   WHERE season=?
                   GROUP BY game_id""",
                (int(season),),
            )
        ]
    return {
        "market_games": len(rows),
        "spread_games": sum(int(r["has_spread"]) for r in rows),
        "spread_open_games": sum(int(r["has_spread_open"]) for r in rows),
        "total_games": sum(int(r["has_total"]) for r in rows),
        "total_open_games": sum(int(r["has_total_open"]) for r in rows),
        "spread_and_total_games": sum(
            int(r["has_spread"] and r["has_total"]) for r in rows
        ),
        "full_open_close_market_games": sum(
            int(
                r["has_spread"]
                and r["has_spread_open"]
                and r["has_total"]
                and r["has_total_open"]
            )
            for r in rows
        ),
    }


def _backtest_game_counts(
    repository: CFBRepository, season: int, tables: set[str]
) -> dict[str, int]:
    if "cfb_projection_backtest" not in tables:
        return {"backtest_games": 0, "backtest_team_rows": 0}
    with repository._reader() as connection:
        row = connection.execute(
            """SELECT COUNT(DISTINCT game_id) AS games,
                      COUNT(*) AS rows
               FROM cfb_projection_backtest
               WHERE season=?""",
            (int(season),),
        ).fetchone()
    return {
        "backtest_games": int(row["games"] or 0),
        "backtest_team_rows": int(row["rows"] or 0),
    }


def _team_table_count(
    repository: CFBRepository,
    table: str,
    season: int,
    tables: set[str],
) -> int:
    if table not in tables:
        return 0
    cols = _columns(repository, table)
    if "season" in cols:
        return _scalar(
            repository,
            f"SELECT COUNT(*) FROM {table} WHERE season=?",
            (int(season),),
        )
    if "game_id" in cols:
        return _scalar(
            repository,
            f"""SELECT COUNT(*) FROM {table} t
                JOIN games g ON g.game_id=t.game_id
                WHERE g.season=?""",
            (int(season),),
        )
    return 0


def _play_counts(
    repository: CFBRepository, season: int, tables: set[str]
) -> tuple[int, int]:
    pbp = (
        _scalar(
            repository,
            "SELECT COUNT(*) FROM cfb_plays WHERE season=?",
            (int(season),),
        )
        if "cfb_plays" in tables
        else 0
    )
    derived = 0
    if "cfb_play_metrics" in tables and "cfb_plays" in tables:
        derived = _scalar(
            repository,
            """SELECT COUNT(*)
               FROM cfb_play_metrics m
               JOIN cfb_plays p USING(play_id)
               WHERE p.season=?""",
            (int(season),),
        )
    return pbp, derived


def _grade(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons = []
    games = int(row["completed_games"])
    expected_team = int(row["expected_team_rows"])
    if games == 0:
        return "NO_GAMES", ["no completed games"]

    core_cov = min(
        float(row.get("pace_coverage_pct") or 0),
        float(row.get("scoring_coverage_pct") or 0),
        float(row.get("drive_outcomes_coverage_pct") or 0),
    )
    market_close = min(
        float(row.get("spread_coverage_pct") or 0),
        float(row.get("total_coverage_pct") or 0),
    )
    market_open = min(
        float(row.get("spread_open_coverage_pct") or 0),
        float(row.get("total_open_coverage_pct") or 0),
    )

    if row["pbp_rows"] <= 0:
        reasons.append("no play-by-play")
    if row["derived_play_rows"] <= 0:
        reasons.append("no derived play metrics")
    if core_cov < 90:
        reasons.append(f"core team-game coverage {core_cov:.1f}%")
    if market_close < 80:
        reasons.append(f"closing market coverage {market_close:.1f}%")
    if market_open < 60:
        reasons.append(f"opening market coverage {market_open:.1f}%")

    if (
        row["pbp_rows"] > 0
        and row["derived_play_rows"] > 0
        and core_cov >= 95
        and market_close >= 90
        and market_open >= 80
    ):
        return "STRONG", reasons
    if (
        row["pbp_rows"] > 0
        and row["derived_play_rows"] > 0
        and core_cov >= 90
        and market_close >= 80
    ):
        return "USABLE", reasons
    if core_cov >= 75 and market_close >= 60:
        return "PARTIAL", reasons
    return "WEAK", reasons


def season_row(
    repository: CFBRepository, season: int, tables: set[str] | None = None
) -> dict[str, Any]:
    tables = tables or _tables(repository)
    completed = _scalar(
        repository,
        """SELECT COUNT(*) FROM games
           WHERE completed=1 AND season=?""",
        (int(season),),
    )
    expected_team_rows = completed * 2
    pbp, derived = _play_counts(repository, season, tables)

    row: dict[str, Any] = {
        "season": int(season),
        "completed_games": completed,
        "expected_team_rows": expected_team_rows,
        "pbp_rows": pbp,
        "derived_play_rows": derived,
    }
    for table, key in CORE_TEAM_TABLES:
        row[key] = _team_table_count(repository, table, season, tables)

    row.update(_market_game_counts(repository, season, tables))
    row.update(_backtest_game_counts(repository, season, tables))

    for source, target in (
        ("pace_team_rows", "pace_coverage_pct"),
        ("scoring_team_rows", "scoring_coverage_pct"),
        ("special_teams_team_rows", "special_teams_coverage_pct"),
        ("drive_outcomes_team_rows", "drive_outcomes_coverage_pct"),
        ("backtest_team_rows", "backtest_team_coverage_pct"),
    ):
        row[target] = _pct(int(row[source]), expected_team_rows)

    for source, target in (
        ("market_games", "market_any_coverage_pct"),
        ("spread_games", "spread_coverage_pct"),
        ("spread_open_games", "spread_open_coverage_pct"),
        ("total_games", "total_coverage_pct"),
        ("total_open_games", "total_open_coverage_pct"),
        ("spread_and_total_games", "spread_and_total_coverage_pct"),
        ("full_open_close_market_games", "full_open_close_market_coverage_pct"),
        ("backtest_games", "backtest_game_coverage_pct"),
    ):
        row[target] = _pct(int(row[source]), completed)

    grade, reasons = _grade(row)
    row["research_grade"] = grade
    row["limitations"] = "; ".join(reasons)
    return row


def audit(
    repository: CFBRepository, *, from_season: int, to_season: int
) -> dict[str, Any]:
    if from_season > to_season:
        raise ValueError("from_season must not be after to_season")
    tables = _tables(repository)
    rows = [
        season_row(repository, season, tables)
        for season in range(int(from_season), int(to_season) + 1)
    ]

    strong = [r["season"] for r in rows if r["research_grade"] == "STRONG"]
    usable = [
        r["season"]
        for r in rows
        if r["research_grade"] in {"STRONG", "USABLE"}
    ]
    contiguous = []
    if usable:
        last = max(usable)
        cursor = last
        usable_set = set(usable)
        while cursor in usable_set:
            contiguous.append(cursor)
            cursor -= 1
        contiguous.sort()

    recommendation = {
        "strong_seasons": strong,
        "usable_seasons": usable,
        "latest_contiguous_usable_window": contiguous,
        "recommended_backtest_from": contiguous[0] if contiguous else None,
        "recommended_backtest_to": contiguous[-1] if contiguous else None,
        "note": (
            "Choose the contiguous recent window where production inputs and closing "
            "market coverage are at least usable. Opening-line studies require a "
            "stricter subset and should be filtered by full_open_close_market coverage."
        ),
    }
    return {
        "version": "historical-backtest-coverage-v1",
        "from_season": int(from_season),
        "to_season": int(to_season),
        "seasons": rows,
        "recommendation": recommendation,
    }


def export_report(payload: dict[str, Any], output_dir: str) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "historical_backtest_coverage.json")
    csv_path = os.path.join(output_dir, "historical_backtest_coverage.csv")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    rows = payload["seasons"]
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(rows[0].keys()) if rows else ["season"]
        )
        writer.writeheader()
        writer.writerows(rows)
    return {"json": json_path, "csv": csv_path}
