"""Admin page for the two sources that arrive as CSV files: PFF and CFBDepth.

Both are downloaded by hand and imported by hand, and neither could tell you
what was already loaded. PFF had a command line only; CFBDepth had a page with
three file inputs and nothing else, so the question a person actually has while
standing in front of it -- is this batch newer than what is in there? -- could
only be answered by running a separate `status` command.

So this pairs each upload control with what that source currently holds: which
datasets are present, how many rows, and when each was last imported. The
import paths themselves are the existing ones, unchanged. Nothing here writes
to a table the established importers do not already own.
"""

from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
from typing import Any

from flask import (
    Blueprint, current_app, redirect, render_template, request, session, url_for,
)

from sports_aggregator.cfb import cfbdepth_data
from sports_aggregator.cfb.data_status import display_time, relative_time
from sports_aggregator.cfb.cfbdepth_flexible import canonicalize_cfbdepth_upload
from sports_aggregator.cfb.pff_flexible import (
    PRIMARY_SIGNATURES,
    SUPPLEMENTAL_SIGNATURES,
    import_pff_directory_flexible,
    preflight_pff_directory,
)
from sports_aggregator.cfb.prospects import import_board, initialize as initialize_board
from sports_aggregator.page_cache import cache


data_import_pages = Blueprint("cfb_data_import", __name__)

#: A PFF batch is a dozen CSVs of a few hundred KB each. Generous enough for a
#: season export, small enough that a mistaken upload fails fast rather than
#: after a long transfer.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: The three CFBDepth exports, in the order the page presents them.
CFBDEPTH_KINDS = (
    ("roster", "Roster Breakdown", "cfbdepth_roster_breakdown"),
    ("impact", "Team Impact Report", "cfbdepth_team_impact"),
    ("updates", "Player Updates", "cfbdepth_player_updates"),
)


def _repository():
    return current_app.extensions["cfb_repository"]


def _configured_season() -> int:
    return int(current_app.config.get("CFB_DEFAULT_SEASON")
               or datetime.now(timezone.utc).year)


def pff_seasons(repository) -> list[int]:
    """Seasons the PFF snapshot actually holds, newest first."""
    repository.initialize()
    with closing(sqlite3.connect(repository.path)) as connection:
        if not _table_exists(connection, "pff_player_metrics"):
            return []
        return [int(row[0]) for row in connection.execute(
            "SELECT DISTINCT season FROM pff_player_metrics "
            "WHERE season IS NOT NULL ORDER BY season DESC")]


def _default_season(repository) -> int:
    """The season the page opens on: the newest one that has PFF data.

    Not the current calendar year, which is what the configured default gives
    and which is reliably wrong here: a PFF season is the one just played,
    graded against next year's roster, so for much of the year the season on
    the clock has no snapshot at all and the page would report an empty source
    as though the data had gone missing.
    """
    seasons = pff_seasons(repository)
    return seasons[0] if seasons else _configured_season()


def _authorized() -> bool:
    """The same gate the CFBDepth import has always used.

    A constant-time comparison against either configured secret, and a session
    flag for an operator who has already proved it once this session.
    """
    if session.get("cfb_admin") is True:
        return True
    supplied = str(request.form.get("token") or "").strip()
    if not supplied:
        return False
    for key in ("CFB_ADMIN_PIN", "CFB_REFRESH_TOKEN"):
        expected = str(current_app.config.get(key) or "").strip()
        if expected and secrets.compare_digest(supplied, expected):
            return True
    return False


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def _relative(value: Any) -> str:
    """How long ago, in the data-status page's words.

    "never" rather than that page's "unknown" for an absent value: a source
    that has never been imported is a fact, not a gap in the record.
    """
    return relative_time(value) if value else "never"


