"""Import an external consensus draft board and reconcile it with our own data.

A consensus board is a *source*, not a fact: it records what evaluators currently
think, and it is stored with its provenance and its own rank intact. It is never
merged into a single blended number, because the interesting question is where
the board and the on-field profile disagree.

Reconciling it against the PFF-calibrated board in `draft.py` produces three
useful groups:

* **Consensus and production agree** — ranked highly and grades out.
* **Board is higher than the profile** — the case rests on traits or projection
  this system cannot see, which is worth knowing before repeating the ranking.
* **Profile is higher than the board** — a returner whose production matches
  drafted players but who is not on the board yet, which is exactly the
  "before they are obvious" case the application exists to surface.

Identity follows the same conservative rules as the PFF importer: an exact
normalized name on the same school is a confirmed link, a unique name at another
school is only a candidate, and anything ambiguous stays unresolved.
"""

from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sports_aggregator.cfb.models import normalize_alias
from sports_aggregator.cfb.repository import CFBRepository, _logo_pair, schema_once


PROSPECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS draft_prospect_rankings (
 draft_year INTEGER NOT NULL, source TEXT NOT NULL, rank INTEGER NOT NULL,
 player_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 school TEXT NOT NULL DEFAULT '', position TEXT NOT NULL DEFAULT '',
 cfbd_player_id TEXT, cfbd_team_id INTEGER, link_status TEXT NOT NULL,
 link_evidence TEXT NOT NULL DEFAULT '', source_file TEXT NOT NULL,
 imported_at TEXT NOT NULL,
 PRIMARY KEY(draft_year,source,normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_prospect_rank ON draft_prospect_rankings(draft_year,rank);
"""

#: Board position codes mapped to the CFBD draft vocabulary used elsewhere.
BOARD_POSITIONS = {
    "QB": "Quarterback", "RB": "Running Back", "HB": "Running Back",
    "WR": "Wide Receiver", "TE": "Tight End",
    "OT": "Offensive Tackle", "T": "Offensive Tackle", "IOL": "Offensive Guard",
    "OG": "Offensive Guard", "G": "Offensive Guard", "C": "Center", "OL": "Offensive Tackle",
    "EDGE": "Defensive Edge", "ED": "Defensive Edge", "DE": "Defensive Edge",
    "DT": "Defensive Tackle", "DI": "Defensive Tackle", "DL": "Defensive Tackle",
    "LB": "Linebacker", "CB": "Cornerback", "S": "Safety", "DB": "Safety",
    "K": "Place Kicker", "P": "Punter",
}


#: Names draft boards use that no CFBD alias covers. Kept as an explicit,
#: reviewable mapping rather than added to `team_aliases`, because "Mississippi"
#: is genuinely ambiguous with Mississippi State outside this context.
BOARD_SCHOOL_ALIASES = {
    "mississippi": "ole miss",
    "southern mississippi": "southern miss",
    "pitt": "pittsburgh",
    "nc state": "north carolina state",
}

#: Generational suffixes that boards and rosters disagree about constantly.
NAME_SUFFIXES = ("jr", "sr", "ii", "iii", "iv", "v")


def strip_suffix(normalized: str) -> str:
    """Drop a trailing generational suffix from an already-normalized name."""
    parts = normalized.split()
    while len(parts) > 2 and parts[-1] in NAME_SUFFIXES:
        parts.pop()
    return " ".join(parts)


def board_position(code: str | None) -> str:
    value = (code or "").strip().upper()
    return BOARD_POSITIONS.get(value, value.title() or "Unknown")


@schema_once("prospects")
def initialize(repository: CFBRepository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript(PROSPECT_SCHEMA)


def read_board(path: str | Path) -> list[dict[str, Any]]:
    """Read a ranked board CSV with Rank, Player, School, Position columns.

    `College` is accepted in place of `School` -- boards from different sites
    use either header for the same column, and neither ever supplies both.
    """
    entries = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("Player") or "").strip()
            if not name:
                continue
            try:
                rank = int(str(row.get("Rank") or "").strip())
            except ValueError:
                continue
            entries.append({
                "rank": rank,
                "player_name": name,
                "normalized_name": normalize_alias(name),
                "school": (row.get("School") or row.get("College") or "").strip(),
                "position": (row.get("Position") or "").strip(),
            })
    return entries


def import_board(repository: CFBRepository, path: str | Path, *, draft_year: int,
                 source: str, roster_season: int) -> dict[str, Any]:
    """Store a board and resolve each prospect against the current roster."""
    initialize(repository)
    entries = read_board(path)
    now = datetime.now(timezone.utc).isoformat()
    filename = Path(path).name
    counts = {"rows": len(entries), "confirmed": 0, "school_mismatch": 0,
              "unresolved": 0, "unknown_school": 0}
    with closing(repository._connect()) as connection:
        roster = connection.execute(
            """SELECT p.player_id,p.first_name,p.last_name,p.team,t.team_id
               FROM players p LEFT JOIN teams t ON t.school=p.team
               WHERE p.season=?""", (roster_season,)).fetchall()
        by_name: dict[str, list[dict[str, Any]]] = {}
        for row in roster:
            key = normalize_alias(f"{row['first_name']} {row['last_name']}")
            by_name.setdefault(key, []).append(dict(row))
        aliases: dict[str, int] = {}
        for row in connection.execute("SELECT team_id,normalized_alias FROM team_aliases"):
            aliases.setdefault(row["normalized_alias"], row["team_id"])
        # A second index keyed on the suffix-stripped name, so "Terrance Carter"
        # on a board can reach "Terrance Carter Jr." on the roster.
        by_stripped: dict[str, list[dict[str, Any]]] = {}
        for key, rows in by_name.items():
            by_stripped.setdefault(strip_suffix(key), []).extend(rows)

        for entry in entries:
            school_key = normalize_alias(entry["school"])
            school_key = BOARD_SCHOOL_ALIASES.get(school_key, school_key)
            board_team = aliases.get(school_key)
            matches = by_name.get(entry["normalized_name"], [])
            suffix_match = False
            if not matches:
                matches = by_stripped.get(strip_suffix(entry["normalized_name"]), [])
                suffix_match = bool(matches)
            same_school = [row for row in matches if row["team_id"] == board_team]
            if len(same_school) == 1:
                link, status = same_school[0], "CONFIRMED"
                evidence = (f"name matched on {link['team']} ignoring generational suffix"
                            if suffix_match else f"exact name on {link['team']}")
                counts["confirmed"] += 1
            elif len(matches) == 1 and board_team is not None:
                # The board and the roster disagree on school. That is a real
                # signal (a transfer, or a bad row) and must not be silently
                # resolved into a confirmed identity.
                link, status = matches[0], "SCHOOL_MISMATCH"
                evidence = f"name unique but roster lists {matches[0]['team']}"
                counts["school_mismatch"] += 1
            else:
                link, status = None, "UNRESOLVED"
                if board_team is None:
                    evidence = f"board school '{entry['school']}' did not resolve to an FBS team"
                    counts["unknown_school"] += 1
                elif not matches:
                    evidence = "no roster match"
                else:
                    evidence = f"{len(matches)} roster players share this name"
                counts["unresolved"] += 1
            connection.execute(
                """INSERT INTO draft_prospect_rankings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(draft_year,source,normalized_name) DO UPDATE SET
                   rank=excluded.rank,school=excluded.school,position=excluded.position,
                   cfbd_player_id=excluded.cfbd_player_id,cfbd_team_id=excluded.cfbd_team_id,
                   link_status=excluded.link_status,link_evidence=excluded.link_evidence,
                   source_file=excluded.source_file,imported_at=excluded.imported_at""",
                (draft_year, source, entry["rank"], entry["player_name"],
                 entry["normalized_name"], entry["school"], entry["position"],
                 link["player_id"] if link else None, board_team,
                 status, evidence, filename, now))
        connection.commit()
    return counts


def consensus_board(repository: CFBRepository, *, draft_year: int = 2027,
                    source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """The stored consensus board with its link status intact."""
    initialize(repository)
    params: list[Any] = [draft_year]
    clause = ""
    if source:
        clause = " AND source=?"
        params.append(source)
    params.append(limit)
    with repository._reader() as connection:
        rows = connection.execute(
            f"""SELECT r.*,t.school team_school,t.color,t.logos_json
                FROM draft_prospect_rankings r
                LEFT JOIN teams t ON t.team_id=r.cfbd_team_id
                WHERE r.draft_year=?{clause} ORDER BY r.rank LIMIT ?""", params).fetchall()
    import json
    board = []
    for row in rows:
        item = dict(row)
        logos = json.loads(item.pop("logos_json") or "[]")
        item["logo"], item["logo_dark"] = _logo_pair(logos)
        item["draft_position"] = board_position(item["position"])
        board.append(item)
    return board


#: Short verdicts describing how the board and the profile compare.
VERDICTS = {
    "AGREE": "Board & data agree",
    "BOARD_AHEAD": "Board ahead of data",
    "NO_PROFILE": "No prior-season sample",
    "UNLINKED": "Not linked to a roster",
}


def board_with_profile(repository: CFBRepository, profile_board: dict[str, Any], *,
                       draft_year: int = 2027, limit: int = 100) -> list[dict[str, Any]]:
    """The consensus board in its own order, each row carrying our profile.

    This is the board a reader wants: the ranking they recognise, annotated with
    what our data says about each name, rather than a competing list.
    """
    consensus = consensus_board(repository, draft_year=draft_year, limit=limit)
    by_id = {prospect["cfbd_player_id"]: prospect
             for prospect in profile_board.get("prospects") or []
             if prospect.get("cfbd_player_id")}
    by_name = {normalize_alias(prospect["player_name"]): prospect
               for prospect in profile_board.get("prospects") or []}
    rows = []
    for entry in consensus:
        profile = by_id.get(entry.get("cfbd_player_id")) or by_name.get(entry["normalized_name"])
        if entry["link_status"] == "UNRESOLVED":
            verdict = VERDICTS["UNLINKED"]
        elif profile is None:
            verdict = VERDICTS["NO_PROFILE"]
        elif profile["percentile"] >= 0.75:
            verdict = VERDICTS["AGREE"]
        else:
            verdict = VERDICTS["BOARD_AHEAD"]
        rows.append({
            **entry,
            "position": entry["draft_position"],
            "profile_percentile": profile["percentile"] if profile else None,
            "interest_score": profile["interest_score"] if profile else None,
            "verdict": verdict,
        })
    return rows


def reconcile(repository: CFBRepository, profile_board: dict[str, Any], *,
              draft_year: int = 2027, source: str | None = None) -> dict[str, Any]:
    """Compare the consensus board against the PFF-calibrated profile board.

    Returns the two disagreement groups plus the agreement set, each carrying the
    numbers behind the comparison so a reader can judge the claim.
    """
    consensus = consensus_board(repository, draft_year=draft_year, source=source, limit=200)
    profiles = {}
    for prospect in profile_board.get("prospects") or []:
        if prospect.get("cfbd_player_id"):
            profiles[prospect["cfbd_player_id"]] = prospect
    by_name = {normalize_alias(prospect["player_name"]): prospect
               for prospect in profile_board.get("prospects") or []}

    agree, board_high, no_profile, unranked = [], [], [], []
    ranked_ids: set[str] = set()
    total_ranked = len(consensus) or 1
    for entry in consensus:
        profile = (profiles.get(entry.get("cfbd_player_id"))
                   or by_name.get(entry["normalized_name"]))
        if profile and profile.get("cfbd_player_id"):
            ranked_ids.add(profile["cfbd_player_id"])
        board_percentile = 1 - ((entry["rank"] - 1) / total_ranked)
        item = {
            "rank": entry["rank"],
            "player_name": entry["player_name"],
            "school": entry["school"],
            "position": entry["draft_position"],
            "cfbd_player_id": entry.get("cfbd_player_id"),
            "link_status": entry["link_status"],
            "link_evidence": entry["link_evidence"],
            "logo": entry.get("logo"),
            "logo_dark": entry.get("logo_dark"),
            "color": entry.get("color"),
            "profile_percentile": profile["percentile"] if profile else None,
            "interest_score": profile["interest_score"] if profile else None,
            "board_percentile": round(board_percentile, 3),
        }
        if profile is None:
            # Usually a freshman with no qualifying prior-season sample. That is
            # missing evidence, not disagreement, and is reported separately.
            item["note"] = "no linked prior-season profile to compare against"
            no_profile.append(item)
        elif profile["percentile"] >= 0.75:
            item["note"] = "board rank and production profile agree"
            agree.append(item)
        else:
            item["note"] = ("ranked well above the production profile; "
                            "the case rests on traits this system cannot see")
            board_high.append(item)

    for prospect in profile_board.get("prospects") or []:
        if prospect.get("cfbd_player_id") in ranked_ids:
            continue
        if normalize_alias(prospect["player_name"]) in {
                entry["normalized_name"] for entry in consensus}:
            continue
        if prospect["percentile"] >= 0.9:
            unranked.append({
                **prospect,
                "note": "matches drafted profiles but is absent from the consensus board",
            })

    return {
        "draft_year": draft_year,
        "consensus_size": len(consensus),
        "agree": agree,
        "board_ahead": board_high,
        "no_profile": no_profile,
        "profile_ahead": unranked[:40],
    }
