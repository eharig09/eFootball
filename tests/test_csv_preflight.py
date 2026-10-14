from __future__ import annotations

from pathlib import Path

from sports_aggregator.cfb.cfbdepth_flexible import (
    canonicalize_cfbdepth_upload,
    preflight_cfbdepth_upload,
)
from sports_aggregator.cfb.pff_flexible import PRIMARY_SIGNATURES, preflight_pff_directory


def test_pff_preflight_uses_headers_not_download_filenames(tmp_path: Path):
    for index, (dataset, signature) in enumerate(PRIMARY_SIGNATURES.items(), start=1):
        path = tmp_path / f"random-download-{index}.csv"
        path.write_text(",".join(sorted(signature)) + "\n" + ",".join("1" for _ in signature) + "\n")

    report = preflight_pff_directory(tmp_path)

    assert report.ready is True
    assert set(report.primary) == set(PRIMARY_SIGNATURES)
    assert report.missing_primary == ()
    assert report.duplicates == {}


def test_pff_preflight_rejects_duplicate_dataset_matches(tmp_path: Path):
    # Same headers, different contents: a filtered export beside the full one.
    signature = sorted(PRIMARY_SIGNATURES["passing"])
    header = ",".join(signature) + "\n"
    row = ",".join("1" for _ in signature) + "\n"
    (tmp_path / "passing-one.csv").write_text(header + row)
    (tmp_path / "passing-two.csv").write_text(header + row + row)

    report = preflight_pff_directory(tmp_path)

    assert report.ready is False
    assert "primary:passing" in report.duplicates
    assert report.identical_copies == ()


def test_pff_preflight_treats_byte_identical_redownloads_as_one_file(tmp_path: Path):
    for index, (dataset, signature) in enumerate(PRIMARY_SIGNATURES.items(), start=1):
        content = ",".join(sorted(signature)) + "\n" + ",".join("1" for _ in signature) + "\n"
        (tmp_path / f"{dataset}_summary.csv").write_text(content)
    passing = (tmp_path / "passing_summary.csv").read_text()
    (tmp_path / "passing_summary (1).csv").write_text(passing)
    (tmp_path / "passing_summary (2).csv").write_text(passing)

    report = preflight_pff_directory(tmp_path)

    assert report.ready is True
    assert report.duplicates == {}
    assert Path(report.primary["passing"]).name == "passing_summary (1).csv" or         Path(report.primary["passing"]).name == "passing_summary.csv"
    assert len(report.identical_copies) == 2
    assert report.csv_files == len(PRIMARY_SIGNATURES) + 2


def test_cfbdepth_upload_normalizes_header_punctuation():
    raw = (
        "school,conference,active_players,transfers,transfer %,home_grown,home grown %,"
        "5 star,4 star,3 star,2 star,0 star,blue_chip %,ol_avg_wt,dl avg wt,roster avg wt\n"
        "Example U,Test,85,12,14.1,73,85.9,1,10,30,20,24,12.9,315,292,228\n"
    )

    check, canonical = canonicalize_cfbdepth_upload(raw, expected_kind="roster")

    assert check.ready is True
    assert "Active Players" in canonical.splitlines()[0]
    assert "Example U" in canonical


def test_cfbdepth_upload_rejects_wrong_slot():
    updates = "Name,Team,Status,Last Update,Update\nPlayer One,Example U,Out,2026-08-30,injury\n"

    check = preflight_cfbdepth_upload(updates, expected_kind="roster")

    assert check.ready is False
    assert check.missing_required


def _write(path: Path, *signatures) -> None:
    """A file carrying the union of several dataset signatures.

    Which is what PFF actually ships: `defense_summary` is a superset of the
    coverage and pass-rush exports, not a disjoint file.
    """
    columns = sorted(set().union(*signatures))
    path.write_text(",".join(columns) + "\n" + ",".join("1" for _ in columns) + "\n")


def _append_row(path: Path) -> None:
    """Make a file differ from its siblings without changing which dataset it is."""
    columns = path.read_text().splitlines()[0].split(",")
    with path.open("a") as handle:
        handle.write(",".join("2" for _ in columns) + "\n")