def _dataset_rows(connection, table: str, season: int) -> dict[str, dict[str, Any]]:
    if not _table_exists(connection, table):
        return {}
    return {row["dataset"]: {"rows": row["rows"], "imported_at": row["imported_at"]}
            for row in connection.execute(
                f"""SELECT dataset, COUNT(*) rows, MAX(imported_at) imported_at
                    FROM {table} WHERE season=? GROUP BY dataset""", (season,))}


def _dataset_report(expected, found) -> list[dict[str, Any]]:
    return [{"dataset": name,
             "rows": (found.get(name) or {}).get("rows") or 0,
             "imported_at": (found.get(name) or {}).get("imported_at"),
             "relative": _relative((found.get(name) or {}).get("imported_at")),
             "present": name in found}
            for name in sorted(expected)]


def pff_state(repository, season: int) -> dict[str, Any]:
    """What the PFF snapshot holds for one season, dataset by dataset.

    Reported per dataset rather than as one total because a batch can import
    cleanly while still being short a file, and a total would hide that.
    """
    repository.initialize()
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.row_factory = sqlite3.Row
        primary = _dataset_rows(connection, "pff_player_metrics", season)
        supplemental = _dataset_rows(connection, "pff_supplemental_metrics", season)
        players = linked = 0
        if _table_exists(connection, "pff_players"):
            row = connection.execute(
                "SELECT COUNT(*) players, SUM(cfbd_player_id IS NOT NULL) linked "
                "FROM pff_players WHERE season=?", (season,)).fetchone()
            players, linked = row["players"] or 0, row["linked"] or 0

    primary_rows = _dataset_report(PRIMARY_SIGNATURES, primary)
    supplemental_rows = _dataset_report(SUPPLEMENTAL_SIGNATURES, supplemental)
    stamps = [entry["imported_at"] for entry in primary_rows + supplemental_rows
              if entry["imported_at"]]
    return {
        "season": season,
        "players": players,
        "linked": linked,
        "primary": primary_rows,
        "supplemental": supplemental_rows,
        "primary_present": sum(1 for entry in primary_rows if entry["present"]),
        "primary_expected": len(PRIMARY_SIGNATURES),
        "supplemental_present": sum(1 for entry in supplemental_rows if entry["present"]),
        "supplemental_expected": len(SUPPLEMENTAL_SIGNATURES),
        "rows": sum(entry["rows"] for entry in primary_rows + supplemental_rows),
        "last_import": display_time(max(stamps)) if stamps else None,
        "relative": _relative(max(stamps)) if stamps else "never",
        "loaded": bool(stamps),
    }


def cfbdepth_state(repository) -> dict[str, Any]:
    """Row counts and last import for each of the three CFBDepth snapshots."""
    cfbdepth_data.initialize(repository)
    entries = []
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.row_factory = sqlite3.Row
        for key, label, table in CFBDEPTH_KINDS:
            if not _table_exists(connection, table):
                entries.append({"key": key, "label": label, "rows": 0,
                                "imported_at": None, "relative": "never"})
                continue
            row = connection.execute(
                f"SELECT COUNT(*) rows, MAX(imported_at) imported_at FROM {table}"
            ).fetchone()
            entries.append({"key": key, "label": label, "rows": row["rows"] or 0,
                            "imported_at": row["imported_at"],
                            "relative": _relative(row["imported_at"])})
    stamps = [entry["imported_at"] for entry in entries if entry["imported_at"]]
    return {"entries": entries,
            "last_import": display_time(max(stamps)) if stamps else None,
            "relative": _relative(max(stamps)) if stamps else "never",
            "loaded": bool(stamps),
            "rows": sum(entry["rows"] for entry in entries)}


