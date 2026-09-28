from pathlib import Path
import sqlite3

from sports_aggregator.nfl.storage_audit import storage_status


def test_storage_status_is_public_safe_and_reports_budget(tmp_path: Path):
    database = tmp_path / "private" / "nfl.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES (?)", ("x" * 1000,))

    packet = storage_status(database, max_bytes=1)

    assert packet["status"] == "over-budget"
    assert packet["database_bytes"] > 0
    assert packet["page_count"] > 0
    assert "path" not in packet
    assert str(tmp_path) not in str(packet)


def test_storage_status_handles_a_database_that_does_not_exist(tmp_path: Path):
    packet = storage_status(tmp_path / "missing.sqlite3", max_bytes=1024)

    assert packet["status"] == "ok"
    assert packet["total_bytes"] == 0
    assert packet["page_count"] == 0
