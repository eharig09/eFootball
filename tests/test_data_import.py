"""The CSV import page: what it reports, and what it refuses.

The refusals matter more than the imports here. Both of these sources replace
snapshots wholesale, so a batch that is wrong in a way nobody notices is worse
than one that fails loudly -- and every failing path below asserts that the
data already loaded came through untouched.
"""

from __future__ import annotations

import io
from pathlib import Path
import sqlite3
from contextlib import closing

import pytest

from app import create_app
from sports_aggregator.cfb.pff_flexible import (
    PRIMARY_SIGNATURES, SUPPLEMENTAL_SIGNATURES,
)


TOKEN = "test-admin-pin"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CFB_PAGE_CACHE_SECONDS", "0")
    app = create_app({
        "TESTING": True,
        "REGISTER_LEGACY_DASHBOARDS": False,
        "CFB_DATABASE_PATH": str(tmp_path / "cfb.sqlite3"),
        "CFB_ADMIN_PIN": TOKEN,
        "CFB_DEFAULT_SEASON": 2025,
    })
    return app.test_client()


def _csv(signature) -> bytes:
    """One row carrying exactly the columns that identify a dataset."""
    columns = sorted(signature)
    body = ",".join(columns) + "\n" + ",".join("1" for _ in columns) + "\n"
    return body.encode("utf-8")


def _batch(datasets=None, *, supplemental=False) -> list[tuple[io.BytesIO, str]]:
    wanted = datasets if datasets is not None else list(PRIMARY_SIGNATURES)
    files = [(io.BytesIO(_csv(PRIMARY_SIGNATURES[name])), f"download-{name}.csv")
             for name in wanted]
    if supplemental:
        files += [(io.BytesIO(_csv(signature)), f"download-{name}.csv")
                  for name, signature in SUPPLEMENTAL_SIGNATURES.items()]
    return files


def _metric_rows(client) -> int:
    path = client.application.config["CFB_DATABASE_PATH"]
    if not Path(path).exists():
        return 0
    with closing(sqlite3.connect(path)) as connection, connection:
        try:
            return connection.execute(
                "SELECT COUNT(*) FROM pff_player_metrics").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


def _post_pff(client, files, **extra):
    data = {"token": TOKEN, "season": "2025", "roster_season": "2026", "batch": files}
    data.update(extra)
    return client.post("/college-football/data-import/pff", data=data,
                       content_type="multipart/form-data")


def test_the_page_reports_an_empty_source_as_empty_rather_than_missing(client):
    body = client.get("/college-football/data-import/").get_data(as_text=True)
    assert "0/7" in body        # primary datasets present
    assert "no batch imported" in body
    assert "no export imported" in body


def test_an_upload_without_the_admin_secret_changes_nothing(client):
    response = _post_pff(client, _batch(), token="wrong")
    assert response.status_code == 401
    assert "Authorization failed" in response.get_data(as_text=True)
    assert _metric_rows(client) == 0


def test_a_partial_batch_is_refused_before_anything_is_written(client):
    """The importer already refuses a short batch. The page has to say which
    files were missing, because "it failed" is not actionable at 2am."""
    response = _post_pff(client, _batch(["passing", "blocking"]))
    body = response.get_data(as_text=True)

    assert response.status_code == 400
    assert "Preflight failed" in body
    assert "the current snapshot is unchanged" in body
    for absent in ("coverage", "defense", "pass_rush", "receiving", "rushing"):
        assert absent in body
    assert _metric_rows(client) == 0


def test_duplicate_downloads_name_the_datasets_that_collided(client):
    """A download folder accumulates `passing (1).csv`, `passing (2).csv`.

    Selecting all of them is the obvious mistake, and the fix -- deselect the
    copies -- is only obvious if the page says which dataset matched twice.
    """
    files = _batch()
    # Same headers, different contents: a fuller export beside the first.
    longer = _csv(PRIMARY_SIGNATURES["passing"]) + b",".join(b"2" for _ in PRIMARY_SIGNATURES["passing"]) + b"\n"
    files.append((io.BytesIO(longer), "passing (1).csv"))
    response = _post_pff(client, files)
    body = response.get_data(as_text=True)

    assert response.status_code == 400
    assert "primary:passing matched 2 files" in body
    # Row counts, not just names: the copies in a real download folder are a
    # filtered export beside a full season, and the names cannot say which.
    assert "passing (1).csv (2 rows)" in body
    assert _metric_rows(client) == 0


