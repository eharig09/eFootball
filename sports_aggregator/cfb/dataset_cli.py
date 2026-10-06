"""Run one CFBD dataset sync per process to keep Render memory bounded."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
import time
from typing import Callable

from dotenv import load_dotenv

from sports_aggregator.cfb.cfbd import CFBDClient, CFBDConfigurationError
from sports_aggregator.cfb.lines import store_lines
from sports_aggregator.cfb.models import Game, Player, Team, normalize_alias
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.sync import flatten_rankings


DATASETS = (
    "teams",
    "players",
    "games",
    "betting_lines",
    "media",
    "records",
    "coaches",
    "rankings",
    "team_stats",
    "advanced_stats",
    "core_ratings",
)


def _replace_team_players(
    repository: CFBRepository,
    season: int,
    team: str,
    players: list[dict],
) -> int:
    """Replace only one team's roster so the scheduler can fetch teams separately."""
    items = [Player.from_cfbd(item, season) for item in players]
    with repository.transaction() as connection:
        connection.execute("DELETE FROM players WHERE season=? AND team=?", (season, team))
        connection.executemany(
            "INSERT INTO players VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    season,
                    player.player_id,
                    player.first_name,
                    player.last_name,
                    normalize_alias(player.name),
                    player.team,
                    player.position,
                    player.jersey,
                    player.height,
                    player.weight,
                    player.class_year,
                )
                for player in items
            ],
        )
    return len(items)


def _sync_team_roster(repository: CFBRepository, client: CFBDClient, season: int,
                      team: str, force: bool = False) -> int:
    payload = client.get(
        "/roster",
        {"year": season, "team": team},
        cache_ttl_seconds=21600,
        force=force,
    )
    if not isinstance(payload, list):
        raise ValueError("CFBD roster returned a non-list payload")
    return _replace_team_players(repository, season, team, payload)


def sync_all_team_rosters(
    season: int,
    *,
    force: bool = False,
    retry_after: float = 60.0,
    sleep: Callable[[float], None] | None = None,
    emit: Callable[[str], None] = print,
) -> tuple[int, list[str]]:
    """Sync every FBS team's roster in this one process; returns (players stored, failed teams).

    The scheduler used to launch one interpreter per team: 138 starts at about half a second each
    is over a minute of pure process overhead on every run, even when every response is cached.
    The per-team calls, the per-team replace, and the failure isolation are unchanged -- one
    team failing does not stop the rest -- and teams that failed (CFBD answers HTTP 429 once it
    starts refusing) get one more pass after `retry_after` seconds, when the window has usually
    closed. Output lines match the single-team command so the logs read the same.
    """
    database = os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    repository = CFBRepository(database)
    repository.initialize()
    client = CFBDClient(raw_cache_path=os.getenv("CFBD_RAW_CACHE_PATH", "instance/cfbd_raw"))
    if not client.configured:
        raise CFBDConfigurationError("CFBD_API_KEY is required for sync")
    with closing(repository._connect()) as connection:
        teams = [row[0] for row in connection.execute(
            "SELECT school FROM teams WHERE lower(classification)='fbs' ORDER BY school") if row[0]]
    stored = 0
    pending = list(teams)
    for attempt in (1, 2):
        failed: list[str] = []
        for index, team in enumerate(pending, start=1):
            started = time.monotonic()
            try:
                count = _sync_team_roster(repository, client, season, team, force)
            except Exception as exc:  # one team failing must not stop the others
                failed.append(team)
                emit(f"players team={team}: failed ({str(exc)[:200]}) in {time.monotonic() - started:.1f}s")
                continue
            stored += count
            emit(f"players team={team}: success ({count}) in {time.monotonic() - started:.1f}s")
        pending = failed
        if attempt == 1 and pending and retry_after > 0:
            emit(f"{len(pending)} of {len(teams)} teams failed; waiting {retry_after:.0f}s before one retry")
            (sleep or time.sleep)(retry_after)
            continue
        break
    return stored, pending


def sync_dataset(
    name: str,
    season: int,
    *,
    force: bool = False,
    team: str | None = None,
) -> int:
    database = os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    repository = CFBRepository(database)
    repository.initialize()
    client = CFBDClient(raw_cache_path=os.getenv("CFBD_RAW_CACHE_PATH", "instance/cfbd_raw"))
    if not client.configured:
        raise CFBDConfigurationError("CFBD_API_KEY is required for sync")

    if name == "teams":
        return repository.replace_teams(Team.from_cfbd(item) for item in client.teams(season, force))
    if name == "players":
        if team:
            return _sync_team_roster(repository, client, season, team, force)
        return repository.replace_players(
            season, (Player.from_cfbd(item, season) for item in client.roster(season, force))
        )
    if name == "games":
        # An explicit games sync is the live-score pulse. The raw /games cache is
        # deliberately useful for ordinary reads, but accepting a cached response
        # here can make a successful 15-minute Scores cron simply rewrite the
        # previous score snapshot. Always hit CFBD for this dataset so in-progress
        # points and the completed flag advance on every scheduled pulse.
        return repository.replace_games(
            season, (Game.from_cfbd(item) for item in client.games(season, True))
        )
    if name == "betting_lines":
        return store_lines(repository, season, client.betting_lines(season, force))
    if name == "media":
        return repository.update_game_media(client.game_media(season, force))
    if name == "records":
        return repository.replace_records(
            season,
            (item for item in client.records(season, force)
             if item.get("classification") == "fbs"),
        )
    if name == "coaches":
        return repository.replace_coach_seasons(season, client.coaches(season, force))
    if name == "rankings":
        return repository.replace_rankings(season, flatten_rankings(client.rankings(season, force)))
    if name == "team_stats":
        return repository.replace_team_stats(season, client.team_stats(season, force))
    if name == "advanced_stats":
        return repository.replace_advanced_stats(season, client.advanced_team_stats(season, force))
    if name == "core_ratings":
        return repository.replace_core_ratings(season, client.core_ratings(season, force))
    raise ValueError(f"unknown dataset: {name}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Synchronize one CFBD dataset")
    parser.add_argument("dataset", choices=DATASETS)
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--team", default=None,
                        help="Limit supported datasets (currently roster) to one team")
    parser.add_argument("--all-teams", action="store_true",
                        help="players: sync every FBS team's roster in this one process")
    args = parser.parse_args(argv)
    started = datetime.now(timezone.utc)
    if args.all_teams:
        if args.dataset != "players":
            parser.error("--all-teams applies to the players dataset only")
        stored, failed = sync_all_team_rosters(args.year, force=args.force)
        seconds = (datetime.now(timezone.utc) - started).total_seconds()
        if failed:
            print(f"players: failed teams: {', '.join(failed[:8])} ({stored} players stored) in {seconds:.1f}s")
            return 1
        print(f"players: success ({stored}) in {seconds:.1f}s")
        return 0
    count = sync_dataset(args.dataset, args.year, force=args.force, team=args.team)
    seconds = (datetime.now(timezone.utc) - started).total_seconds()
    scope = f" team={args.team}" if args.team else ""
    print(f"{args.dataset}{scope}: success ({count}) in {seconds:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
