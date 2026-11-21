"""Staged research planner, independent budget allocator, corroboration engine, and Section 10.1 metrics."""

from __future__ import annotations

import hashlib
import inspect
import re
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from app.services.discovery.base import DiscoveredCandidate, SearchProvider, SearchProviderOutcome
from app.services.discovery.career_page import (
    classify_publisher_and_platform,
    extract_structured_application_evidence,
    fetch_and_parse_career_page,
)
from app.services.verification import FREE_MAIL_DOMAINS, is_demo_email_or_domain, verify_discovered_candidate

REQ_ID_PATTERNS = (
    re.compile(r"(?:\breq(?:uisition)?[\s#:_-]*id|\bjob[\s#:_-]*id|\bref(?:erence)?[\s#:_-]*(?:no|number|code)?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-_]{2,24})", re.IGNORECASE),
    re.compile(r"\b(REQ[-_]?\d{3,10})\b", re.IGNORECASE),
)

OFFICIAL_CAREER_SUBDOMAIN_PREFIXES = ("careers.", "jobs.", "talent.", "hr.", "apply.")


@dataclass
class StagedBudgetConfig:
    max_search_queries: int = 10
    max_fetched_pages: int = 36
    max_employer_followup_queries: int = 3
    max_email_corroboration_queries: int = 3
    per_domain_page_cap: int = 3
    deadline_seconds: int = 180
    max_batch_cost_usd: float = 1.50
    target_goal_count: int = 10

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StagedBudgetUsage:
    search_queries_executed: int = 0
    pages_fetched: int = 0
    pages_skipped_domain_cap: int = 0
    pages_blocked_robots: int = 0
    pages_blocked_http: int = 0
    pages_inaccessible: int = 0
    followup_queries_executed: int = 0
    corroboration_queries_executed: int = 0
    deadline_hit: bool = False
    spend_cap_hit: bool = False
    estimated_cost_usd: float = 0.0
    domain_fetch_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_staged_budget(
    pref,
    settings,
    *,
    max_search_queries_override: int | None = None,
    target_goal_override: int | None = None,
    budget_override: dict[str, Any] | None = None,
) -> StagedBudgetConfig:
    """Resolve independent stage budgets so search noise cannot starve employer verification."""
    cfg_raw: dict[str, Any] = {}
    if isinstance(getattr(pref, "budget_config_json", None), dict):
        cfg_raw.update(pref.budget_config_json)
    if isinstance(budget_override, dict):
        cfg_raw.update(budget_override)

    max_q = int(
        max_search_queries_override
        or cfg_raw.get("max_search_queries")
        or getattr(pref, "max_search_queries_per_batch", 10)
        or 10
    )
    max_q = max(1, min(max_q, 30))

    max_fetch = int(cfg_raw.get("max_fetched_pages") or max(48, max_q * 6))
    max_followup = int(cfg_raw["max_employer_followup_queries"]) if "max_employer_followup_queries" in cfg_raw else 3
    max_corrob = int(cfg_raw["max_email_corroboration_queries"]) if "max_email_corroboration_queries" in cfg_raw else 3
    domain_cap = int(cfg_raw.get("per_domain_page_cap") or 3)
    deadline = int(cfg_raw.get("deadline_seconds") or 180)
    goal = int(target_goal_override or getattr(pref, "target_goal_count", None) or getattr(pref, "batch_size", 10) or 10)
    cap_usd = min(float(getattr(pref, "max_batch_cost_usd", 1.0) or 1.0), float(settings.max_batch_search_cost_usd))

    return StagedBudgetConfig(
        max_search_queries=max_q,
        max_fetched_pages=max(1, min(max_fetch, 120)),
        max_employer_followup_queries=max(0, min(max_followup, 15)),
        max_email_corroboration_queries=max(0, min(max_corrob, 15)),
        per_domain_page_cap=max(1, min(domain_cap, 20)),
        deadline_seconds=max(15, min(deadline, 600)),
        max_batch_cost_usd=max(0.05, cap_usd),
        target_goal_count=max(1, min(goal, 50)),
    )


def extract_requisition_id(*, title: str = "", source_url: str = "", jd_text: str = "") -> str:
    blob = f"{title}\n{source_url}\n{jd_text[:2500]}"
    for pat in REQ_ID_PATTERNS:
        m = pat.search(blob)
        if m:
            return m.group(1).strip().upper()[:64]
    return ""


