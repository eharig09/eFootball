"""Import the curated NFL Bluesky workbook into league-scoped source metadata."""

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
import zipfile

from sports_aggregator.social.models import LeagueSourceProfile, SourceProfile


DEFAULT_PATH = Path("data/nfl/NFL_Bluesky_Directory.xlsx")
NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

REQUIRED_COLUMNS = {
    "Name", "Handle", "Scope", "Conference", "Division", "Team",
    "Account Type", "Coverage Tags", "Specialty Tags", "Priority",
    "Recommended Lists", "Notes", "Verification",
}

LIST_SECTIONS = {
    "NFL Wire": "wire",
    "NFL Film + Analytics": "analysis",
    "NFL Personnel": "personnel",
    "NFL Players + Usage": "players",
    "AFC Beats": "beats",
    "NFC Beats": "beats",
}

TAG_SECTIONS = {
    "reporting": "wire", "breaking news": "wire",
    "analysis": "analysis", "film/analytics": "analysis",
    "draft": "personnel", "personnel": "personnel",
    "position specialist": "players", "team coverage": "beats",
}

TYPE_MAP = {
    "reporter": "REPORTER", "analyst": "ANALYST",
    "editor/analyst": "ANALYST", "outlet": "OUTLET",
    "official/team": "OFFICIAL_TEAM", "community": "COMMUNITY_ANALYSIS",
}


class NFLSourceDirectoryError(ValueError):
    pass


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        raise NFLSourceDirectoryError(f"invalid cell reference {reference!r}")
    value = 0
    for character in letters.group():
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _split(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in value.split(";") if part.strip()))


def _sheet_rows(path: str | Path) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise NFLSourceDirectoryError(f"cannot read NFL source directory: {exc}") from exc
    with archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", NS):
        cells: dict[int, str] = {}
        for cell in row.findall("x:c", NS):
            value = cell.find("x:v", NS)
            cells[_column_index(cell.attrib["r"])] = (
                value.text if value is not None and value.text is not None else ""
            )
        width = max(cells, default=-1) + 1
        rows.append([cells.get(index, "") for index in range(width)])
    return rows


def _source_type(account_type: str, scope: str) -> str:
    if account_type.casefold() == "reporter" and scope.casefold() == "team":
        return "BEAT_REPORTER"
    if account_type.casefold() == "outlet" and scope.casefold() == "team":
        return "TEAM_OUTLET"
    return TYPE_MAP.get(account_type.casefold(), "SPECIALIST")


def _profile(row: dict[str, str]) -> LeagueSourceProfile:
    handle = row["Handle"].strip().removeprefix("@").casefold()
    if not handle or "." not in handle or "/" in handle:
        raise NFLSourceDirectoryError(f"invalid Bluesky handle {row['Handle']!r}")
    profile_url = row.get("Profile URL", "").strip()
    if profile_url:
        parsed = urlparse(profile_url)
        if parsed.scheme != "https" or parsed.netloc.casefold() != "bsky.app":
            raise NFLSourceDirectoryError(f"invalid Bluesky profile URL for {handle}")
    try:
        directory_priority = int(float(row["Priority"]))
    except ValueError as exc:
        raise NFLSourceDirectoryError(f"invalid priority for {handle}") from exc
    if directory_priority not in {1, 2, 3}:
        raise NFLSourceDirectoryError(f"priority must be 1, 2, or 3 for {handle}")

    coverage = _split(row["Coverage Tags"])
    specialty = _split(row["Specialty Tags"])
    lists = _split(row["Recommended Lists"])
    tags: list[tuple[str, str, str]] = []
    for tag in coverage:
        tags.append((tag, "coverage", TAG_SECTIONS.get(tag.casefold(), "overview")))
    for tag in specialty:
        tags.append((tag, "specialty", TAG_SECTIONS.get(tag.casefold(), "overview")))
    for tag in lists:
        tags.append((tag, "recommended_list", LIST_SECTIONS.get(tag, "overview")))
    # Exact duplicate cells are harmless, but the scoped-tag primary key is not.
    tags = list(dict.fromkeys(tags))

    account_type = row["Account Type"].strip()
    source_type = _source_type(account_type, row["Scope"])
    reporting = 5 if "Reporting" in coverage and directory_priority == 1 else (4 if "Reporting" in coverage else 2)
    analysis = 5 if "Film/Analytics" in coverage and directory_priority == 1 else (4 if "Analysis" in coverage else 2)
    source = SourceProfile(
        handle=handle, display_name=row["Name"].strip(), organization=None,
        source_type=source_type,
        specialties=("nfl",) + tuple(re.sub(r"[^a-z0-9]+", "_", tag.casefold()).strip("_")
                                     for tag in coverage + specialty + lists),
        reliability=5 if source_type == "OFFICIAL_TEAM" else (2 if source_type == "COMMUNITY_ANALYSIS" else 4),
        original_reporting_score=reporting, analysis_score=analysis,
        breaking_news_score=5 if "Breaking News" in coverage else 2,
        prospect_score=4 if "Draft" in coverage else 2, g5_score=1,
        priority={1: 5, 2: 4, 3: 3}[directory_priority],
    )
    return LeagueSourceProfile(
        source=source, league="nfl", scope=row["Scope"].strip(),
        conference=row["Conference"].strip() or None,
        division=row["Division"].strip() or None, team=row["Team"].strip() or None,
        account_type=account_type, directory_priority=directory_priority,
        profile_url=profile_url or f"https://bsky.app/profile/{handle}",
        notes=row["Notes"].strip(), verification=row["Verification"].strip(),
        tags=tuple(tags),
    )


def load_directory(path: str | Path = DEFAULT_PATH) -> tuple[LeagueSourceProfile, ...]:
    rows = _sheet_rows(path)
    header_index = next((index for index, row in enumerate(rows) if "Handle" in row), None)
    if header_index is None:
        raise NFLSourceDirectoryError("Directory sheet has no Handle header")
    headers = rows[header_index]
    missing = REQUIRED_COLUMNS - set(headers)
    if missing:
        raise NFLSourceDirectoryError(f"Directory sheet missing columns {sorted(missing)}")
    profiles: list[LeagueSourceProfile] = []
    seen: set[str] = set()
    for values in rows[header_index + 1:]:
        row = {header: values[index] if index < len(values) else ""
               for index, header in enumerate(headers) if header}
        if not row.get("Handle", "").strip():
            continue
        profile = _profile(row)
        if profile.source.handle in seen:
            raise NFLSourceDirectoryError(f"duplicate handle {profile.source.handle}")
        seen.add(profile.source.handle)
        profiles.append(profile)
    return tuple(profiles)


def import_directory(registry, path: str | Path = DEFAULT_PATH) -> int:
    return registry.seed_league(load_directory(path))
