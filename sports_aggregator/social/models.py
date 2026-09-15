from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SourceProfile:
    handle: str
    display_name: str
    organization: str | None
    source_type: str
    specialties: tuple[str, ...]
    conferences: tuple[str, ...] = field(default_factory=tuple)
    teams: tuple[str, ...] = field(default_factory=tuple)
    reliability: int = 4
    original_reporting_score: int = 3
    analysis_score: int = 3
    breaking_news_score: int = 2
    prospect_score: int = 2
    g5_score: int = 2
    priority: int = 3
    active: bool = True

    def __post_init__(self) -> None:
        scores = (
            self.reliability, self.original_reporting_score, self.analysis_score,
            self.breaking_news_score, self.prospect_score, self.g5_score,
        )
        if any(score < 1 or score > 5 for score in scores):
            raise ValueError("source scores must be between 1 and 5")


@dataclass(frozen=True, slots=True)
class LeagueSourceProfile:
    """One source's league-specific editorial scope and routing metadata."""

    source: SourceProfile
    league: str
    scope: str
    conference: str | None = None
    division: str | None = None
    team: str | None = None
    account_type: str = ""
    directory_priority: int = 3
    profile_url: str = ""
    notes: str = ""
    verification: str = ""
    # (display tag, tag family, destination section)
    tags: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    requested_handle: str
    did: str | None
    current_handle: str | None
    display_name: str | None
    status: str
    description: str = ""
    #: True when retrying cannot help: the platform answered, and its answer
    #: was that this handle does not exist. A renamed or deleted account fails
    #: on every run forever, which is a different thing from the API being
    #: unreachable, and only one of the two is worth waking someone for.
    permanent: bool = False


@dataclass(frozen=True, slots=True)
class SourceEntityProfile:
    name: str
    organization: str | None
    entity_type: str
    source_classes: tuple[str, ...]
    specialties: tuple[str, ...] = field(default_factory=tuple)
    conferences: tuple[str, ...] = field(default_factory=tuple)
    teams: tuple[str, ...] = field(default_factory=tuple)
    reliability_score: int = 3
    reporting_score: int = 1
    team_access_score: int = 1
    national_score: int = 1
    analytics_score: int = 1
    scheme_score: int = 1
    recruiting_score: int = 1
    transfer_score: int = 1
    draft_score: int = 1
    awards_score: int = 1
    g5_score: int = 1
    breaking_score: int = 1
    official_score: int = 0
    priority: int = 3
    trust_status: str = "TRUSTED_SEED"
    active: bool = True
    entity_key: str | None = None

    def __post_init__(self) -> None:
        scores = (
            self.reliability_score, self.reporting_score, self.team_access_score,
            self.national_score, self.analytics_score, self.scheme_score,
            self.recruiting_score, self.transfer_score, self.draft_score,
            self.awards_score, self.g5_score, self.breaking_score, self.official_score,
        )
        if any(score < 0 or score > 5 for score in scores):
            raise ValueError("source entity scores must be between 0 and 5")


@dataclass(frozen=True, slots=True)
class SourceEndpointProfile:
    platform: str
    endpoint_type: str
    handle: str | None = None
    platform_id: str | None = None
    url: str | None = None
    endpoint_key: str | None = None
    verification_status: str = "seeded_unverified"
    active: bool = True


@dataclass(frozen=True, slots=True)
class EndpointResolution:
    endpoint_key: str
    status: str
    platform_id: str | None = None
    resolved_url: str | None = None
    display_name: str | None = None
    description: str = ""
    activity_score: float | None = None