def compute_normalized_vacancy_key(
    *,
    employer: str,
    role: str,
    country_code: str,
    city: str,
    requisition_id: str = "",
) -> str:
    emp_clean = re.sub(r"[^a-z0-9\u0600-\u06FF]+", "", (employer or "").lower())
    if emp_clean and requisition_id:
        raw = f"{emp_clean}|REQ:{requisition_id.upper()}|{(country_code or '').upper()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
    cleaned_role_txt = re.sub(r"[\[(][^\]\)]*(?:req|ref|job)[^\]\)]*[\])]", " ", role or "", flags=re.IGNORECASE)
    role_clean = re.sub(r"[^a-z0-9\u0600-\u06FF]+", "", cleaned_role_txt.lower())
    loc_clean = f"{(country_code or '').upper()}:{(city or '').strip().lower()}"
    raw = f"{emp_clean}|{role_clean}|{loc_clean}|"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _host_matches_domain(host: str, domain: str) -> bool:
    h = (host or "").lower().removeprefix("www.")
    d = (domain or "").lower().removeprefix("www.")
    if not h or not d:
        return False
    return h == d or h.endswith("." + d) or d.endswith("." + h)


def classify_source_verification_tier(
    candidate: DiscoveredCandidate,
    *,
    discovery_category: str = "",
    verification_status: str = "",
    corroboration_url: str = "",
    corroborated_official: bool = False,
) -> tuple[str, str, list[str]]:
    """
    Classify verified/skipped candidate into Section 5.3 trust tiers:
    - tier_1_official: Official employer site / verified domain match with direct email invite
    - tier_2_authorized: Authorized recruiter or Stage 3/4 corroborated posting
    - tier_3_third_party_claim: Social post / aggregator claiming email without official site corroboration (review only!)
    - tier_4_non_email_or_closed: Portal-only, closed, informational, or incomplete evidence
    - tier_5_unrelated_or_prohibited: Scam, demo out-of-sandbox, invalid domain, or noreply
    Returns (tier, corroboration_status, review_reasons).
    """
    review_reasons: list[str] = []
    if discovery_category in ("website_application_only", "informational_post", "incomplete_evidence"):
        return "tier_4_non_email_or_closed", "uncorroborated", []
    if discovery_category == "rejected" or verification_status in ("suspicious", "closed"):
        return "tier_5_unrelated_or_prohibited", "uncorroborated", []
    if verification_status != "verified" or not candidate.application_email:
        if discovery_category == "demo_fixture":
            return "tier_1_official" if candidate.application_email else "tier_4_non_email_or_closed", "uncorroborated", []
        return "tier_4_non_email_or_closed", "uncorroborated", []

    host = (urlparse(candidate.source_url or "").hostname or candidate.company_domain or "").lower()
    email_domain = candidate.application_email.split("@", 1)[1].lower() if "@" in candidate.application_email else ""
    _, platform = classify_publisher_and_platform(candidate.source_url)
    is_social = platform == "social_platform"
    is_freemail = email_domain in FREE_MAIL_DOMAINS
    is_pasted = candidate.source_provider == "user_pasted"

    if corroborated_official and corroboration_url:
        return "tier_2_authorized", "corroborated_authorized", []

    if is_freemail:
        review_reasons.append(f"Recipient domain '{email_domain}' is a free-mail provider; verify employer authorization")
        return "tier_3_third_party_claim", "third_party_claim_only", review_reasons

    # Official company site where host domain matches corporate recipient email domain (or explicit user-pasted corporate domain)
    fetched_primary = candidate.source_provider == "career_page" or candidate.access_status == "fetched_200"
    if (platform == "company_site" and fetched_primary and not is_pasted and not candidate.page_inaccessible_reason
            and _host_matches_domain(host, email_domain)):
        return "tier_1_official", "corroborated_official", []

    if is_social:
        review_reasons.append(f"Social post on '{host}' claiming '{candidate.application_email}' lacks official site corroboration")
    elif email_domain and host and not _host_matches_domain(host, email_domain):
        review_reasons.append(
            f"Third-party host '{host}' claims email domain '{email_domain}' without official-domain corroboration"
        )
    else:
        review_reasons.append("Third-party email application claim awaits official domain corroboration")

    return "tier_3_third_party_claim", "third_party_claim_only", review_reasons


