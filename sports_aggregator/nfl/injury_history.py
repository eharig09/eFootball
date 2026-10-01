"""Historical NFL injury-report and snap-count backfill for availability research.

The live ESPN snapshot (injury_reports) only covers the current season, and
snap_counts was only populated from 2025. This pulls nflverse's weekly injury
reports (2009+) and snap counts (2013+) so availability features can be
backtested. Both writers are per-season and idempotent.
"""
from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.nflverse import NflverseClient
from sports_aggregator.nfl.repository import NFLRepository

INJURY_FIRST_SEASON = 2009
SNAP_FIRST_SEASON = 2013


def _records(frame) -> list[dict[str, Any]]:
    if frame is None or len(frame) == 0:
        return []
    return frame.where(frame.notna(), None).to_dict("records")


def backfill(repository: NFLRepository, client: NflverseClient, start_season: int,
             end_season: int, *, snaps_end_season: int | None = None,
             force: bool = False) -> dict[str, dict[int, int]]:
    repository.initialize()
    out: dict[str, dict[int, int]] = {"injuries": {}, "snap_counts": {}}
    for season in range(int(start_season), int(end_season) + 1):
        if season >= INJURY_FIRST_SEASON:
            rows = _records(client.load_injuries([season], force=force))
            out["injuries"][season] = repository.replace_injury_history(season, rows)
        if season >= SNAP_FIRST_SEASON and season <= (snaps_end_season if snaps_end_season is not None else end_season):
            rows = _records(client.load_snap_counts([season], force=force))
            out["snap_counts"][season] = repository.replace_snap_counts(season, rows) if rows else 0
    return out
