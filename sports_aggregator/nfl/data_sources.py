"""Validated catalog of candidate NFL data sources from the project workbook."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sports_aggregator.nfl.source_directory import _sheet_rows


DEFAULT_PATH = Path("data/nfl/NFL_Data_Sources_Directory.xlsx")
REQUIRED_COLUMNS = {
    "Provider", "Dataset / Endpoint", "Access Type", "Cost", "Authentication",
    "Format", "Data Tags", "Known Limitations", "License / Terms", "Recommendation",
    "Integration Route", "Documentation URL", "Reviewed",
}


class NFLDataSourceDirectoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NFLDataSource:
    provider: str
    dataset: str
    access_type: str
    cost: str
    authentication: str
    format: str
    historical_coverage: str
    update_cadence: str
    tags: tuple[str, ...]
    uses: str
    fields: str
    limitations: str
    license_terms: str
    recommendation: str
    integration_route: str
    documentation_url: str
    reviewed: str

    @property
    def paid(self) -> bool:
        # "Free for non-commercial use" is still a no-cost candidate; retain
        # its license restriction rather than misclassifying it as a paid API.
        cost = self.cost.casefold().strip()
        return (
            self.access_type.casefold().strip() == "documented commercial api"
            or self.recommendation.casefold().strip() == "commercial option"
            or cost.startswith("paid") or cost.startswith("trial")
        )


def load_data_sources(path: str | Path = DEFAULT_PATH) -> tuple[NFLDataSource, ...]:
    rows = _sheet_rows(path)
    header_index = next((index for index, row in enumerate(rows) if "Provider" in row), None)
    if header_index is None:
        raise NFLDataSourceDirectoryError("Source Directory sheet has no Provider header")
    headers = rows[header_index]
    missing = REQUIRED_COLUMNS - set(headers)
    if missing:
        raise NFLDataSourceDirectoryError(f"data source directory missing columns {sorted(missing)}")

    sources = []
    identities = set()
    for values in rows[header_index + 1:]:
        row = {header: values[index] if index < len(values) else ""
               for index, header in enumerate(headers) if header}
        if not row.get("Provider", "").strip():
            continue
        identity = (row["Provider"].strip().casefold(), row["Dataset / Endpoint"].strip().casefold())
        if identity in identities:
            raise NFLDataSourceDirectoryError(f"duplicate source {identity[0]} / {identity[1]}")
        identities.add(identity)
        url = row["Documentation URL"].strip()
        if not url.startswith("https://"):
            raise NFLDataSourceDirectoryError(f"source {row['Provider']} has no HTTPS documentation URL")
        sources.append(NFLDataSource(
            provider=row["Provider"].strip(), dataset=row["Dataset / Endpoint"].strip(),
            access_type=row["Access Type"].strip(), cost=row["Cost"].strip(),
            authentication=row["Authentication"].strip(), format=row["Format"].strip(),
            historical_coverage=row.get("Historical Coverage", "").strip(),
            update_cadence=row.get("Update Cadence", "").strip(),
            tags=tuple(tag.strip() for tag in row["Data Tags"].split(";") if tag.strip()),
            uses=row.get("Best Application Uses", "").strip(),
            fields=row.get("Key Fields / Measures", "").strip(),
            limitations=row["Known Limitations"].strip(),
            license_terms=row["License / Terms"].strip(),
            recommendation=row["Recommendation"].strip(),
            integration_route=row["Integration Route"].strip(),
            documentation_url=url, reviewed=row["Reviewed"].strip(),
        ))
    return tuple(sources)


def free_sources(path: str | Path = DEFAULT_PATH) -> tuple[NFLDataSource, ...]:
    """Return sources eligible for exploration under the current no-paid-source policy."""
    return tuple(source for source in load_data_sources(path) if not source.paid)


if __name__ == "__main__":
    sources = load_data_sources()
    free = tuple(source for source in sources if not source.paid)
    print(f"sources={len(sources)} free_or_open={len(free)} paid_ignored={len(sources) - len(free)}")
    for source in free:
        print(f"{source.recommendation}: {source.provider} — {source.dataset}")
