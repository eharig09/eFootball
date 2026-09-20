"""Readiness audit for extending the CFB projection backtest into an older season."""
from __future__ import annotations

from typing import Any

from sports_aggregator.cfb.historical_coverage import season_row, _tables
from sports_aggregator.cfb.repository import CFBRepository


PIPELINE = (
    ("games", "Canonical completed games", None),
    ("market", "Stored closing/opening market lines", None),
    ("pbp", "Raw CFBD play-by-play", "python -m sports_aggregator.cfb.pbp_cli backfill --year {year}"),
    ("derived", "pbp-v1 play/drive metrics", "python -m sports_aggregator.cfb.pbp_cli derive --year {year}"),
    ("pace", "Team-game pace / drives / volume", "python -m sports_aggregator.cfb.pbp_cli build-team-pace --from-year {year} --to-year {year}"),
    ("scoring", "Team-game turnovers / red zone", "python -m sports_aggregator.cfb.pbp_cli build-team-scoring --from-year {year} --to-year {year}"),
    ("special_teams", "Team-game field position / special teams", "python -m sports_aggregator.cfb.pbp_cli build-team-special-teams --from-year {year} --to-year {year}"),
    ("drive_outcomes", "Team-game offensive points / PPD", "python -m sports_aggregator.cfb.pbp_cli build-team-drive-outcomes --from-year {year} --to-year {year}"),
    ("xpoints_dataset", "xPoints training dataset", "python -m sports_aggregator.cfb.pbp_cli build-xpoints-dataset --from-year {year} --to-year {year}"),
    ("backtest_actuals", "Projection backtest actuals preparation", "python -m sports_aggregator.cfb.projection_backtest_cli prepare --year {year}"),
)


def _count(repository: CFBRepository, sql: str, params=()) -> int:
    with repository._reader() as connection:
        return int(connection.execute(sql, params).fetchone()[0] or 0)


def _table_count_for_season(
    repository: CFBRepository, table: str, season: int, tables: set[str]
) -> int:
    if table not in tables:
        return 0
    with repository._reader() as connection:
        cols = {str(r[1]) for r in connection.execute(f"PRAGMA table_info({table})")}
        if "season" in cols:
            return int(connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE season=?", (int(season),)
            ).fetchone()[0] or 0)
        if "game_id" in cols:
            return int(connection.execute(
                f"""SELECT COUNT(*) FROM {table} t
                    JOIN games g ON g.game_id=t.game_id
                    WHERE g.season=?""", (int(season),)
            ).fetchone()[0] or 0)
    return 0


def _completed_weeks(repository: CFBRepository, season: int) -> list[int]:
    with repository._reader() as connection:
        return [
            int(r[0]) for r in connection.execute(
                """SELECT DISTINCT week FROM games
                   WHERE season=? AND completed=1 AND week IS NOT NULL
                   ORDER BY week""", (int(season),)
            )
        ]


def _games_without_plays(repository: CFBRepository, season: int, tables: set[str]) -> int:
    if "cfb_plays" not in tables:
        return _count(
            repository,
            "SELECT COUNT(*) FROM games WHERE season=? AND completed=1",
            (int(season),),
        )
    return _count(
        repository,
        """SELECT COUNT(*) FROM games g
           WHERE g.season=? AND g.completed=1
             AND NOT EXISTS (
               SELECT 1 FROM cfb_plays p WHERE p.game_id=g.game_id
             )""",
        (int(season),),
    )