def board_state(repository, draft_year: int) -> dict[str, Any]:
    """What the consensus big board holds for one draft year, source by source.

    Multiple sources can coexist for the same draft year (draft_prospect_rankings
    keys on draft_year+source+player), so this reports each one separately
    rather than a single blended total -- a stale "Consensus" import sitting
    next to a fresh "PFF Draft Guide" one should read as two different ages,
    not get averaged into one.
    """
    initialize_board(repository)
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.row_factory = sqlite3.Row
        if not _table_exists(connection, "draft_prospect_rankings"):
            return {"draft_year": draft_year, "sources": [], "rows": 0,
                    "last_import": None, "relative": "never", "loaded": False}
        rows = connection.execute(
            """SELECT source, COUNT(*) rows, MAX(imported_at) imported_at,
                      SUM(link_status='CONFIRMED') confirmed,
                      SUM(link_status='UNRESOLVED') unresolved,
                      SUM(link_status='SCHOOL_MISMATCH') school_mismatch
               FROM draft_prospect_rankings WHERE draft_year=? GROUP BY source
               ORDER BY imported_at DESC""",
            (draft_year,)).fetchall()
    sources = [{"source": row["source"], "rows": row["rows"],
               "confirmed": row["confirmed"] or 0, "unresolved": row["unresolved"] or 0,
               "school_mismatch": row["school_mismatch"] or 0,
               "imported_at": row["imported_at"], "relative": _relative(row["imported_at"])}
              for row in rows]
    stamps = [entry["imported_at"] for entry in sources if entry["imported_at"]]
    return {"draft_year": draft_year, "sources": sources,
            "rows": sum(entry["rows"] for entry in sources),
            "last_import": display_time(max(stamps)) if stamps else None,
            "relative": _relative(max(stamps)) if stamps else "never",
            "loaded": bool(stamps)}


def _result(kind: str, ok: bool, headline: str, detail: str = "",
            rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"kind": kind, "ok": ok, "headline": headline,
            "detail": detail, "rows": rows or []}


def _page(*, season: int | None = None, draft_year: int | None = None,
          result: dict[str, Any] | None = None):
    repository = _repository()
    chosen = season or _default_season(repository)
    seasons = pff_seasons(repository)
    # The board's own draft year is one ahead of the roster season it's
    # projecting into (roster_season = chosen + 1), matching prospect_board's
    # own default relationship between the two.
    chosen_draft_year = draft_year or (chosen + 2)
    return render_template(
        "cfb_data_import.html",
        pff=pff_state(repository, chosen),
        cfbdepth=cfbdepth_state(repository),
        board=board_state(repository, chosen_draft_year),
        season=chosen,
        roster_season=chosen + 1,
        draft_year=chosen_draft_year,
        # The configured season is offered even when it holds nothing, because
        # importing a season for the first time is exactly when it is needed.
        seasons=sorted({*seasons, chosen, _configured_season()}, reverse=True),
        draft_years=sorted({chosen_draft_year, chosen_draft_year - 1, chosen_draft_year + 1},
                           reverse=True),
        result=result,
    )


@data_import_pages.get("/college-football/data-import/")
def data_import():
    return _page(season=request.args.get("season", type=int),
                 draft_year=request.args.get("draft_year", type=int))


def _saved_uploads(files, destination: Path) -> tuple[int, int]:
    """Write the uploaded CSVs where the batch importer can read them.

    Returns how many were kept and how many were ignored for not being CSVs, so
    the page can say so rather than silently importing a subset.
    """
    kept = skipped = 0
    for storage in files:
        name = Path(storage.filename or "").name
        if not name:
            continue
        if not name.lower().endswith(".csv"):
            skipped += 1
            continue
        raw = storage.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"{name} is larger than the "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
        (destination / name).write_bytes(raw)
        kept += 1
    return kept, skipped