def test_byte_identical_redownloads_are_ignored_and_said_so(client):
    """The same export downloaded again is not a choice to make."""
    files = _batch()
    for suffix in ("(1)", "(2)"):
        files.append((io.BytesIO(_csv(PRIMARY_SIGNATURES["passing"])), f"download-passing {suffix}.csv"))
    response = _post_pff(client, files, validate_only="1")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "would import cleanly" in body
    assert "2 identical copies ignored" in body


def test_validate_only_reports_a_clean_batch_without_importing_it(client):
    response = _post_pff(client, _batch(supplemental=True), validate_only="1")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "would import cleanly" in body
    assert _metric_rows(client) == 0, "validate-only must not write"


def test_files_that_are_not_csvs_are_ignored_and_said_so(client):
    files = _batch()
    files.append((io.BytesIO(b"not a csv"), "notes.txt"))
    response = _post_pff(client, files, validate_only="1")

    assert response.status_code == 200
    assert "1 non-CSV file ignored" in response.get_data(as_text=True)


def test_a_complete_batch_imports_and_the_page_then_says_so(client):
    response = _post_pff(client, _batch(supplemental=True))
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    assert "Imported" in response.get_data(as_text=True)
    assert _metric_rows(client) > 0

    body = client.get("/college-football/data-import/?season=2025").get_data(as_text=True)
    assert "7/7" in body
    assert "just now" in body


def test_the_old_cfbdepth_import_url_still_reaches_the_page(client):
    response = client.get("/college-football/cfbdepth-import/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/college-football/data-import/")


ROSTER_CSV = (
    "school,conference,active_players,transfers,transfer %,home_grown,home grown %,"
    "5 star,4 star,3 star,2 star,0 star,blue_chip %,ol_avg_wt,dl avg wt,roster avg wt\n"
    "Example U,Test,85,12,14.1,73,85.9,1,10,30,20,24,12.9,315,292,228\n"
).encode("utf-8")


def _cfbdepth_rows(client, table: str) -> int:
    path = client.application.config["CFB_DATABASE_PATH"]
    with closing(sqlite3.connect(path)) as connection, connection:
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


def test_a_cfbdepth_export_replaces_only_its_own_snapshot(client):
    response = client.post(
        "/college-football/data-import/cfbdepth",
        data={"token": TOKEN, "roster": (io.BytesIO(ROSTER_CSV), "Roster Breakdown.csv")},
        content_type="multipart/form-data")

    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    assert "Replaced 1 snapshot" in response.get_data(as_text=True)
    assert _cfbdepth_rows(client, "cfbdepth_roster_breakdown") == 1
    # The two nobody uploaded are left alone rather than emptied.
    assert _cfbdepth_rows(client, "cfbdepth_team_impact") == 0
    assert _cfbdepth_rows(client, "cfbdepth_player_updates") == 0


def test_the_wrong_export_in_a_slot_leaves_the_snapshot_alone(client):
    """Three file inputs side by side, three exports that all look alike.

    Putting Player Updates into the Roster slot is the mistake the layout
    invites, and since every import replaces its snapshot whole, it has to fail
    before the existing rows are touched rather than after.
    """
    client.post(
        "/college-football/data-import/cfbdepth",
        data={"token": TOKEN, "roster": (io.BytesIO(ROSTER_CSV), "roster.csv")},
        content_type="multipart/form-data")
    assert _cfbdepth_rows(client, "cfbdepth_roster_breakdown") == 1

    updates_csv = b"Name,Team,Status\nA Player,Example U,OUT\n"
    response = client.post(
        "/college-football/data-import/cfbdepth",
        data={"token": TOKEN, "roster": (io.BytesIO(updates_csv), "Player Updates.csv")},
        content_type="multipart/form-data")

    assert response.status_code == 400
    assert "existing data was not changed" in response.get_data(as_text=True)
    assert _cfbdepth_rows(client, "cfbdepth_roster_breakdown") == 1


def test_optional_columns_absent_is_reported_rather_than_swallowed(client):
    """A roster export needs only `School` to be accepted, and one carrying
    nothing else would replace the whole snapshot with near-empty rows. That is
    the importer's contract, so the page has to say what was not in the file.
    """
    response = client.post(
        "/college-football/data-import/cfbdepth",
        data={"token": TOKEN,
              "roster": (io.BytesIO(b"school\nExample U\n"), "roster.csv")},
        content_type="multipart/form-data")

    assert response.status_code == 200
    assert "optional columns absent" in response.get_data(as_text=True)
