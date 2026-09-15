"""Team-level pass-rush/blitz tendency data from Pro-Football-Reference exports.

PFR's "share" downloads save as an HTML table with an .xls extension, not a
real workbook -- pandas' HTML reader handles that natively. The export has
no season/date of its own, so whichever file is newest in the drop folder is
treated as the current snapshot; dropping a fresher download in and re-running
the sync is the whole refresh workflow (no filename to edit, no code change).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sports_aggregator.nfl.models import optional_float
from sports_aggregator.nfl.naming import TEAM_NICKNAMES
from sports_aggregator.nfl.repository import NFLRepository


DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "nfl"
FILENAME_GLOB = "sportsref_download*.xls"

# Tm -> data-stat column name, mirrored onto our own snake_case columns.
COLUMNS = (
    ("games", "G"), ("dadot", "DADOT"), ("air_yards", "Air"), ("yards_after_catch", "YAC"),
    ("blitzes", "Bltz"), ("blitz_rate", "Bltz%"), ("hurries", "Hrry"), ("hurry_rate", "Hrry%"),
    ("qb_knockdowns", "QBKD"), ("knockdown_rate", "QBKD%"), ("sacks", "Sk"),
    ("pressures", "Prss"), ("pressure_rate", "Prss%"), ("missed_tackles", "MTkl"),
)


def _percent(value: Any) -> float | None:
    return optional_float(str(value).rstrip("%") if value is not None else value)


def find_latest_export(directory: str | Path = DEFAULT_DIR) -> Path | None:
    candidates = sorted(Path(directory).glob(FILENAME_GLOB), key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def pressure_rate_rows(repository: NFLRepository,
                       path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read the newest PFR pass-rush export and resolve full team names to
    our canonical abbreviations via the teams table (falling back to the
    static nickname map for a name the table doesn't carry yet)."""
    source = Path(path) if path else find_latest_export()
    if source is None or not source.exists():
        return []
    import pandas as pd
    frame = pd.read_html(source)[0]
    by_name = {team["name"].strip().upper(): team["abbreviation"]
              for team in repository.list_teams()}
    rows = []
    for record in frame.to_dict("records"):
        raw_name = str(record.get("Tm") or "").strip()
        team = by_name.get(raw_name.upper())
        if not team:
            nickname = raw_name.split()[-1].upper() if raw_name else ""
            team = TEAM_NICKNAMES.get(nickname)
        if not team:
            continue
        row: dict[str, Any] = {"team": team}
        for key, source_column in COLUMNS:
            value = record.get(source_column)
            row[key] = _percent(value) if key.endswith("_rate") else optional_float(value)
        rows.append(row)
    return rows