def _preflight_rows(check) -> list[dict[str, Any]]:
    rows = [{"dataset": dataset, "kind": "primary", "detail": Path(path).name}
            for dataset, path in sorted(check.primary.items())]
    rows += [{"dataset": dataset, "kind": "supplemental", "detail": Path(path).name}
             for dataset, path in sorted(check.supplemental.items())]
    return rows


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def _described(path: Path) -> str:
    """A filename with its row count, for the files preflight could not tell apart.

    The copies in a download folder are not copies. A real one holds
    `pass_rush_summary (1).csv` at 88 rows beside `pass_rush_summary.csv` at
    4,211: a filtered export sitting next to the full season. Names alone
    cannot distinguish those, and keeping the wrong one imports a fortieth of
    the data without anything looking wrong afterwards.
    """
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            rows = max(0, sum(1 for _ in csv.reader(source)) - 1)
    except (OSError, UnicodeError, csv.Error):
        return path.name
    return f"{path.name} ({rows:,} rows)"


@data_import_pages.post("/college-football/data-import/pff")
def import_pff():
    season = request.form.get("season", type=int) or _default_season(_repository())
    roster_season = request.form.get("roster_season", type=int) or season + 1
    if not _authorized():
        return _page(season=season, result=_result(
            "pff", False, "Authorization failed.",
            "Nothing was read and nothing was changed.")), 401

    validate_only = bool(request.form.get("validate_only"))
    files = [storage for storage in request.files.getlist("batch") if storage]
    if not files:
        return _page(season=season,
                     result=_result("pff", False, "No files selected.")), 400

    with TemporaryDirectory(prefix="cfb-pff-upload-") as temp:
        staging = Path(temp)
        try:
            kept, skipped = _saved_uploads(files, staging)
        except ValueError as exc:
            return _page(season=season,
                         result=_result("pff", False, str(exc))), 400
        if not kept:
            return _page(season=season, result=_result(
                "pff", False, "None of the selected files were CSVs.")), 400

        check = preflight_pff_directory(staging)
        rows = _preflight_rows(check)
        notes = []
        if skipped:
            notes.append(f"{_plural(skipped, 'non-CSV file')} ignored")
        if check.unclassified:
            notes.append("unrecognized: " + ", ".join(sorted(check.unclassified)))
        if check.missing_primary:
            notes.append("missing: " + ", ".join(check.missing_primary))
        for dataset, names in sorted(check.duplicates.items()):
            notes.append(f"{dataset} matched {len(names)} files: "
                         + ", ".join(_described(staging / name) for name in names))

        if not check.ready:
            # The established importer refuses a partial batch, and refuses it
            # before writing anything. Saying so here means an operator learns
            # it from the page rather than from a stack trace.
            return _page(season=season, result=_result(
                "pff", False,
                f"Preflight failed on {_plural(kept, 'file')} — nothing was "
                "imported and the current snapshot is unchanged.",
                "; ".join(notes), rows)), 400

        if validate_only:
            return _page(season=season, result=_result(
                "pff", True,
                f"Validated {_plural(kept, 'file')}. This batch would import cleanly.",
                "; ".join(notes) or "Every primary dataset is present.", rows))

        import_pff_directory_flexible(
            _repository(), staging, season=season, roster_season=roster_season)

    cache.clear()
    return _page(season=season, result=_result(
        "pff", True,
        f"Imported {_plural(kept, 'file')} into the {season} snapshot.",
        "; ".join(notes), rows))