def readiness(repository: CFBRepository, *, season: int) -> dict[str, Any]:
    season = int(season)
    tables = _tables(repository)
    cov = season_row(repository, season, tables)
    expected_team = int(cov["expected_team_rows"])

    xpoints_rows = _table_count_for_season(
        repository, "cfb_xpoints_dataset", season, tables)
    backtest_games = int(cov["backtest_games"])
    complete_weeks = _completed_weeks(repository, season)
    games_without_plays = _games_without_plays(repository, season, tables)

    statuses = {
        "games": {
            "ready": cov["completed_games"] > 0,
            "rows": cov["completed_games"],
            "expected": cov["completed_games"],
        },
        "market": {
            "ready": (cov["spread_coverage_pct"] or 0) >= 80
                     and (cov["total_coverage_pct"] or 0) >= 80,
            "closing_spread_pct": cov["spread_coverage_pct"],
            "closing_total_pct": cov["total_coverage_pct"],
            "opening_spread_pct": cov["spread_open_coverage_pct"],
            "opening_total_pct": cov["total_open_coverage_pct"],
        },
        "pbp": {
            "ready": cov["pbp_rows"] > 0 and games_without_plays == 0,
            "rows": cov["pbp_rows"],
            "games_without_plays": games_without_plays,
            "weeks": complete_weeks,
        },
        "derived": {
            "ready": cov["derived_play_rows"] > 0
                     and cov["derived_play_rows"] == cov["pbp_rows"],
            "rows": cov["derived_play_rows"],
            "raw_play_rows": cov["pbp_rows"],
        },
        "pace": {
            "ready": (cov["pace_coverage_pct"] or 0) >= 95,
            "rows": cov["pace_team_rows"], "expected": expected_team,
        },
        "scoring": {
            "ready": (cov["scoring_coverage_pct"] or 0) >= 95,
            "rows": cov["scoring_team_rows"], "expected": expected_team,
        },
        "special_teams": {
            "ready": (cov["special_teams_coverage_pct"] or 0) >= 95,
            "rows": cov["special_teams_team_rows"], "expected": expected_team,
        },
        "drive_outcomes": {
            "ready": (cov["drive_outcomes_coverage_pct"] or 0) >= 95,
            "rows": cov["drive_outcomes_team_rows"], "expected": expected_team,
        },
        "xpoints_dataset": {
            "ready": xpoints_rows > 0,
            "rows": xpoints_rows,
        },
        "backtest_actuals": {
            "ready": backtest_games > 0,
            "games": backtest_games,
            "coverage_pct": cov["backtest_game_coverage_pct"],
        },
    }

    steps = []
    for key, label, command in PIPELINE:
        state = statuses[key]
        steps.append({
            "stage": key,
            "label": label,
            "ready": bool(state["ready"]),
            "command": command.format(year=season) if command and not state["ready"] else None,
            "detail": state,
        })

    # A held-out season must train xPoints strictly before that season.
    prior_xpoints_rows = 0
    if "cfb_xpoints_dataset" in tables:
        prior_xpoints_rows = _count(
            repository,
            "SELECT COUNT(*) FROM cfb_xpoints_dataset WHERE season < ?",
            (season,),
        )
    full_walk_forward_ready = (
        all(statuses[k]["ready"] for k in (
            "games", "market", "pbp", "derived", "pace",
            "scoring", "special_teams", "drive_outcomes", "xpoints_dataset"
        ))
        and prior_xpoints_rows >= 50
    )

    commands = [
        step["command"] for step in steps
        if step["command"] is not None
    ]
    if full_walk_forward_ready:
        commands.append(
            "python -m sports_aggregator.cfb.projection_backtest_cli run "
            f"--year {season} --points-train-from-year <earliest-prior-training-season>"
        )

    return {
        "version": "historical-backfill-readiness-v1",
        "season": season,
        "coverage": cov,
        "pipeline": steps,
        "missing_stages": [s["stage"] for s in steps if not s["ready"]],
        "prior_xpoints_training_rows": prior_xpoints_rows,
        "full_walk_forward_ready": full_walk_forward_ready,
        "recommended_commands": commands,
        "training_dependency": {
            "required": True,
            "ready": prior_xpoints_rows >= 50,
            "detail": (
                f"A {season} held-out fold needs xPoints training rows from seasons before {season}. "
                f"Current prior-season xPoints rows: {prior_xpoints_rows}. "
                "If zero or sparse, backfill the preceding season before treating this season as "
                "a fully comparable walk-forward test."
            ),
        },
        "interpretation": (
            "A season can have excellent market coverage and still be unusable for the full "
            "projection backtest until raw PBP and derived team-game actuals are populated. "
            "Backfilling the target season also improves the training history available to later folds."
        ),
    }
