"""Incremental weekly CFBD synchronization into canonical SQLite tables."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Callable, Iterable

from sports_aggregator.cfb.cfbd import CFBDClient
from sports_aggregator.cfb.lines import store_lines
from sports_aggregator.cfb.models import (
    Game, Player, PollRanking, SyncDatasetResult, SyncReport, Team, optional_int,
)
from sports_aggregator.cfb.repository import CFBRepository

LOGGER = logging.getLogger(__name__)


def _as_list(dataset: str, payload: Any) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError(f"CFBD {dataset} returned {type(payload).__name__}, expected a list")
    return payload


def flatten_rankings(payload: Iterable[dict[str, Any]]) -> list[PollRanking]:
    rankings: list[PollRanking] = []
    for snapshot in payload:
        for poll in snapshot.get("polls") or []:
            for item in poll.get("ranks") or []:
                rankings.append(PollRanking(
                    season=int(snapshot["season"]), season_type=str(snapshot["seasonType"]),
                    week=int(snapshot["week"]), poll=str(poll["poll"]),
                    is_final=bool(poll.get("isFinal")), rank=int(item["rank"]),
                    team_id=optional_int(item.get("teamId")), school=str(item["school"]),
                    conference=item.get("conference"),
                    first_place_votes=optional_int(item.get("firstPlaceVotes")),
                    points=optional_int(item.get("points")),
                ))
    return rankings


class CFBDataSync:
    """Sync datasets independently so a paid-tier or transient failure is non-fatal."""

    def __init__(self, client: CFBDClient, repository: CFBRepository) -> None:
        self.client = client
        self.repository = repository

    def sync(self, season: int, *, force: bool = False,
             include_advanced: bool = True) -> SyncReport:
        started_at = datetime.now(timezone.utc)
        self.repository.initialize()
        jobs: list[tuple[str, Callable[[], int]]] = [
            ("teams", lambda: self.repository.replace_teams(
                Team.from_cfbd(item) for item in _as_list("teams", self.client.teams(season, force)))),
            ("players", lambda: self.repository.replace_players(
                season, (Player.from_cfbd(item, season)
                         for item in _as_list("roster", self.client.roster(season, force))))),
            ("recruits", lambda: self.repository.replace_recruits(
                season, _as_list("recruits", self.client.recruits(season, force)))),
            ("games", lambda: self.repository.replace_games(
                season, (Game.from_cfbd(item)
                         for item in _as_list("games", self.client.games(season, force))))),
            ("betting_lines", lambda: store_lines(
                self.repository, season,
                _as_list("betting lines", self.client.betting_lines(season, force)))),
            ("media", lambda: self.repository.update_game_media(
                _as_list("media", self.client.game_media(season, force)))),
            ("records", lambda: self.repository.replace_records(
                season, (item for item in _as_list("records", self.client.records(season, force))
                         if item.get("classification") == "fbs"))),
            ("coaches", lambda: self.repository.replace_coach_seasons(
                season, _as_list("coaches", self.client.coaches(season, force)))),
            ("rankings", lambda: self.repository.replace_rankings(
                season, flatten_rankings(_as_list("rankings", self.client.rankings(season, force))))),
            ("team_stats", lambda: self.repository.replace_team_stats(
                season, _as_list("team stats", self.client.team_stats(season, force)))),
        ]
        if include_advanced:
            jobs.extend([
                ("advanced_stats", lambda: self.repository.replace_advanced_stats(
                    season, _as_list("advanced stats", self.client.advanced_team_stats(season, force)))),
                ("core_ratings", lambda: self.repository.replace_core_ratings(
                    season, _as_list("CORE ratings", self.client.core_ratings(season, force)))),
                ("player_ppa", lambda: self.repository.replace_player_ppa(
                    season, _as_list("player PPA", self.client.ppa_players_season(season, force)))),
            ])

        results: list[SyncDatasetResult] = []
        for name, job in jobs:
            try:
                count = job()
                results.append(SyncDatasetResult(name, count, "success"))
                LOGGER.info("CFBD sync dataset=%s season=%s count=%s", name, season, count)
            except Exception as exc:
                results.append(SyncDatasetResult(name, 0, "failed", str(exc)))
                LOGGER.exception("CFBD sync failed dataset=%s season=%s", name, season)

        # Intelligence enrichments run after games/lines so their timestamped
        # evidence sees the freshest canonical state. They are deliberately
        # non-fatal: losing play-by-play must not make scores/lines look stale.
        try:
            from sports_aggregator.cfb.pregame_snapshots import capture_due
            packet = capture_due(self.repository, season=season)
            results.append(SyncDatasetResult("pregame_snapshots", int(packet.get("count") or 0), "success"))
        except Exception as exc:
            results.append(SyncDatasetResult("pregame_snapshots", 0, "skipped", str(exc)))
            LOGGER.exception("Pregame snapshot capture degraded season=%s", season)

        try:
            from sports_aggregator.cfb.play_by_play import sync_recent_plays
            if not hasattr(self.client, "get"):
                raise RuntimeError("client does not expose generic /plays access")
            packet = sync_recent_plays(
                self.repository, self.client, season=season, recent_weeks=2, force=force)
            results.append(SyncDatasetResult("play_by_play", int(packet.get("plays") or 0), "success"))
        except Exception as exc:
            results.append(SyncDatasetResult("play_by_play", 0, "skipped", str(exc)))
            LOGGER.exception("Play-by-play enrichment degraded season=%s", season)

        report = SyncReport(season=season, started_at=started_at,
                            finished_at=datetime.now(timezone.utc), datasets=tuple(results))
        self.repository.record_sync(report)
        return report
