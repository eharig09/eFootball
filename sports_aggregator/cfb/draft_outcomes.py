"""How drafted players at each position have actually turned out, 2010-2025.

`draft.py`'s calibration compares a returner's production profile against the
players who were drafted a year earlier -- but that calibration set is a
single class, because prior-season PFF data only goes back one year. It can
say a profile looks like a drafted player's; it cannot say what drafted
players at that position usually become.

This module imports a standalone, name-and-college-only historical draft
export (round, pick, and career result -- Pro Bowls, seasons started,
career approximate value) and summarizes it by position and draft day. It
needs no identity link to this app's own roster or PFF data, because the
export already carries its own college and position for every pick; the
question it answers is a base rate ("how often has a Day 2 cornerback
started"), not a claim about any specific current prospect.
"""

from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sports_aggregator.cfb.models import normalize_alias
from sports_aggregator.cfb.repository import CFBRepository, schema_once


OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_draft_outcomes (
 draft_year INTEGER NOT NULL, round INTEGER NOT NULL, pick INTEGER NOT NULL,
 nfl_team TEXT NOT NULL, player_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 position TEXT NOT NULL, draft_position TEXT NOT NULL, college TEXT NOT NULL DEFAULT '',
 draft_age INTEGER, last_season INTEGER,
 all_pro_seasons INTEGER NOT NULL DEFAULT 0, pro_bowls INTEGER NOT NULL DEFAULT 0,
 seasons_started INTEGER NOT NULL DEFAULT 0, career_av INTEGER, team_av INTEGER,
 games INTEGER NOT NULL DEFAULT 0, source_file TEXT NOT NULL, imported_at TEXT NOT NULL,
 PRIMARY KEY(draft_year,pick)
);
CREATE INDEX IF NOT EXISTS idx_historical_outcomes_position
    ON historical_draft_outcomes(draft_position,round);
