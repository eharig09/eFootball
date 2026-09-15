"""Bounded ESPN adapters for current NFL staff and availability context."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any

import requests

from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.pressure_rates import pressure_rate_rows
from sports_aggregator.nfl.repository import NFLRepository


INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
STAFF_PATH = Path(__file__).resolve().parents[2] / "data" / "nfl" / "team_staff_2026.csv"
SCHEME_RATE_PATH = (Path(__file__).resolve().parents[2] / "data" / "nfl"
                    / "Blitz+Box Rate - Sheet1.csv")
INJURY_TTL_SECONDS = 30 * 60
INACTIVE_DESIGNATIONS = {"", "A", "ACTIVE", "HEALTHY"}


class ESPNNFLClient:
    """Fetch the single league injury document and keep a small raw cache."""

    def __init__(self, cache_path: str | os.PathLike[str], *,
                 session: requests.Session | None = None, clock=time.time) -> None:
        self.cache_path = Path(cache_path)
        self.session = session or requests.Session()
        self.clock = clock

    def load_injuries(self, *, force: bool = False) -> dict[str, Any]:
        path = self.cache_path / "espn_injuries.json"
        if (not force and path.exists() and
                self.clock() - path.stat().st_mtime < INJURY_TTL_SECONDS):
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            response = self.session.get(INJURIES_URL, timeout=120)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, OSError):
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            raise
        self.cache_path.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.part")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
        return payload


def injury_rows(payload: dict[str, Any], season: int) -> list[dict[str, Any]]:
    """Normalize ESPN's current report and omit active transaction-news rows."""
    rows = []
    fetched_at = str(payload.get("timestamp") or datetime.now(timezone.utc).isoformat())
    for club in payload.get("injuries") or ():
        for item in club.get("injuries") or ():
            athlete = item.get("athlete") or {}
            team = athlete.get("team") or {}
            injury_type = item.get("type") or {}
            details = item.get("details") or {}
            designation = str(injury_type.get("abbreviation") or item.get("status") or "").upper()
            if designation in INACTIVE_DESIGNATIONS or str(item.get("status") or "").lower() == "active":
                continue
            notes = (item.get("notes") or {}).get("items") or []
            latest_note = notes[0] if notes else {}
            espn_id = str(athlete.get("id") or "").strip()
            if not espn_id:
                player_link = next((link.get("href") for link in athlete.get("links") or ()
                                    if "/id/" in str(link.get("href") or "")), "")
                match = re.search(r"/id/(\d+)", str(player_link))
                espn_id = match.group(1) if match else ""
            if not espn_id or not team.get("abbreviation"):
                continue
            practice = (details.get("practiceStatus") or item.get("practiceStatus") or
                        details.get("practice") or item.get("practice"))
            rows.append({
                "season": season, "team": canon_team(team.get("abbreviation")),
                "injury_id": str(item.get("id") or f"{espn_id}:{designation}"),
                "espn_id": espn_id, "player_name": athlete.get("displayName") or "Unknown",
                "position": (athlete.get("position") or {}).get("abbreviation"),
                "designation": designation, "status": item.get("status"),
                "injury_type": details.get("type"), "location": details.get("location"),
                "detail": details.get("detail"), "side": details.get("side"),
                "practice_status": practice, "return_date": details.get("returnDate"),
                "short_comment": item.get("shortComment") or latest_note.get("headline"),
                "long_comment": item.get("longComment") or latest_note.get("text"),
                "report_date": item.get("date") or latest_note.get("date"),
                "note_source": latest_note.get("source"), "fetched_at": fetched_at,
                "source_url": INJURIES_URL,
            })
    return rows


def staff_rows(path: str | Path = STAFF_PATH) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return [{**row, "season": int(row["season"]), "team": canon_team(row["team"])}
                for row in csv.DictReader(handle)]


def scheme_rate_rows(path: str | Path = SCHEME_RATE_PATH) -> list[dict[str, Any]]:
    """Manually-curated blitz/box/sub-package tendency rates, one snapshot per
    team with no season column of its own -- the caller stamps whichever
    season it is currently syncing, same as the ESPN injury feed."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            team = canon_team(row.get("Team"))
            if not team:
                continue
            rows.append({
                "team": team,
                "blitz_rate": row.get("Blitz Rate"),
                "light_box_rate": row.get("Light Box Rate"),
                "heavy_box_rate": row.get("Heavy Box Rate"),
                "sub_package_rate": row.get("Sub Package Rate"),
            })
        return rows


def sync_espn_context(repository: NFLRepository, cache_path: str | os.PathLike[str],
                      season: int, *, force: bool = False,
                      client: ESPNNFLClient | None = None) -> dict[str, int]:
    client = client or ESPNNFLClient(cache_path)
    staff = [row for row in staff_rows() if row["season"] == season]
    staff_count = repository.replace_team_staff(season, staff)
    scheme_rate_count = repository.replace_team_scheme_rates(season, scheme_rate_rows())
    pressure_rate_count = repository.replace_team_pressure_rates(
        season, pressure_rate_rows(repository),
    )
    injuries = injury_rows(client.load_injuries(force=force), season)
    injury_count = repository.replace_injuries(season, injuries)
    return {"staff": staff_count, "scheme_rates": scheme_rate_count,
            "pressure_rates": pressure_rate_count, "injuries": injury_count}
