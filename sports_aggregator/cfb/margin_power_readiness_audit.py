"""Diagnose current-season Margin Power readiness without changing the model."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb.repository import CFBRepository

DEFAULT_SEASON = 2026
SAMPLE_LIMIT = 12


def _completed_games(repository: CFBRepository, season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT game_id,season,week,start_date,home_team,away_team,
                          home_points,away_points,completed
                   FROM games
                   WHERE season=?
                     AND completed=1
                     AND home_points IS NOT NULL
                     AND away_points IS NOT NULL
                   ORDER BY week,start_date,game_id""",
                (int(season),),
            )
        ]


def _scheduled_games(repository: CFBRepository, season: int, week: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT game_id,season,week,start_date,home_team,away_team,
                          home_points,away_points,completed
                   FROM games
                   WHERE season=? AND week=?
                   ORDER BY start_date,game_id""",
                (int(season), int(week)),
            )
        ]


def _infer_upcoming_week(repository: CFBRepository, season: int) -> int | None:
    with repository._reader() as connection:
        row = connection.execute(
            """SELECT MIN(week)
               FROM games
               WHERE season=? AND completed=0 AND week IS NOT NULL""",
            (int(season),),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _upcoming_week_readiness(
    completed_games: list[dict[str, Any]],
    scheduled_games: list[dict[str, Any]],
    upcoming_week: int,
) -> list[dict[str, Any]]:
    prior = [
        game for game in completed_games
        if game.get("week") is not None and int(game["week"]) < int(upcoming_week)
    ]
    ratings, counts = ipl._solve_srs(prior)
    output = []
    for game in scheduled_games:
        home = str(game["home_team"])
        away = str(game["away_team"])
        home_prior = int(counts.get(home, 0))
        away_prior = int(counts.get(away, 0))
        both_ready = (
            home_prior >= ipl.MIN_SRS_GAMES
            and away_prior >= ipl.MIN_SRS_GAMES
        )
        projected_home_margin = None
        if both_ready and home in ratings and away in ratings:
            projected_home_margin = (
                float(ratings[home]) - float(ratings[away]) + float(ipl.HFA_POINTS)
            )
        output.append({
            **game,
            "home_prior_games": home_prior,
            "away_prior_games": away_prior,
            "home_ready": home_prior >= ipl.MIN_SRS_GAMES,
            "away_ready": away_prior >= ipl.MIN_SRS_GAMES,
            "both_ready": both_ready,
            "diagnostic_home_margin_if_played_now": projected_home_margin,
        })
    return output


def _readiness_rows(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mirror Margin Power's prior-week eligibility rule game by game."""
    weeks = sorted({int(g["week"]) for g in games if g.get("week") is not None})
    prior: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    for week in weeks:
        current = [g for g in games if int(g["week"]) == int(week)]
        _ratings, counts = ipl._solve_srs(prior)
        for game in current:
            home = str(game["home_team"])
            away = str(game["away_team"])
            home_prior = int(counts.get(home, 0))
            away_prior = int(counts.get(away, 0))
            output.append({
                **game,
                "home_prior_games": home_prior,
                "away_prior_games": away_prior,
                "home_ready": home_prior >= ipl.MIN_SRS_GAMES,
                "away_ready": away_prior >= ipl.MIN_SRS_GAMES,
                "both_ready": (
                    home_prior >= ipl.MIN_SRS_GAMES
                    and away_prior >= ipl.MIN_SRS_GAMES
                ),
            })
        prior.extend(current)
    return output


def _by_week(rows: list[dict[str, Any]], snapshots: dict[tuple[int, str], float],
             lens_by_game: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["week"])].append(row)

    output = []
    for week in sorted(grouped):
        current = grouped[week]
        output.append({
            "week": week,
            "completed_games": len(current),
            "home_3_plus_prior": sum(bool(r["home_ready"]) for r in current),
            "away_3_plus_prior": sum(bool(r["away_ready"]) for r in current),
            "both_3_plus_prior": sum(bool(r["both_ready"]) for r in current),
            "margin_power_snapshot_games": sum(
                (int(r["game_id"]), str(r["home_team"])) in snapshots
                for r in current
            ),
            "lens_rows": sum(int(r["game_id"]) in lens_by_game for r in current),
            "lens_rows_with_margin_power_edge": sum(
                lens_by_game.get(int(r["game_id"]), {}).get("margin_power_edge") is not None
                for r in current
            ),
        })
    return output


