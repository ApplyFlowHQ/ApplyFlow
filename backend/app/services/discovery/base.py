"""Discovery provider contracts and normalized raw candidate posting records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class DiscoveredCandidate:
    source_provider: str
    source_url: str
    source_type: str  # job_post | standing_invitation | user_pasted
    title: str
    company_name: str
    company_domain: str
    jd_text: str
    location_raw: str
    retrieved_at: datetime
    posted_at: datetime | None = None
    closing_at: datetime | None = None
    source_timezone: str = ""
    employment_type: str = "unknown"
    job_code: str = ""
    application_email: str = ""
    cc_emails: list[str] = field(default_factory=list)
    subject_instruction: str = ""
    attachment_requirements: list[str] = field(default_factory=list)
    evidence_quote: str = ""
    is_standing_invitation: bool = False
    weekly_hours: float | None = None
    qualifications: list[str] = field(default_factory=list)
    extraction_method: str = "deterministic"
    source_priority_tier: int = 5  # 1 (highest priority) .. 5 (generic portal)
    portal_apply_detected: bool = False
    rejected_emails: list[dict[str, str]] = field(default_factory=list)
    page_inaccessible_reason: str = ""
    is_multi_role_digest: bool = False
    is_informational_post: bool = False
    canonical_url: str = ""
    search_query: str = ""
    publisher: str = ""
    purported_employer: str = ""
    validated_employer: str = ""
    employer_confidence: str = "unknown"
    role_extracted: str = ""
    location_confidence: str = "unknown"
    date_source: str = "unknown"
    access_status: str = "unknown"
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchProviderOutcome:
    provider_name: str
    query: str
    candidates: list[DiscoveredCandidate] = field(default_factory=list)
    error: str = ""
    estimated_cost_usd: float = 0.0
    country_code: str = ""
    page: int = 0
    source_group: str = ""


class SearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        freshness_days: int = 14,
        max_results: int = 10,
        country: str = "",
        page: int = 0,
    ) -> SearchProviderOutcome:
        ...
