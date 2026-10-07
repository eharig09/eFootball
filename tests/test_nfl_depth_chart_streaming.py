"""Depth charts are 600k+ rows; converting them whole to dicts peaked near 1 GB and the step was killed."""
from __future__ import annotations

import sqlite3
import tracemalloc

import pytest

pd = pytest.importorskip("pandas")

from sports_aggregator.nfl import sync as sync_module
from sports_aggregator.nfl.repository import NFLRepository


def _frame(n, *, extra_columns=0):
    data = {
        "dt": [f"2026-09-{1 + i % 28:02d}T00:00:00Z" for i in range(n)],
        "team": ["KAN" if i % 2 else "BUF" for i in range(n)],
        "player_name": [f"Player {i}" for i in range(n)],
        "gsis_id": [f"00-{i:07d}" for i in range(n)],
        "espn_id": [str(i) for i in range(n)],
        "pos_grp": ["Offense"] * n, "pos_id": ["QB"] * n, "pos_name": ["Quarterback"] * n,
        "pos_abb": ["QB"] * n, "pos_slot": [1] * n, "pos_rank": [1] * n,
    }
    for k in range(extra_columns):
        data[f"extra_{k}"] = ["x" * 20] * n
    return pd.DataFrame(data)


@pytest.fixture()
def repo(tmp_path):
    repository = NFLRepository(tmp_path / "nfl.sqlite3")
    repository.initialize()
    return repository


def _count(repo, season=2026):
    with sqlite3.connect(repo.path) as connection:
        return connection.execute("SELECT COUNT(*) FROM depth_chart_snapshots WHERE season=?", (season,)).fetchone()[0]


def test_records_arrive_in_chunks_and_cover_every_row():
    frame = _frame(95)
    rows = list(sync_module._iter_records("depth_charts", frame, chunk=40))
    assert len(rows) == 95 and rows[0]["player_name"] == "Player 0" and rows[-1]["player_name"] == "Player 94"


def test_only_the_columns_the_insert_reads_are_converted():
    frame = _frame(10, extra_columns=5)
    row = next(iter(sync_module._iter_records("depth_charts", frame, columns=sync_module.DEPTH_CHART_COLUMNS)))
    assert "extra_0" not in row and row["gsis_id"] == "00-0000000"


def test_a_missing_required_column_is_still_an_error():
    with pytest.raises(ValueError, match="missing required columns"):
        list(sync_module._iter_records("depth_charts", _frame(3).drop(columns=["team"])))


def test_the_repository_stores_a_generator_in_batches(repo, monkeypatch):
    frame = _frame(50_123)
    stored = repo.replace_depth_charts(2026, sync_module._iter_records("depth_charts", frame, chunk=7_000))
    assert stored == 50_123 and _count(repo) == 50_123


def test_replacing_a_season_removes_what_was_there_and_keeps_other_seasons(repo):
    repo.replace_depth_charts(2025, iter(_frame(30).to_dict("records")))
    repo.replace_depth_charts(2026, iter(_frame(100).to_dict("records")))
    repo.replace_depth_charts(2026, iter(_frame(40).to_dict("records")))
    assert _count(repo, 2026) == 40 and _count(repo, 2025) == 30


def test_rows_missing_a_team_or_name_are_skipped_not_stored(repo):
    rows = [{"dt": "2026-09-01", "team": "KAN", "player_name": "A B", "gsis_id": "g1"},
            {"dt": "2026-09-01", "team": "KAN", "player_name": "", "gsis_id": "g2"},
            {"dt": "", "team": "KAN", "player_name": "C D", "gsis_id": "g3"}]
    assert repo.replace_depth_charts(2026, iter(rows)) == 1


def test_a_failure_part_way_leaves_the_previous_season_intact(repo):
    repo.replace_depth_charts(2026, iter(_frame(25).to_dict("records")))

    def exploding():
        yield from _frame(30_000).to_dict("records")[:25_000]
        raise RuntimeError("download died")

    with pytest.raises(RuntimeError):
        repo.replace_depth_charts(2026, exploding())
    assert _count(repo) == 25                       # rolled back, not half-replaced


def test_peak_memory_is_far_below_converting_the_whole_frame(repo):
    frame = _frame(120_000)
    tracemalloc.start()
    repo.replace_depth_charts(2026, sync_module._iter_records("depth_charts", frame, columns=sync_module.DEPTH_CHART_COLUMNS))
    _, streamed = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    tracemalloc.start()
    whole = frame.to_dict("records")
    _, materialised = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del whole
    assert streamed < materialised * 0.35, (streamed, materialised)