@data_import_pages.post("/college-football/data-import/cfbdepth")
def import_cfbdepth():
    season = request.form.get("season", type=int) or _default_season(_repository())
    if not _authorized():
        return _page(season=season, result=_result(
            "cfbdepth", False, "Authorization failed.",
            "Nothing was read and nothing was changed.")), 401

    importers = {
        "roster": cfbdepth_data.import_roster_breakdown,
        "impact": cfbdepth_data.import_team_impact,
        "updates": cfbdepth_data.import_player_updates,
    }
    # Every file is canonicalized before any importer runs, so a batch carrying
    # one bad file leaves all three snapshots exactly as they were.
    prepared: dict[str, tuple[str, str]] = {}
    for key, label, _table in CFBDEPTH_KINDS:
        storage = request.files.get(key)
        if not storage or not storage.filename:
            continue
        raw = storage.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            return _page(season=season, result=_result(
                "cfbdepth", False,
                f"{label} is larger than the "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")), 400
        try:
            check, canonical = canonicalize_cfbdepth_upload(
                raw, expected_kind=key, label=storage.filename)
        except ValueError as exc:
            return _page(season=season, result=_result(
                "cfbdepth", False,
                f"Validation failed on {label} — existing data was not changed.",
                str(exc))), 400
        prepared[key] = (canonical, ", ".join(check.missing_optional))

    if not prepared:
        return _page(season=season,
                     result=_result("cfbdepth", False, "No files selected.")), 400

    rows, notes = [], []
    for key, label, _table in CFBDEPTH_KINDS:
        if key not in prepared:
            continue
        canonical, missing_optional = prepared[key]
        imported = importers[key](_repository(), canonical)
        rows.append({"dataset": label, "kind": "snapshot",
                     "detail": _plural(imported, "row")})
        if missing_optional:
            notes.append(f"{label}: optional columns absent — {missing_optional}")

    cache.clear()
    return _page(season=season, result=_result(
        "cfbdepth", True,
        f"Replaced {_plural(len(rows), 'snapshot')}.",
        "; ".join(notes), rows))


@data_import_pages.post("/college-football/data-import/board")
def import_board_route():
    """Upload an external consensus big board (Rank, Player, School, Position CSV).

    Previously CLI-only (prospects_cli.py); this pairs the same import_board()
    call with the upload/status pattern PFF and CFBDepth already have here,
    so a draft-year refresh no longer needs shell access to the machine
    running the app.
    """
    season = request.form.get("season", type=int) or _default_season(_repository())
    draft_year = request.form.get("draft_year", type=int)
    if not _authorized():
        return _page(season=season, draft_year=draft_year, result=_result(
            "board", False, "Authorization failed.",
            "Nothing was read and nothing was changed.")), 401

    if not draft_year:
        return _page(season=season, result=_result(
            "board", False, "Draft year is required.")), 400
    roster_season = request.form.get("roster_season", type=int) or (season + 1)
    source = (request.form.get("source") or "consensus").strip() or "consensus"

    storage = request.files.get("file")
    if not storage or not storage.filename:
        return _page(season=season, draft_year=draft_year,
                     result=_result("board", False, "No file selected.")), 400
    name = Path(storage.filename).name
    if not name.lower().endswith(".csv"):
        return _page(season=season, draft_year=draft_year,
                     result=_result("board", False, "File must be a CSV.")), 400
    raw = storage.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return _page(season=season, draft_year=draft_year, result=_result(
            "board", False,
            f"{name} is larger than the "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")), 400

    with TemporaryDirectory(prefix="cfb-board-upload-") as temp:
        path = Path(temp) / name
        path.write_bytes(raw)
        try:
            counts = import_board(_repository(), path, draft_year=draft_year,
                                  source=source, roster_season=roster_season)
        except Exception as exc:
            return _page(season=season, draft_year=draft_year, result=_result(
                "board", False,
                "Import failed — existing data was not changed.", str(exc))), 400

    if counts["rows"] == 0:
        return _page(season=season, draft_year=draft_year, result=_result(
            "board", False,
            f"{name} had no readable rows.",
            "Expected a CSV with Rank, Player, School, and Position columns.")), 400

    cache.clear()
    detail = (f"{counts['confirmed']} confirmed to a current roster player, "
             f"{counts['school_mismatch']} school mismatch, "
             f"{counts['unresolved']} unresolved "
             f"({counts['unknown_school']} unrecognized school)")
    return _page(season=season, draft_year=draft_year, result=_result(
        "board", True,
        f"Imported {_plural(counts['rows'], 'row')} into the {draft_year} \"{source}\" board.",
        detail))


@data_import_pages.get("/college-football/cfbdepth-import/")
def legacy_cfbdepth_import():
    """The CFBDepth-only page now lives inside the one that shows both."""
    return redirect(url_for("cfb_data_import.data_import"), code=302)