def test_a_superset_export_does_not_steal_the_dataset_it_merely_contains(tmp_path: Path):
    """The bug this replaced: `defense_summary` read as coverage.

    Testing signatures one file at a time and taking the first hit made the
    answer depend on dictionary order, so the summary export claimed `coverage`
    -- colliding with the real coverage file -- and `defense` was reported
    missing from a batch that contained it.
    """
    _write(tmp_path / "defense_summary.csv",
           PRIMARY_SIGNATURES["defense"], PRIMARY_SIGNATURES["coverage"])
    _write(tmp_path / "defense_coverage_summary.csv", PRIMARY_SIGNATURES["coverage"])

    report = preflight_pff_directory(tmp_path)

    assert report.primary["defense"].endswith("defense_summary.csv")
    assert report.primary["coverage"].endswith("defense_coverage_summary.csv")
    assert "primary:coverage" not in report.duplicates


def test_resolving_one_dataset_frees_the_next(tmp_path: Path):
    """`passing` is forced, which frees `rushing`, which frees `receiving`."""
    _write(tmp_path / "passing_summary.csv",
           PRIMARY_SIGNATURES["passing"], PRIMARY_SIGNATURES["rushing"])
    _write(tmp_path / "rushing_summary.csv",
           PRIMARY_SIGNATURES["rushing"], PRIMARY_SIGNATURES["receiving"])
    _write(tmp_path / "receiving_summary.csv", PRIMARY_SIGNATURES["receiving"])

    report = preflight_pff_directory(tmp_path)

    assert report.primary["passing"].endswith("passing_summary.csv")
    assert report.primary["rushing"].endswith("rushing_summary.csv")
    assert report.primary["receiving"].endswith("receiving_summary.csv")


def test_an_ambiguity_the_batch_cannot_settle_is_reported_not_guessed(tmp_path: Path):
    """Two files that could each be either dataset, and nothing to break the tie.

    Choosing one would file a season of grades under the wrong dataset without
    saying so, so the batch is refused instead.
    """
    both = (PRIMARY_SIGNATURES["rushing"], PRIMARY_SIGNATURES["receiving"])
    _write(tmp_path / "one.csv", *both)
    _write(tmp_path / "two.csv", *both)
    _append_row(tmp_path / "two.csv")  # distinct contents: identical bytes collapse to one file

    report = preflight_pff_directory(tmp_path)

    assert report.ready is False
    assert "rushing" in report.missing_primary
    assert "receiving" in report.missing_primary
    assert report.duplicates["primary:rushing"] == ("one.csv", "two.csv")
    assert "rushing" not in report.primary
    assert "receiving" not in report.primary


def test_the_assignment_does_not_depend_on_filesystem_order(tmp_path: Path):
    """Same batch, names that sort the other way: same answer."""
    _write(tmp_path / "zzz_defense_summary.csv",
           PRIMARY_SIGNATURES["defense"], PRIMARY_SIGNATURES["coverage"])
    _write(tmp_path / "aaa_coverage.csv", PRIMARY_SIGNATURES["coverage"])

    report = preflight_pff_directory(tmp_path)

    assert report.primary["defense"].endswith("zzz_defense_summary.csv")
    assert report.primary["coverage"].endswith("aaa_coverage.csv")


def test_duplicate_copies_of_one_export_are_still_refused(tmp_path: Path):
    """Resolving the batch together must not paper over real duplicates: three
    downloads of the same export are three files nobody can tell apart.

    They must differ in content: byte-identical copies are collapsed to one
    file instead and are covered by their own test.
    """
    for index in range(3):
        path = tmp_path / f"pass_rush_summary ({index}).csv"
        _write(path, PRIMARY_SIGNATURES["pass_rush"])
        for _ in range(index):
            _append_row(path)

    report = preflight_pff_directory(tmp_path)

    assert report.ready is False
    assert len(report.duplicates["primary:pass_rush"]) == 3