"""

#: Draft-era position codes mapped to this app's draft vocabulary (see
#: `draft.py`'s `PFF_TO_DRAFT_POSITION` and `prospects.py`'s `BOARD_POSITIONS`).
#: A code the source uses generically across eras -- `OL`, `DL`, `DB` -- is kept
#: as its own explicit "(unspecified)" bucket rather than guessed into a
#: specific position; that mirrors the conservative identity rule the rest of
#: this app's draft code uses (an ambiguous row is reported as ambiguous, not
#: silently resolved).
POSITION_MAP = {
    "QB": "Quarterback",
    "RB": "Running Back", "HB": "Running Back", "FB": "Running Back",
    "WR": "Wide Receiver",
    "TE": "Tight End",
    "T": "Offensive Tackle", "OT": "Offensive Tackle",
    "G": "Offensive Guard",
    "C": "Center",
    "OL": "Offensive Line (unspecified)",
    "DE": "Defensive Edge",
    "DT": "Defensive Tackle", "NT": "Defensive Tackle",
    "DL": "Defensive Line (unspecified)",
    "LB": "Linebacker", "OLB": "Linebacker", "ILB": "Linebacker",
    "CB": "Cornerback",
    "S": "Safety", "SAF": "Safety", "FS": "Safety",
    "DB": "Defensive Back (unspecified)",
    "K": "Place Kicker",
    "P": "Punter",
    "LS": "Long Snapper",
}

#: Draft-day groupings, coarser than individual rounds so a position/round
#: bucket has enough of the 2010-2025 sample behind it to mean something.
ROUND_BUCKETS = ((1, 1, "Round 1"), (2, 3, "Rounds 2-3"), (4, 7, "Rounds 4-7"))


def draft_position_bucket(code: str | None) -> str:
    value = (code or "").strip().upper()
    return POSITION_MAP.get(value, value.title() or "Unknown")


def _round_bucket(round_number: int) -> str:
    for low, high, label in ROUND_BUCKETS:
        if low <= round_number <= high:
            return label
    return ROUND_BUCKETS[-1][2]


@schema_once("draft_outcomes")
def initialize(repository: CFBRepository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript(OUTCOME_SCHEMA)


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def read_outcomes(path: str | Path) -> list[dict[str, Any]]:
    """Read the fixed-layout historical export.

    Only identity and career-outcome columns are kept; the passing/rushing/
    receiving/defensive box-score columns are not read at all. `csv.DictReader`
    is skipped anyway and columns are read by position, because the header
    repeats `Att`, `Yds`, `TD`, and `Int` across three different stat blocks --
    a dict silently keeps only the last of each -- and carries an unlabeled
    link column between `College/Univ` and `Year`. Reading by position sidesteps
    both instead of trusting exact header text to keep matching future exports.
    The layout this export has always shipped with:

    Rnd,Pick,Tm,Player,Pos,Age,To,AP1,PB,St,wAV,DrAV,G,
    Cmp,Att,Yds,TD,Int,        (passing)
    Att,Yds,TD,                (rushing)
    Rec,Yds,TD,                (receiving)
    Solo,Int,Sk,               (defense)
    College/Univ,<link label>,Year
    """
    entries: list[dict[str, Any]] = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)  # header
        for row in reader:
            if len(row) < 30 or not (row[0] or "").strip() or not (row[3] or "").strip():
                continue
            try:
                round_number = int(row[0])
                pick = int(row[1])
                draft_year = int(row[29])
            except ValueError:
                continue
            name = row[3].strip()
            position = (row[4] or "").strip().upper()
            entries.append({
                "draft_year": draft_year, "round": round_number, "pick": pick,
                "nfl_team": (row[2] or "").strip(),
                "player_name": name, "normalized_name": normalize_alias(name),
                "position": position, "draft_position": draft_position_bucket(position),
                "college": (row[27] or "").strip(),
                "draft_age": _int_or_none(row[5]),
                "last_season": _int_or_none(row[6]),
                "all_pro_seasons": _int_or_none(row[7]) or 0,
                "pro_bowls": _int_or_none(row[8]) or 0,
                "seasons_started": _int_or_none(row[9]) or 0,
                "career_av": _int_or_none(row[10]),
                "team_av": _int_or_none(row[11]),
                "games": _int_or_none(row[12]) or 0,
            })
    return entries


def import_outcomes(repository: CFBRepository, path: str | Path) -> dict[str, Any]:
    """Replace stored outcomes for every draft year present in the file.

    Years are replaced wholesale rather than upserted row by row: the source is
    a point-in-time export, not an incremental feed, so a re-import always
    means "this file is now the record for these years."
    """
    initialize(repository)
    entries = read_outcomes(path)
    now = datetime.now(timezone.utc).isoformat()
    filename = Path(path).name
    years = sorted({entry["draft_year"] for entry in entries})
    with closing(repository._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if years:
            placeholders = ",".join("?" for _ in years)
            connection.execute(
                f"DELETE FROM historical_draft_outcomes WHERE draft_year IN ({placeholders})",
                years)
        connection.executemany(
            """INSERT INTO historical_draft_outcomes VALUES(
                 :draft_year,:round,:pick,:nfl_team,:player_name,:normalized_name,
                 :position,:draft_position,:college,:draft_age,:last_season,
                 :all_pro_seasons,:pro_bowls,:seasons_started,:career_av,:team_av,
                 :games,:source_file,:imported_at)""",
            [{**entry, "source_file": filename, "imported_at": now} for entry in entries],
        )
        connection.commit()
    return {"rows": len(entries), "draft_years": years}


def outcome_summary(repository: CFBRepository, *, min_sample: int = 5) -> list[dict[str, Any]]:
    """Realized outcomes grouped by position and draft-day window.

    "Started" means at least one season as a primary starter -- the source's
    own `St` column. This is a realized base rate for players already
    drafted, never a projection for a specific current prospect; `n` is
    included on every row so a thin bucket (a punter taken in round one) is
    visible rather than hidden.
    """
    initialize(repository)
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT draft_position, round, seasons_started, pro_bowls, career_av
               FROM historical_draft_outcomes""")]
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["draft_position"], _round_bucket(row["round"]))
        buckets.setdefault(key, []).append(row)
    summary = []
    for (position, bucket), entries in buckets.items():
        n = len(entries)
        starters = sum(1 for entry in entries if (entry["seasons_started"] or 0) >= 1)
        pro_bowlers = sum(1 for entry in entries if (entry["pro_bowls"] or 0) >= 1)
        career_avs = sorted(entry["career_av"] for entry in entries
                            if entry["career_av"] is not None)
        summary.append({
            "position": position, "round_bucket": bucket, "n": n,
            "starter_rate": round(starters / n, 3) if n else None,
            "pro_bowl_rate": round(pro_bowlers / n, 3) if n else None,
            "median_career_av": career_avs[len(career_avs) // 2] if career_avs else None,
            "reliable": n >= min_sample,
        })
    order = {label: index for index, (_, _, label) in enumerate(ROUND_BUCKETS)}
    summary.sort(key=lambda item: (item["position"], order.get(item["round_bucket"], 9)))
    return summary
