"""Sampled ANALYZE turns a single-valued index into 'about 401 rows per key', and the planner believes it."""
from __future__ import annotations

import sqlite3

import pytest

from sports_aggregator.cfb.repository import CFBRepository


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(CFBRepository, "ANALYSIS_LIMIT", 50)
    repo = CFBRepository(tmp_path / "cfb.sqlite3")
    repo.initialize()
    with sqlite3.connect(repo.path) as connection:
        # the shape that went wrong: every row carries the same version; the season is what selects
        connection.executescript("""
            CREATE TABLE plays_probe (play_id INTEGER PRIMARY KEY, season INTEGER, version TEXT);
            CREATE INDEX idx_probe_version ON plays_probe(version);
            CREATE INDEX idx_probe_season ON plays_probe(season);
        """)
        connection.executemany("INSERT INTO plays_probe VALUES(?,?,?)",
                               [(i, 2015 + i % 10, "v1") for i in range(1, 6001)])
    return repo


def _stat(repo, index):
    with sqlite3.connect(repo.path) as connection:
        return connection.execute("SELECT stat FROM sqlite_stat1 WHERE idx=?", (index,)).fetchone()[0]


def test_sampled_analyze_alone_produces_the_misleading_estimate(repository):
    with sqlite3.connect(repository.path) as connection:
        connection.execute("PRAGMA analysis_limit=50")
        connection.execute("ANALYZE")
    assert _stat(repository, "idx_probe_version").split()[1] == "51"      # limit + 1: "very selective"


def test_optimize_repairs_it_so_the_planner_starts_from_the_selective_index(repository):
    repository.optimize()
    assert _stat(repository, "idx_probe_version").split()[:2] == ["6000", "6000"]    # honest: one value, every row
    with sqlite3.connect(repository.path) as connection:
        plan = [row[3] for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM plays_probe WHERE version=? AND season=?", ("v1", 2026))]
    assert "idx_probe_version" not in " ".join(plan) and "idx_probe_season" in " ".join(plan)


def test_the_repair_only_touches_tables_that_carry_the_artifact(repository, monkeypatch):
    with sqlite3.connect(repository.path) as connection:
        connection.execute("PRAGMA analysis_limit=50")
        connection.execute("ANALYZE")
        connection.row_factory = None
        repaired = repository._repair_sampled_statistics(connection)
    assert "plays_probe" in repaired
    assert all(name != "teams" for name in repaired)          # small tables are exact already