def report(
    repository: CFBRepository,
    *,
    season: int = DEFAULT_SEASON,
    upcoming_week: int | None = None,
) -> dict[str, Any]:
    season = int(season)
    games = _completed_games(repository, season)
    readiness = _readiness_rows(games)
    snapshots = ipl.margin_power_snapshots(
        repository, from_season=season, to_season=season
    )
    lens_rows = ipl.build_lens_rows(repository, test_season=season)
    lens_by_game = {
        int(row["game_id"]): row
        for row in lens_rows
        if int(row.get("season", -1)) == season
    }

    enriched = []
    for row in readiness:
        gid = int(row["game_id"])
        home_key = (gid, str(row["home_team"]))
        away_key = (gid, str(row["away_team"]))
        lens = lens_by_game.get(gid)
        enriched.append({
            **row,
            "home_snapshot_exists": home_key in snapshots,
            "away_snapshot_exists": away_key in snapshots,
            "lens_row_exists": lens is not None,
            "margin_power_edge_exists": (
                lens is not None and lens.get("margin_power_edge") is not None
            ),
        })

    if upcoming_week is None:
        upcoming_week = _infer_upcoming_week(repository, season)
    upcoming_games = (
        _scheduled_games(repository, season, int(upcoming_week))
        if upcoming_week is not None else []
    )
    upcoming_rows = (
        _upcoming_week_readiness(games, upcoming_games, int(upcoming_week))
        if upcoming_week is not None else []
    )
    upcoming_eligible = [r for r in upcoming_rows if r["both_ready"]]

    eligible = [r for r in enriched if r["both_ready"]]
    snapshot_games = [r for r in enriched if r["home_snapshot_exists"]]
    eligible_without_snapshot = [
        r for r in eligible if not r["home_snapshot_exists"]
    ]
    snapshot_without_lens_edge = [
        r for r in snapshot_games if not r["margin_power_edge_exists"]
    ]

    return {
        "version": "cfb-margin-power-readiness-audit-v1",
        "season": season,
        "min_srs_games": int(ipl.MIN_SRS_GAMES),
        "summary": {
            "completed_games": len(enriched),
            "games_home_3_plus_prior": sum(bool(r["home_ready"]) for r in enriched),
            "games_away_3_plus_prior": sum(bool(r["away_ready"]) for r in enriched),
            "games_both_3_plus_prior": len(eligible),
            "margin_power_snapshot_games": len(snapshot_games),
            "target_season_lens_rows": len(lens_by_game),
            "lens_rows_with_margin_power_edge": sum(
                row.get("margin_power_edge") is not None
                for row in lens_by_game.values()
            ),
            "eligible_without_snapshot": len(eligible_without_snapshot),
            "snapshot_without_lens_margin_power_edge": len(snapshot_without_lens_edge),
        },
        "by_week": _by_week(enriched, snapshots, lens_by_game),
        "upcoming_week": {
            "week": upcoming_week,
            "scheduled_games": len(upcoming_rows),
            "games_home_3_plus_prior": sum(bool(r["home_ready"]) for r in upcoming_rows),
            "games_away_3_plus_prior": sum(bool(r["away_ready"]) for r in upcoming_rows),
            "games_both_3_plus_prior": len(upcoming_eligible),
            "eligible_rate": (
                round(len(upcoming_eligible) / len(upcoming_rows), 4)
                if upcoming_rows else None
            ),
            "eligible_games": upcoming_eligible,
            "all_scheduled_games": upcoming_rows,
            "note": (
                "These are pregame readiness checks using only completed prior weeks. "
                "diagnostic_home_margin_if_played_now mirrors the frozen SRS equation "
                "but is not persisted as a production snapshot."
            ),
        },
        "sample_theoretically_eligible_games": eligible[:SAMPLE_LIMIT],
        "sample_eligible_without_snapshot": eligible_without_snapshot[:SAMPLE_LIMIT],
        "sample_snapshot_without_lens_edge": snapshot_without_lens_edge[:SAMPLE_LIMIT],
        "diagnosis": {
            "eligibility_exists": bool(eligible),
            "snapshot_generation_matches_eligibility": not eligible_without_snapshot,
            "snapshot_to_lens_join_matches": not snapshot_without_lens_edge,
            "interpretation": (
                "If games_both_3_plus_prior is zero, current-season Margin Power is "
                "correctly cold-started. If it is positive but eligible_without_snapshot "
                "is nonzero, inspect snapshot generation. If snapshots exist but "
                "snapshot_without_lens_margin_power_edge is nonzero, inspect the lens join."
            ),
        },
        "notes": [
            "Diagnostic only; no Margin Power threshold, SRS equation, or routing rule is changed.",
            "Prior-game eligibility is computed by completed week, matching margin_power_snapshots.",
            "The upcoming-week section includes scheduled games and uses only completed prior weeks, so Week 4 can be inspected before its games are played.",
            "Upcoming diagnostic margins do not alter or persist Margin Power; they only show whether the frozen SRS inputs are already sufficient.",
            "Samples are ordered by week/start_date/game_id and capped for compact output.",
        ],
    }