_STAGE34_OFF_DOMAIN_TOKENS = {
    "accountant", "accounting", "bookkeeper", "tax", "auditor", "nurse", "clinical",
    "pharmacist", "doctor", "dentist", "chef", "cook", "baker", "driver", "chauffeur",
    "housekeeping", "cleaner", "receptionist", "teacher", "sales", "cashier",
    "carpenter", "mason", "plumber", "electrician", "mechanic", "welder",
}


def _candidate_worth_followup_or_corrob(
    c: DiscoveredCandidate,
    *,
    freshness_days: int,
    role_terms: list[str],
    now_utc: datetime,
) -> bool:
    if getattr(c, "is_informational_post", False):
        return False
    if c.posted_at:
        p_dt = c.posted_at if c.posted_at.tzinfo else c.posted_at.replace(tzinfo=timezone.utc)
        if p_dt < now_utc - timedelta(days=max(freshness_days, 0)):
            return False
    role_str = (c.role_extracted or c.title or "").lower()
    role_blob = " ".join(role_terms or []).lower()
    role_words = set(re.findall(r"[a-z]{3,}", role_str))
    for bad in _STAGE34_OFF_DOMAIN_TOKENS:
        if bad in role_words and bad not in role_blob:
            return False
    return True


def serialize_candidate(candidate: DiscoveredCandidate) -> dict:
    data = asdict(candidate)
    for name in ("retrieved_at", "posted_at", "closing_at"):
        if data[name]:
            data[name] = data[name].isoformat()
    return data


def restore_candidate(data: dict) -> DiscoveredCandidate:
    values = dict(data)
    for name in ("retrieved_at", "posted_at", "closing_at"):
        if values.get(name):
            values[name] = datetime.fromisoformat(values[name])
    return DiscoveredCandidate(**values)


@dataclass(frozen=True)
class ResearchQuery:
    stage: str
    target_index: int
    text: str
    employer: str
    role: str
    country: str


def _plan_research_queries(
    candidates: list[DiscoveredCandidate], budget: StagedBudgetConfig, target_country_codes: set[str],
    role_terms: list[str], freshness_days: int,
) -> list[ResearchQuery]:
    followups = []
    corroborations = []
    seen_employers = set()
    now = datetime.now(timezone.utc)
    country = next(iter(target_country_codes)) if len(target_country_codes) == 1 else ""
    for index, target in enumerate(candidates):
        if is_demo_email_or_domain(target.application_email) or not _candidate_worth_followup_or_corrob(
            target, freshness_days=freshness_days, role_terms=role_terms, now_utc=now
        ):
            continue
        emp = (target.validated_employer or target.purported_employer or target.company_name or "").strip()
        raw_role = (target.role_extracted or target.title or (role_terms[0] if role_terms else "")).strip()
        eligible_employer = (
            3 <= len(emp) <= 65 and "#" not in emp
            and not re.search(r"^(?:urgent\s+hiring|available\s+immediately|we\s+are\s+hiring|hello|uae\b)", emp, re.I)
            and not re.search(r"[\U00010000-\U0010ffff]", emp)
            and not is_demo_email_or_domain(target.company_domain)
        )
        if (len(followups) < budget.max_employer_followup_queries and eligible_employer
                and emp.lower() not in seen_employers
                and (target.portal_apply_detected or not target.application_email or not target.evidence_quote)):
            seen_employers.add(emp.lower())
            role = "" if "#" in raw_role or len(raw_role) > 60 else raw_role[:50]
            role_clause = f'"{role}" ' if role else ""
            text = f'"{emp}" {role_clause}careers ("send your CV" OR "apply by email" OR "careers@" OR "recruitment@") {country}'.strip()
            followups.append(ResearchQuery("employer_followup", index, text, emp, role, country))

        if not target.application_email or len(corroborations) >= budget.max_email_corroboration_queries:
            continue
        tier, _, _ = classify_source_verification_tier(
            target, discovery_category="verified_email_vacancy", verification_status="verified"
        )
        if tier != "tier_3_third_party_claim":
            continue
        domain = target.application_email.split("@", 1)[-1].lower()
        role = (target.role_extracted or target.title or "").strip()[:50]
        if domain in FREE_MAIL_DOMAINS:
            text = f'"{emp}" "{target.application_email}" careers OR recruitment'
        else:
            text = f'site:{domain} ("{role}" OR careers OR recruitment OR "{target.application_email}")'
        corroborations.append(ResearchQuery("email_corroboration", index, text, emp, role, ""))
    return followups + corroborations


