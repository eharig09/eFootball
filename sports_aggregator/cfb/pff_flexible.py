"""Flexible discovery/preflight for new PFF CSV export batches.

PFF download filenames are not stable: browser downloads commonly add numeric
suffixes even when the export schema is identical.  The core importer remains
the trusted database writer; this module identifies files by their headers and
stages them under the canonical names that importer already understands.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Any

from sports_aggregator.cfb.pff import DATASETS, SUPPLEMENTAL_DATASETS, PFFImporter


# Dataset recognition intentionally uses distinctive columns rather than exact
# complete schemas so harmless PFF additions do not break imports.
PRIMARY_SIGNATURES: dict[str, frozenset[str]] = {
    "coverage": frozenset({"player_id", "player", "team_name", "position", "grades_coverage_defense", "snap_counts_coverage"}),
    "defense": frozenset({"player_id", "player", "team_name", "position", "grades_defense", "snap_counts_defense"}),
    "blocking": frozenset({"player_id", "player", "team_name", "position", "snap_counts_offense", "grades_pass_block", "grades_run_block"}),
    "passing": frozenset({"player_id", "player", "team_name", "position", "grades_pass", "dropbacks"}),
    "pass_rush": frozenset({"player_id", "player", "team_name", "position", "grades_pass_rush_defense", "snap_counts_pass_rush"}),
    "receiving": frozenset({"player_id", "player", "team_name", "position", "grades_pass_route", "routes"}),
    "rushing": frozenset({"player_id", "player", "team_name", "position", "grades_run", "attempts"}),
}

SUPPLEMENTAL_SIGNATURES: dict[str, frozenset[str]] = {
    "coverage_scheme": frozenset({"player_id", "player", "team_name", "man_grades_coverage_defense", "zone_grades_coverage_defense"}),
    "passing_depth": frozenset({"player_id", "player", "team_name", "short_grades_pass", "medium_grades_pass", "deep_grades_pass"}),
    "receiving_scheme": frozenset({"player_id", "player", "team_name", "man_grades_pass_route", "zone_grades_pass_route"}),
    "returns": frozenset({"player_id", "player", "team_name", "grades_return", "total_attempts"}),
    "run_defense_detail": frozenset({"player_id", "player", "team_name", "grades_run_defense", "snap_counts_run"}),
    "receiving_concept": frozenset({"player_id", "player", "team_name", "screen_grades_pass_route", "slot_grades_pass_route"}),
    "receiving_depth": frozenset({"player_id", "player", "team_name", "behind_los_grades_pass_route", "deep_grades_pass_route"}),
    # No grade column at all, so nothing here overlaps a grades_* field -- the
    # coverage_snaps_per_target/per_reception pair is distinctive enough on
    # its own (the primary "coverage"/"defense" signatures use
    # snap_counts_coverage, a different column, so there's no collision).
    "slot_coverage": frozenset({"player_id", "player", "team_name", "coverage_snaps",
                                "coverage_snaps_per_target", "coverage_snaps_per_reception"}),
}

CANONICAL_PRIMARY = {dataset: filename for filename, (dataset, _grade, _usage) in DATASETS.items()}
CANONICAL_SUPPLEMENTAL = {dataset: filename for filename, (dataset, _fields) in SUPPLEMENTAL_DATASETS.items()}


@dataclass(frozen=True, slots=True)
class PFFBatchPreflight:
    directory: str
    primary: dict[str, str]
    supplemental: dict[str, str]
    missing_primary: tuple[str, ...]
    duplicates: dict[str, tuple[str, ...]]
    unclassified: tuple[str, ...]
    csv_files: int

    @property
    def ready(self) -> bool:
        return not self.missing_primary and not self.duplicates

    def as_dict(self) -> dict[str, Any]:
        packet = asdict(self)
        packet["ready"] = self.ready
        return packet


def _headers(path: Path) -> set[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source)
            return {str(value).strip() for value in next(reader, []) if str(value).strip()}
    except (OSError, UnicodeError, csv.Error):
        return set()


def _candidates(headers: set[str]) -> list[tuple[str, str]]:
    """Every dataset whose signature this file satisfies, not just the first.

    Supplemental shapes are listed first, keeping the precedence the
    single-answer version documented: those schemas carry the generic summary
    columns too, so they are the more specific reading when both fit.
    """
    matches = [("supplemental", dataset)
               for dataset, signature in SUPPLEMENTAL_SIGNATURES.items()
               if signature.issubset(headers)]
    matches += [("primary", dataset)
                for dataset, signature in PRIMARY_SIGNATURES.items()
                if signature.issubset(headers)]
    return matches


def _classify(headers: set[str]) -> tuple[str, str] | None:
    """The single best reading of one file, with no batch around it.

    Order-dependent where a file fits more than one signature, which is why
    `preflight_pff_directory` resolves the batch together instead.
    """
    matches = _candidates(headers)
    return matches[0] if matches else None


def _assign(pool: dict[Path, list[tuple[str, str]]]) -> dict[tuple[str, str], Path]:
    """Resolve files to datasets, assigning only what the batch forces.

    PFF's summary exports are supersets of one another: `defense_summary`
    carries the coverage and pass-rush columns as well as its own, and
    `rushing_summary` carries the receiving columns. Testing signatures one
    file at a time and taking the first hit therefore made the answer depend on
    dictionary order -- it read `defense_summary` as coverage, which collided
    with the real coverage export and left `defense` reported missing. Two of
    the seven primary datasets could not be imported from a complete batch.

    Resolving the batch together removes the guesswork without introducing any:
    a dataset only one file can satisfy takes that file, which withdraws it
    from every other dataset's candidates and usually forces the next one. On a
    real export that cascade is enough -- `defense` is forced, which frees
    `coverage`; `passing` is forced, which frees `rushing`, which frees
    `receiving`.

    Nothing is ever assigned on a preference. A dataset still holding two
    possible files when the cascade stops is left unassigned for the caller to
    report, because at that point the batch genuinely does not say which file
    is which, and picking one would silently file a season of grades under the
    wrong dataset.
    """
    assigned: dict[tuple[str, str], Path] = {}
    remaining = {path: list(matches) for path, matches in pool.items()}
    forced = True
    while forced:
        forced = False
        claims: dict[tuple[str, str], list[Path]] = {}
        for path, matches in remaining.items():
            for key in matches:
                claims.setdefault(key, []).append(path)
        # Supplemental before primary, then by name: the loop takes one
        # assignment at a time and recomputes, so this only fixes the order in
        # which equally forced datasets are settled, keeping the result
        # independent of how the filesystem happened to list the directory.
        for key in sorted(claims, key=lambda item: (item[0] != "supplemental", item[1])):
            if len(claims[key]) != 1:
                continue
            assigned[key] = claims[key][0]
            del remaining[claims[key][0]]
            forced = True
            break
    return assigned


def preflight_pff_directory(directory: str | Path) -> PFFBatchPreflight:
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"PFF export directory does not exist: {root}")

    unclassified: list[str] = []
    pool: dict[Path, list[tuple[str, str]]] = {}
    csv_paths = sorted(path for path in root.glob("*.csv") if path.is_file())
    for path in csv_paths:
        matches = _candidates(_headers(path))
        if not matches:
            unclassified.append(path.name)
            continue
        pool[path] = matches

    assigned = _assign(pool)
    primary = {dataset: str(path) for (kind, dataset), path in assigned.items()
               if kind == "primary"}
    supplemental = {dataset: str(path) for (kind, dataset), path in assigned.items()
                    if kind == "supplemental"}

    # Whatever the cascade could not settle: the files that could still be this
    # dataset, which is what an operator needs in order to remove the copies.
    taken = set(assigned.values())
    duplicates: dict[str, tuple[str, ...]] = {}
    for kind, signatures in (("supplemental", SUPPLEMENTAL_SIGNATURES),
                             ("primary", PRIMARY_SIGNATURES)):
        for dataset in signatures:
            if (kind, dataset) in assigned:
                continue
            names = tuple(sorted(path.name for path, matches in pool.items()
                                 if (kind, dataset) in matches and path not in taken))
            if len(names) > 1:
                duplicates[f"{kind}:{dataset}"] = names

    missing = tuple(sorted(set(PRIMARY_SIGNATURES) - set(primary)))
    return PFFBatchPreflight(
        directory=str(root), primary=primary, supplemental=supplemental,
        missing_primary=missing, duplicates=duplicates,
        unclassified=tuple(unclassified), csv_files=len(csv_paths),
    )


def import_pff_directory_flexible(repository, directory: str | Path, *, season: int,
                                  roster_season: int):
    """Validate a fresh PFF export directory, then run the established importer."""
    root = Path(directory)
    check = preflight_pff_directory(root)
    if not check.ready:
        problems = []
        if check.missing_primary:
            problems.append("missing primary datasets: " + ", ".join(check.missing_primary))
        if check.duplicates:
            duplicate_text = "; ".join(
                f"{dataset}=[{', '.join(files)}]" for dataset, files in sorted(check.duplicates.items())
            )
            problems.append("duplicate dataset matches: " + duplicate_text)
        raise ValueError("PFF preflight failed: " + "; ".join(problems))

    with TemporaryDirectory(prefix="cfb-pff-") as temp:
        staging = Path(temp)
        for dataset, source in check.primary.items():
            shutil.copy2(source, staging / CANONICAL_PRIMARY[dataset])
        for dataset, source in check.supplemental.items():
            shutil.copy2(source, staging / CANONICAL_SUPPLEMENTAL[dataset])

        # Preserve optional historical OL context when the batch carries it.
        history = root / "oline_data"
        if history.is_dir():
            shutil.copytree(history, staging / "oline_data")

        return PFFImporter(repository).import_directory(
            staging, season=int(season), roster_season=int(roster_season)
        )
