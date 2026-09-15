"""Admin page for uploading PFF exports directly through the browser.

The read-only NFLPFFService already knows how to catalog and import PFF CSVs
from a directory (see sports_aggregator/nfl/pff.py); it was built for a
sibling scouting_report checkout that only exists on a developer's machine.
This page saves uploaded exports into a persistent, flat directory that
service also scans, so a deploy with no sibling repo (Render) has a way to
get PFF data in at all.
"""

from __future__ import annotations

from pathlib import Path
import secrets

from flask import Blueprint, current_app, render_template, request, session

from sports_aggregator.nfl.nflverse import current_season
from sports_aggregator.nfl.pff import PFF_FAMILIES, NFLPFFService
from sports_aggregator.page_cache import cache


nfl_data_import_pages = Blueprint("nfl_data_import", __name__)

#: A PFF batch is a dozen CSVs of a few hundred KB each. Generous enough for a
#: season export, small enough that a mistaken upload fails fast rather than
#: after a long transfer.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


def _repository():
    return current_app.extensions["nfl_repository"]


def _service() -> NFLPFFService:
    return NFLPFFService(_repository(), current_app.config["NFL_PFF_SOURCE_ROOT"],
                         current_app.config.get("NFL_PFF_UPLOAD_ROOT"))


def _upload_root() -> Path:
    root = Path(current_app.config["NFL_PFF_UPLOAD_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    return root


def _authorized() -> bool:
    """The same gate the CFB PFF/CFBDepth import has always used."""
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


def _family_state(service: NFLPFFService, season: int) -> list[dict]:
    """What the PFF snapshot holds for one season, family by family.

    Reported per family rather than as one total because a batch can import
    cleanly while still being short a file, and a total would hide that.
    """
    catalog = service.catalog_rows()
    by_family: dict[str, list[dict]] = {}
    for row in catalog:
        if row["season"] == season:
            by_family.setdefault(row["family"], []).append(row)
    entries = []
    for family in PFF_FAMILIES:
        matches = by_family.get(family, [])
        best = sorted(matches, key=lambda row: (-row["usable"], -row["row_count"]))[0] if matches else None
        entries.append({
            "family": family,
            "present": bool(best and best["usable"]),
            "row_count": best["row_count"] if best else 0,
            "confidence": (best["confidence"] if best else 0) * 100,
            "file": Path(best["path"]).name if best else None,
            "note": best["note"] if best and not best["usable"] else None,
        })
    return entries


def _result(ok: bool, headline: str, detail: str = "", rows: list[dict] | None = None) -> dict:
    return {"ok": ok, "headline": headline, "detail": detail, "rows": rows or []}


def _page(*, season: int, result: dict | None = None):
    service = _service()
    return render_template(
        "nfl_data_import.html",
        season=season,
        seasons=sorted({season, current_season(), current_season() - 1}, reverse=True),
        families=_family_state(service, season),
        counts=service.counts(season),
        result=result,
    )


@nfl_data_import_pages.get("/nfl/data-import/")
def data_import():
    season = request.args.get("season", type=int) or current_season()
    return _page(season=season)


@nfl_data_import_pages.post("/nfl/data-import/pff")
def import_pff():
    season = request.form.get("season", type=int) or current_season()
    if not _authorized():
        return _page(season=season, result=_result(
            False, "Authorization failed.", "Nothing was uploaded or changed.")), 401

    files = [storage for storage in request.files.getlist("batch") if storage and storage.filename]
    if not files:
        return _page(season=season, result=_result(False, "No files selected.")), 400

    destination = _upload_root()
    saved: list[str] = []
    for storage in files:
        name = Path(storage.filename).name
        if not name.lower().endswith(".csv"):
            continue
        raw = storage.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            return _page(season=season, result=_result(
                False, f"{name} is larger than the "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
                "Nothing was uploaded or changed.")), 400
        (destination / name).write_bytes(raw)
        saved.append(name)

    if not saved:
        return _page(season=season, result=_result(
            False, "None of the selected files were CSVs.")), 400

    service = _service()
    # Force a fresh fingerprint pass so the newly saved files are catalogued
    # (including which season each was matched to) before syncing.
    catalog = {Path(row["path"]).name: row for row in service.scan(force=True)}
    preflight_rows = []
    for name in saved:
        row = catalog.get(name)
        if row is None:
            preflight_rows.append({"file": name, "detail": "could not be re-scanned"})
            continue
        family = row["family"]
        if family not in PFF_FAMILIES:
            preflight_rows.append({"file": name, "detail": f"'{family}' is not an NFL PFF family"})
        elif row["usable"]:
            preflight_rows.append({
                "file": name,
                "detail": f"{family} · season {row['season']} · "
                          f"{row['confidence'] * 100:.0f}% roster confidence",
            })
        else:
            preflight_rows.append({
                "file": name,
                "detail": row["note"] or f"{family} · season not identified",
            })

    report = service.sync(season, force_scan=False)
    cache.clear()
    return _page(season=season, result=_result(
        True, f"Uploaded {len(saved)} file(s) and synced the {season} PFF snapshot.",
        f"{report['families']} of {len(PFF_FAMILIES)} families matched this season · "
        f"{report['players']} players · {report['metrics']:,} metric rows"
        + (f" · {report['unresolved']} unresolved to a roster" if report["unresolved"] else ""),
        preflight_rows,
    ))