async def _research_search(provider: SearchProvider, query: ResearchQuery, freshness_days: int) -> SearchProviderOutcome:
    if query.stage == "email_corroboration":
        kwargs = {"freshness_days": 90, "max_results": 3, "country": ""}
    else:
        kwargs = {"freshness_days": freshness_days, "max_results": 4}
        params = inspect.signature(provider.search).parameters
        if "country" in params:
            kwargs["country"] = query.country
        if "exclude_domains" in params:
            kwargs["exclude_domains"] = ["linkedin.com", "facebook.com", "instagram.com", "tavily.com", "resumecreatorhub.com"]
    try:
        return await provider.search(query.text, **kwargs)
    except Exception as exc:
        return SearchProviderOutcome(provider.name, query.text, error=str(exc))


async def run_employer_followup_and_corroboration(
    *,
    candidates: list[DiscoveredCandidate],
    providers: list[SearchProvider],
    budget: StagedBudgetConfig,
    usage: StagedBudgetUsage,
    target_country_codes: set[str],
    role_terms: list[str],
    freshness_days: int,
    started_monotonic: float,
    transport=None,
    checkpoint: dict | None = None,
    save_checkpoint: Callable[[dict, str, float], None] | None = None,
    check_claim: Callable[[], None] | None = None,
) -> tuple[list[DiscoveredCandidate], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Save each paid response before fetching pages, then save its evaluated evidence and advance."""
    saved = deepcopy(checkpoint or {})
    if "plan" in saved:
        plan = [ResearchQuery(**q) for q in saved["plan"]]
    else:
        plan = _plan_research_queries(candidates, budget, target_country_codes, role_terms, freshness_days)
    extra_candidates = [restore_candidate(c) for c in saved.get("extra_candidates", [])]
    corrob_log = saved.get("corroborations", [])
    extra_sightings = saved.get("sightings", [])
    extra_errors = saved.get("errors", [])
    seen_urls = set(saved.get("seen_urls", [])) | {c.canonical_url or c.source_url for c in candidates if c.source_url}
    now_utc = datetime.now(timezone.utc)
    pending = saved.get("pending")
    cursor = saved.get("cursor", 0)

    def persist(provider_name: str = "", cost: float = 0.0) -> None:
        if save_checkpoint:
            save_checkpoint({
                "plan": [asdict(q) for q in plan], "cursor": cursor, "pending": pending,
                "extra_candidates": [serialize_candidate(c) for c in extra_candidates],
                "corroborations": list(corrob_log), "sightings": list(extra_sightings),
                "errors": list(extra_errors), "seen_urls": sorted(seen_urls),
            }, provider_name, cost)

    if not saved:
        persist()
    while cursor < len(plan):
        query = plan[cursor]
        if check_claim:
            check_claim()
        if pending and pending.get("awaiting_response"):
            # The old lease holder may already have incurred a charge. The provider has
            # no idempotency/receipt lookup, so retain the uncertainty and skip this call.
            extra_errors.append({
                "provider": pending["provider"], "stage": query.stage, "query": query.text,
                "code": "RESEARCH_RESPONSE_UNCERTAIN", "cost_status": "unknown",
                "error": "Worker interrupted before the provider response was recorded",
            })
            if query.stage == "employer_followup":
                usage.followup_queries_executed += 1
            else:
                usage.corroboration_queries_executed += 1
            pending = None
            cursor += 1
            persist()
            continue
        if pending is None:
            if time.monotonic() - started_monotonic >= budget.deadline_seconds:
                usage.deadline_hit = True
                break
            if usage.estimated_cost_usd >= budget.max_batch_cost_usd:
                usage.spend_cap_hit = True
                break
            if not providers:
                break
            stage_exhausted = (usage.followup_queries_executed >= budget.max_employer_followup_queries
                               if query.stage == "employer_followup"
                               else usage.corroboration_queries_executed >= budget.max_email_corroboration_queries)
            if stage_exhausted:
                cursor += 1
                continue
            pending = {"provider": providers[0].name, "awaiting_response": True}
            persist()
            result = await _research_search(providers[0], query, freshness_days)
            if query.stage == "employer_followup":
                usage.followup_queries_executed += 1
            else:
                usage.corroboration_queries_executed += 1
            cost = float(result.estimated_cost_usd or 0.0)
            usage.estimated_cost_usd = round(usage.estimated_cost_usd + cost, 6)
            if result.error:
                extra_errors.append({"provider": providers[0].name, "stage": query.stage,
                                     "query": query.text, "error": result.error})
            pending = {"provider": providers[0].name, "hits": [serialize_candidate(c) for c in result.candidates[:2]]}
            persist(providers[0].name, cost)

        hits = [restore_candidate(c) for c in pending["hits"]]
        provider_name = pending["provider"]
        if query.stage == "employer_followup":
            for hit in hits:
                if hit.source_url in seen_urls or usage.pages_fetched >= budget.max_fetched_pages:
                    continue
                seen_urls.add(hit.source_url)
                fetched, _ = await fetch_and_parse_career_page(
                    hit.source_url,
                    existing_snippet=hit.jd_text,
                    seed_title=hit.title,
                    seed_location=hit.location_raw,
                    seed_location_confidence=hit.location_confidence,
                    seed_posted_at=hit.posted_at,
                    seed_date_source=hit.date_source,
                    seed_search_query=query.text,
                    target_role_terms=role_terms,
                    transport=transport,
                )
                usage.pages_fetched += 1
                if not fetched or fetched.page_inaccessible_reason:
                    usage.pages_inaccessible += 1
                cand_add = fetched or hit
                if fetched is None:
                    ev = extract_structured_application_evidence(hit.jd_text, source_url=hit.source_url, target_role_terms=role_terms)
                    hit.application_email = ev.primary_email
                    hit.cc_emails = ev.cc_emails
                    hit.evidence_quote = ev.evidence_quote
                    hit.subject_instruction = ev.subject_instruction
                    hit.attachment_requirements = ev.attachment_requirements
                    hit.portal_apply_detected = ev.portal_apply_detected
                    cand_add = hit
                extra_sightings.append(
                    {
                        "source_provider": provider_name,
                        "search_query": query.text,
                        "source_url": cand_add.source_url,
                        "publisher": cand_add.publisher or urlparse(hit.source_url).hostname or "",
                        "raw_title": cand_add.title,
                        "raw_snippet": cand_add.jd_text,
                        "access_status": cand_add.access_status,
                        "research_stage": "employer_followup",
                        "corroborated": False,
                    }
                )
                if (cand_add.application_email and cand_add.evidence_quote
                        and _candidate_worth_followup_or_corrob(cand_add, freshness_days=freshness_days,
                                                               role_terms=role_terms, now_utc=now_utc)):
                    cand_add.raw_metadata = {**cand_add.raw_metadata, "discovered_via_stage": "employer_followup", "followup_employer": query.employer}
                    extra_candidates.append(cand_add)

        else:
            target = candidates[query.target_index]
            role = query.role
            email_dom = target.application_email.split("@", 1)[-1].lower()
            matched_url = ""
            matched_quote = ""

            for hit in hits:
                hit_host = (urlparse(hit.source_url or "").hostname or "").lower()
                if (email_dom in FREE_MAIL_DOMAINS or not _host_matches_domain(hit_host, email_dom)
                        or usage.pages_fetched >= budget.max_fetched_pages):
                    continue
                page, status = await fetch_and_parse_career_page(hit.source_url, target_role_terms=[role], transport=transport)
                usage.pages_fetched += 1
                if not page or status != "fetched":
                    usage.pages_inaccessible += 1
                    continue
                role_name = re.split(r"\s*[\[(|—]|\s+-\s+", role, maxsplit=1)[0].strip()
                source_host = urlparse(page.source_url).hostname or ""
                if (role_name and re.search(rf"(?<!\w){re.escape(role_name)}(?!\w)", page.role_extracted or page.title, re.I)
                        and _host_matches_domain(source_host, email_dom)
                        and classify_publisher_and_platform(page.source_url)[1] == "company_site"
                        and page.application_email.casefold() == target.application_email.casefold()
                        and _candidate_worth_followup_or_corrob(page, freshness_days=freshness_days, role_terms=[role], now_utc=now_utc)
                        and verify_discovered_candidate(page, include_standing_invitations=False, skip_network_dns=True).status == "verified"):
                    matched_url = page.source_url
                    matched_quote = page.evidence_quote
                    break

            extra_sightings.append(
                {
                    "source_provider": provider_name,
                    "search_query": query.text,
                    "source_url": matched_url or target.source_url,
                    "publisher": email_dom,
                    "raw_title": f"Corroboration check for {target.application_email}",
                    "raw_snippet": matched_quote,
                    "access_status": "corroborated" if matched_url else "uncorroborated",
                    "research_stage": "email_corroboration",
                    "corroborated": bool(matched_url),
                }
            )
            if matched_url:
                target.raw_metadata = {
                    **target.raw_metadata,
                    "corroborated_official": True,
                    "corroboration_url": matched_url,
                    "corroboration_quote": matched_quote,
                }
                corrob_log.append(
                    {
                        "target_url": target.source_url,
                        "corroboration_url": matched_url,
                        "email": target.application_email,
                        "status": "corroborated_authorized",
                    }
                )

        cursor += 1
        pending = None
        persist()

    return extra_candidates, corrob_log, extra_sightings, extra_errors


def build_section_10_metrics(
    *,
    search_hits: int,
    unique_urls: int,
    normalized_vacancies: int,
    usage: StagedBudgetUsage,
    budget: StagedBudgetConfig,
    verified_role_email: int,
    verified_general_cv: int,
    tier_counts: dict[str, int],
    category_counts: dict[str, int],
    skip_reason_counts: dict[str, int],
    eligible_count: int,
    eligible_for_live_send: int,
    needs_human_review: int,
    ready_for_approval: int,
    approved_for_send: int,
    drafted_count: int,
    sent_accepted: int,
    uncertain_count: int,
    failed_count: int,
    skipped_count: int,
    duplicates_count: int,
    demo_excluded: int,
) -> dict[str, Any]:
    """Build the complete Section 10.1 non-inflating discovery & application funnel dictionary."""
    goal_met = drafted_count >= budget.target_goal_count or sent_accepted >= budget.target_goal_count
    return {
        # Legacy compatibility counters
        "search_hits": search_hits,
        "unique_source_urls": unique_urls,
        "verified_role_email_invitations": verified_role_email,
        "verified_general_cv_invitations": verified_general_cv,
        "eligible_for_draft": eligible_count,
        "eligible_for_live_send": eligible_for_live_send,
        "drafted": drafted_count,
        "sent_accepted": sent_accepted,
        "failed_or_uncertain": uncertain_count + failed_count,
        "skipped": skipped_count,
        "duplicates": duplicates_count,
        "demo_excluded": demo_excluded,
        # Full Section 10.1 research & funnel telemetry
        "search_queries_executed": usage.search_queries_executed,
        "followup_queries_executed": usage.followup_queries_executed,
        "corroboration_queries_executed": usage.corroboration_queries_executed,
        "raw_urls_discovered": search_hits,
        "unique_canonical_urls": unique_urls,
        "pages_fetched": usage.pages_fetched,
        "pages_skipped_domain_cap": usage.pages_skipped_domain_cap,
        "pages_blocked_robots": usage.pages_blocked_robots,
        "pages_blocked_http": usage.pages_blocked_http,
        "pages_inaccessible": usage.pages_inaccessible,
        "normalized_vacancies": normalized_vacancies,
        "vacancies_tier_1_official": tier_counts.get("tier_1_official", 0),
        "vacancies_tier_2_authorized": tier_counts.get("tier_2_authorized", 0),
        "vacancies_tier_3_third_party_claim": tier_counts.get("tier_3_third_party_claim", 0),
        "website_portal_only": category_counts.get("website_application_only", 0),
        "informational_posts": category_counts.get("informational_post", 0),
        "incomplete_evidence": category_counts.get("incomplete_evidence", 0),
        "expired_or_closed": skip_reason_counts.get("EXPIRED_OR_STALE", 0),
        "duplicate_vacancies_skipped": duplicates_count,
        "eligible_hard_gates_passed": eligible_count,
        "needs_human_review": needs_human_review,
        "ready_for_approval": ready_for_approval,
        "approved_for_send": approved_for_send,
        "send_uncertain": uncertain_count,
        "failed": failed_count,
        "goal_target": budget.target_goal_count,
        "goal_met": goal_met,
        "deadline_hit": usage.deadline_hit,
        "spend_cap_hit": usage.spend_cap_hit,
    }
