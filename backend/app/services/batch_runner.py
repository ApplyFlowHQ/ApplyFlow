"""Batch execution pipeline: Email-Application Discovery -> verification -> matching -> grounded drafting."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.metrics import metrics
from app.core.security import normalize_email_dedupe_key
from app.db.base import as_utc, utcnow
from app.db.models import (
    Application,
    Batch,
    CandidateProfile,
    CvDocument,
    CvExtractedFact,
    CvVersion,
    DiscoverySourceSighting,
    JobMatch,
    JobPosting,
    MailboxConnection,
    MessageEvent,
    SearchPreference,
    SupportingFile,
    UsageLedger,
    User,
)
from app.services.ai.base import LLMProvider
from app.services.ai.grounding import generate_grounded_application_draft
from app.services.approval import record_application_transition, sync_application_hash
from app.services.dedupe import find_existing_recipients
from app.services.discovery.base import DiscoveredCandidate, SearchProvider
from app.services.discovery.brave import BraveSearchProvider
from app.services.discovery.career_page import (
    PORTAL_DOMAINS,
    _is_platform_or_aggregator_name,
    classify_publisher_and_platform,
    classify_source_priority_tier,
    clean_and_extract_role_and_employer,
    detect_informational_post,
    extract_structured_application_evidence,
    fetch_and_parse_career_page,
    infer_access_status,
    split_clear_multi_role_post,
)
from app.services.discovery.fixtures_provider import get_isolated_demo_fixtures
from app.services.discovery.query_builder import (
    NON_CRAWLABLE_SOCIAL_DOMAINS,
    PORTAL_NOISE_DOMAINS,
    PlannedSearchQuery,
    build_email_application_discovery_plan,
    format_query_for_provider,
)
from app.services.discovery.source_directory import (
    get_recommended_domain_cap,
    is_non_vacancy_seo_or_index_url,
)
from app.services.discovery.tavily import TavilySearchProvider
from app.services.effective_config import resolve_effective_search_config
from app.services.eligibility import assess_posting_mandatory_eligibility, extract_mandatory_eligibility_requirements
from app.services.geo import parse_job_location, resolve_user_search_locations
from app.services.i18n import detect_text_language
from app.services.mailbox.gmail_sent import sync_gmail_sent_history
from app.services.matching import select_best_cv_for_posting
from app.services.money import parse_salary_from_text
from app.services.pdf_renderer import DOCX_MIME, ensure_cv_version_pdf_ready
from app.services.research_planner import (
    StagedBudgetUsage,
    build_section_10_metrics,
    classify_source_verification_tier,
    compute_normalized_vacancy_key,
    extract_requisition_id,
    resolve_staged_budget,
    restore_candidate,
    run_employer_followup_and_corroboration,
    serialize_candidate,
)
from app.services.storage import get_storage_adapter
from app.services.verification import is_demo_email_or_domain, verify_discovered_candidate

_TRACKING_QUERY_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "fb_action_", "igshid", "trk", "si", "ref", "feature", "mibextid")


def _hash_str(val: str) -> str:
    return hashlib.sha256((val or "").encode("utf-8")).hexdigest()


def _effective_requisition_id(posting: DiscoveredCandidate | JobPosting, *, split_role: bool) -> str:
    # A roundup's URL belongs to the parent page. Only the child's own fields can identify its requisition.
    return (posting.job_code or "").strip().upper()[:64] or extract_requisition_id(
        title=posting.title, source_url="" if split_role else posting.source_url, jd_text=posting.jd_text,
    )


def _vacancy_hashes(*, employer: str, role: str, country: str, city: str, req_id: str,
                    jd_text: str) -> tuple[str, str]:
    # An employer's requisition can be advertised in several cities or countries.
    # For unnumbered openings, location distinguishes otherwise identical postings.
    employer = (employer or "").strip().lower()
    role = (role or "").strip().lower()
    vacancy_key = compute_normalized_vacancy_key(
        employer=employer, role=role, country_code="" if req_id else country,
        city=city, requisition_id=req_id,
    ) if employer and (role or req_id) else ""
    content_hash = _hash_str("|".join((
        role, employer, country.upper(), city.strip().lower(),
        req_id, jd_text[:1000].strip().lower(),
    )))
    return vacancy_key, content_hash


def _canonicalize_url_for_hash(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = "https" if parsed.scheme in ("http", "https", "") else parsed.scheme.lower()
    netloc = (parsed.netloc or "").lower()
    if netloc.startswith("m.") or netloc.startswith("mobile."):
        netloc = netloc.split(".", 1)[1]
    path = (parsed.path or "").rstrip("/") or "/"
    kept_q = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if not any(k.lower().startswith(pref) for pref in _TRACKING_QUERY_PREFIXES)
    ]
    kept_q.sort(key=lambda kv: (kv[0].lower(), kv[1]))
    query = urlencode(kept_q, doseq=True)
    rebuilt = urlunparse((scheme, netloc, path, "", query, ""))
    return rebuilt.rstrip("/") if path != "/" else rebuilt


def _populate_candidate_provenance(cand: DiscoveredCandidate) -> None:
    """Populate normalized publisher, employer, role, and informational markers on a DiscoveredCandidate."""
    role_ext, val_emp, purp_emp, emp_conf, publisher = clean_and_extract_role_and_employer(
        cand.title,
        cand.jd_text,
        cand.source_url,
        jsonld_org=cand.validated_employer or "",
        og_site_name=cand.company_name or "",
    )
    if not val_emp and cand.company_name and not _is_platform_or_aggregator_name(cand.company_name):
        val_emp = cand.company_name.strip()[:180]
        purp_emp = purp_emp or val_emp
        emp_conf = "inferred" if emp_conf == "unknown" else emp_conf
    cand.publisher = cand.publisher or publisher
    cand.validated_employer = cand.validated_employer or val_emp
    cand.purported_employer = cand.purported_employer or purp_emp
    if emp_conf != "unknown":
        cand.employer_confidence = emp_conf
    cand.role_extracted = cand.role_extracted or role_ext
    cand.is_informational_post = bool(
        cand.is_informational_post
        or detect_informational_post(
            url=cand.source_url,
            title=cand.title,
            jd_text=cand.jd_text,
            has_email_invite=bool(cand.application_email),
            has_portal_apply=bool(cand.portal_apply_detected),
        )
    )


def _add_discovery_sighting(
    db: Session,
    *,
    user_id: str,
    batch_id: str,
    source_provider: str,
    search_query: str,
    source_url: str,
    canonical_url: str,
    canonical_url_hash: str,
    snippet_hash: str,
    source_domain: str = "",
    publisher: str = "",
    raw_title: str = "",
    raw_snippet: str = "",
    access_status: str = "unknown",
    disposition: str = "evaluated",
    is_duplicate: bool = False,
    dedupe_reason: str = "",
    is_demo: bool = False,
    job_posting_id: str | None = None,
    primary_skip_code: str = "",
    secondary_skip_codes: list[str] | None = None,
    research_stage: str = "discovery_search",
    corroborated: bool = False,
) -> DiscoverySourceSighting:
    domain = source_domain or (urlparse(canonical_url or source_url).hostname or "").lower()
    sighting = DiscoverySourceSighting(
        user_id=user_id,
        batch_id=batch_id,
        job_posting_id=job_posting_id,
        is_demo=is_demo,
        source_provider=source_provider[:64],
        search_query=search_query[:500],
        source_url=source_url,
        canonical_url=canonical_url,
        canonical_url_hash=canonical_url_hash,
        content_hash=snippet_hash[:64],
        source_domain=domain[:200],
        publisher=publisher[:120],
        title_raw=raw_title[:300],
        access_status=access_status[:48],
        disposition=disposition[:48],
        is_duplicate=is_duplicate,
        dedupe_reason=dedupe_reason[:120],
        primary_skip_code=primary_skip_code[:48],
        secondary_skip_codes_json=list(secondary_skip_codes or []),
        research_stage=research_stage[:40],
        corroborated=corroborated,
    )
    db.add(sighting)
    return sighting


def _application_with_fields(existing: Application | None, **values) -> Application:
    if existing is None:
        return Application(**values)
    for key, value in values.items():
        setattr(existing, key, value)
    return existing


def _add_skipped_application(
    db: Session,
    *,
    user_id: str,
    batch: Batch,
    posting: JobPosting,
    reason: str,
    primary_code: str,
    skip_codes: list[str],
    now: datetime,
    is_demo: bool,
    existing: Application | None = None,
) -> Application:
    secondary = [c for c in skip_codes if c and c != primary_code]
    app_rec = _application_with_fields(existing,
        user_id=user_id,
        batch_id=batch.id,
        job_posting_id=posting.id,
        is_demo=is_demo,
        pipeline_version=getattr(batch, "pipeline_version", "vNext") or "vNext",
        source_verification_tier=getattr(posting, "source_verification_tier", "tier_5_unrelated_or_prohibited") or "tier_5_unrelated_or_prohibited",
        approval_state="rejected",
        source_domain=posting.company_domain or "",
        company_name=posting.validated_employer or posting.company_name or "",
        role_title=posting.role_extracted or posting.title or "",
        country_code=posting.country_code,
        city=posting.city,
        recipient_email=posting.application_email,
        recipient_normalized=normalize_email_dedupe_key(posting.application_email) if posting.application_email else "",
        cc_emails_json=posting.cc_emails_json,
        transport_status="skipped",
        discovery_category=posting.discovery_category,
        primary_skip_code=primary_code,
        skip_codes_json=skip_codes,
        secondary_skip_codes_json=secondary,
        skip_or_error_reason=reason,
        discovered_at=now,
    )
    db.add(app_rec)
    return app_rec


def _user_monthly_cost(db: Session, user_id: str) -> float:
    start_month = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = db.scalar(
        select(func.sum(UsageLedger.estimated_cost_usd)).where(
            UsageLedger.user_id == user_id,
            UsageLedger.created_at >= start_month,
        )
    )
    return float(total or 0.0)


def _get_configured_search_providers(db: Session, user_id: str) -> list[SearchProvider]:
    eff = resolve_effective_search_config(db, user_id=user_id)
    providers: list[SearchProvider] = []
    if eff.provider in ("tavily", "both") and eff.tavily_api_key:
        providers.append(TavilySearchProvider(api_key=eff.tavily_api_key))
    if eff.provider in ("brave", "both") and eff.brave_api_key:
        providers.append(BraveSearchProvider(api_key=eff.brave_api_key))
    return providers


async def _call_provider_search(
    prov: SearchProvider,
    planned: PlannedSearchQuery,
    *,
    freshness_days: int,
    max_results: int,
    formatted_query: str,
):
    sig = inspect.signature(prov.search)
    kwargs: dict[str, object] = {
        "freshness_days": freshness_days,
        "max_results": max_results,
    }
    if "country" in sig.parameters:
        kwargs["country"] = planned.country_code
    if "country_name" in sig.parameters:
        kwargs["country_name"] = planned.country_name
    if "page" in sig.parameters:
        kwargs["page"] = planned.page
    if "source_group" in sig.parameters:
        kwargs["source_group"] = planned.source_group
    if "exclude_domains" in sig.parameters and planned.exclude_generic_portals:
        kwargs["exclude_domains"] = [*PORTAL_NOISE_DOMAINS, *NON_CRAWLABLE_SOCIAL_DOMAINS]
    if "include_domains" in sig.parameters and planned.source_group == "email_first_directory" and planned.preferred_domains:
        kwargs["include_domains"] = list(planned.preferred_domains)
    return await prov.search(formatted_query, **kwargs)


BATCH_LEASE = timedelta(minutes=6)


class BatchClaimLost(Exception):
    """The batch was cancelled, stopped, or claimed by another worker."""


def _load_active_cvs(db: Session, user: User):
    # Load active parsed CV versions, ensure DOCX->PDF readiness, & extract user-evidenced role titles
    storage_adapter = get_storage_adapter()
    cv_docs = db.scalars(
        select(CvDocument).where(CvDocument.user_id == user.id, CvDocument.is_archived.is_(False))
    ).all()
    cv_candidates: list[tuple[CvVersion, list[CvExtractedFact]]] = []
    cv_role_titles: list[str] = []
    for doc in cv_docs:
        if not doc.active_version_id:
            continue
        ver = db.scalar(select(CvVersion).where(CvVersion.id == doc.active_version_id, CvVersion.user_id == user.id, CvVersion.cv_document_id == doc.id))
        if ver and ver.parse_status == "parsed":
            if ver.mime_type == DOCX_MIME and ver.pdf_conversion_status != "ready":
                try:
                    ensure_cv_version_pdf_ready(ver, storage=storage_adapter)
                except Exception:
                    pass
            elif ver.mime_type == "application/pdf" and ver.pdf_conversion_status != "ready":
                ver.derived_pdf_storage_key = ver.storage_key
                ver.derived_pdf_sha256 = ver.sha256
                ver.pdf_conversion_status = "ready"
                ver.pdf_page_count = max(1, int(ver.page_count or 1))
            facts = list(db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == ver.id)).all())
            cv_candidates.append((ver, facts))
            for f in facts:
                if f.category == "role" and f.title:
                    t_clean = f.title.strip()
                    if 2 <= len(t_clean) <= 50 and len(t_clean.split()) <= 5 and t_clean not in cv_role_titles:
                        cv_role_titles.append(t_clean)
                elif f.category == "experience" and f.title and "|" in f.title:
                    prefix_role = f.title.split("|", 1)[0].strip(" •-\t")
                    if (
                        3 <= len(prefix_role) <= 45
                        and len(prefix_role.split()) <= 5
                        and not any(bad in prefix_role.lower() for bad in ("llc", "ltd", "inc", "university", "institute", "college", "b.a.", "b.sc.", "india", "dubai", "uae"))
                        and prefix_role not in cv_role_titles
                    ):
                        cv_role_titles.append(prefix_role)

    return cv_candidates, cv_role_titles


def _prepare_verified_application(
    db: Session, *, user: User, profile: CandidateProfile, pref: SearchPreference,
    batch: Batch, posting: JobPosting, sighting: DiscoverySourceSighting, cv_candidates,
    enabled_supporting_files, tier_review_reasons, now: datetime, llm_provider_override=None,
    existing: Application | None = None,
) -> Application:
    cand_is_demo = posting.is_demo
    verif_tier = posting.source_verification_tier
    # Check strict recipient-level deduplication (To + CCs + imported history) BEFORE drafting
    existing_map = (
        {}
        if cand_is_demo
        else find_existing_recipients(
            db,
            user_id=user.id,
            emails=[posting.application_email, *(posting.cc_emails_json or [])],
            exclude_application_id=existing.id if existing else None,
        )
    )
    def reject(reason: str, codes: list[str]) -> Application:
        all_codes = list(dict.fromkeys([*codes, *(posting.secondary_skip_codes_json or [])]))
        primary = all_codes[0]
        posting.discovery_category = "demo_fixture" if cand_is_demo else (
            "incomplete_evidence" if primary in ("JOB_LOCATION_UNKNOWN", "LOCATION_UNKNOWN") else "rejected"
        )
        posting.primary_skip_code = primary
        posting.skip_codes_json = all_codes
        posting.secondary_skip_codes_json = all_codes[1:]
        posting.skip_reason = reason
        sighting.disposition = "skipped"
        sighting.primary_skip_code = primary
        sighting.secondary_skip_codes_json = all_codes[1:]
        metrics.inc("jobs_skipped")
        return _add_skipped_application(
            db, user_id=user.id, batch=batch, posting=posting, reason=reason,
            primary_code=primary, skip_codes=all_codes, now=now, is_demo=cand_is_demo,
            existing=existing,
        )

    if existing_map:
        return reject(f"Recipient already in contact/suppression history ({', '.join(existing_map)})",
                      ["PREVIOUSLY_CONTACTED"])
    targets = {normalize_email_dedupe_key(addr) for addr in [posting.application_email, *(posting.cc_emails_json or [])] if addr}
    prepared = db.execute(select(Application.recipient_email, Application.cc_emails_json).where(
        Application.user_id == user.id, Application.batch_id == batch.id, Application.transport_status == "draft",
    ))
    for to_addr, cc in prepared:
        if any(normalize_email_dedupe_key(addr) in targets for addr in [to_addr, *(cc or [])] if addr):
            return reject("Another prepared application in this batch targets the same recipient.",
                          ["DUPLICATE_RECIPIENT_IN_BATCH"])

    _, prof_elig = assess_posting_mandatory_eligibility(posting=posting, profile=profile, check_cv_requirements=False)
    if not prof_elig.passed:
        return reject(f"Rejected before CV selection: {'; '.join(prof_elig.failure_messages())}",
                      list(prof_elig.failure_codes) or ["QUALIFICATION_MISMATCH"])
    formats = {req.lower() for req in (posting.attachment_requirements_json or [])}
    if "docx" in formats and "pdf" not in formats:
        return reject("Employer requires DOCX only; ApplyFlow sends the selected PDF.", ["ATTACHMENT_NOT_READY"])

    # Match against all active CV versions
    best_eval, all_evals = select_best_cv_for_posting(
        posting=posting,
        cv_candidates=cv_candidates,
        pref=pref,
        profile=profile,
        now_utc=now,
        has_supporting_attachments=bool(enabled_supporting_files),
    )
    for ev in all_evals:
        db.add(
            JobMatch(
                user_id=user.id,
                batch_id=batch.id,
                job_posting_id=posting.id,
                cv_version_id=ev.cv_version_id,
                hard_filter_passed=ev.hard_filter_passed,
                hard_filter_failures_json=ev.hard_filter_failures,
                failure_codes_json=ev.failure_codes,
                eligibility_check_json=ev.eligibility_check,
                fit_score=ev.fit_score,
                score_components_json=ev.score_components,
                supporting_facts_json=ev.supporting_facts,
                missing_requirements_json=ev.missing_requirements,
                decision_reason=ev.decision_reason,
                why_this_cv=ev.why_this_cv,
                verified_skills_json=ev.verified_skills,
                required_gaps_json=ev.required_gaps,
                attachment_ready=ev.attachment_ready,
                is_selected_cv=bool(best_eval and ev.cv_version_id == best_eval.cv_version_id and ev.hard_filter_passed),
            )
        )
    db.flush()

    if not best_eval or not best_eval.hard_filter_passed:
        reason = best_eval.decision_reason if best_eval else "No eligible CV matched"
        raw_codes = list(best_eval.failure_codes) if best_eval and best_eval.failure_codes else ["QUALIFICATION_MISMATCH"]
        return reject(reason, raw_codes)

    selected_cv, selected_facts = next(c for c in cv_candidates if c[0].id == best_eval.cv_version_id)
    if selected_cv.mime_type != "application/pdf" and not (
        selected_cv.pdf_conversion_status == "ready" and selected_cv.derived_pdf_sha256 and selected_cv.derived_pdf_storage_key
    ):
        return reject("The selected CV has no verified PDF attachment.", ["ATTACHMENT_NOT_READY"])
    selected_match = db.scalar(
        select(JobMatch).where(
            JobMatch.job_posting_id == posting.id,
            JobMatch.cv_version_id == selected_cv.id,
            JobMatch.batch_id == batch.id,
        )
    )

    # Grounded drafting
    supporting_ids = [sf["fact_id"] for sf in best_eval.supporting_facts if sf.get("fact_id")]
    settings = get_settings()
    config = batch.budget_config_json or {}
    batch_cap = min(pref.max_batch_cost_usd, settings.max_batch_search_cost_usd,
                    float(config.get("max_batch_cost_usd", pref.max_batch_cost_usd)))
    monthly_cap = min(pref.max_monthly_cost_usd, settings.max_monthly_user_cost_usd)
    allow_provider = (batch.mode != "demo_fixture" and batch.estimated_cost_usd < batch_cap
                      and _user_monthly_cost(db, user.id) < monthly_cap)
    if allow_provider and config.get("max_ai_calls") is not None:
        previous_calls = db.scalar(select(func.count(UsageLedger.id)).where(
            UsageLedger.user_id == user.id, UsageLedger.batch_id == batch.id, UsageLedger.operation == "draft_email",
        )) or 0
        allow_provider = previous_calls < int(config["max_ai_calls"])
    draft = generate_grounded_application_draft(
        posting=posting,
        profile=profile,
        pref=pref,
        facts=selected_facts,
        supporting_fact_ids=supporting_ids,
        language=best_eval.application_language,
        llm_provider=llm_provider_override,
        allow_provider=allow_provider,
        db=db,
        user_id=user.id,
    )
    if allow_provider and (draft.estimated_cost_usd > 0 or draft.rejection_reason or draft.provider_used != "template_fallback"):
        batch.estimated_cost_usd = round(batch.estimated_cost_usd + draft.estimated_cost_usd, 6)
        db.add(
            UsageLedger(
                user_id=user.id,
                batch_id=batch.id,
                provider=draft.provider_used,
                operation="draft_email",
                units=1,
                estimated_cost_usd=draft.estimated_cost_usd,
            )
        )

    attached_sf_ids: list[str] = []
    jd_lower = posting.jd_text.lower()
    for sf in enabled_supporting_files:
        requested_portfolio = "portfolio" in (posting.attachment_requirements_json or [])
        keyword_hit = any(kw.lower() in jd_lower for kw in (sf.keywords_json or []) if kw)
        if sf.attach_by_default or requested_portfolio or keyword_hit:
            attached_sf_ids.append(sf.id)

    batch.eligible_count += 1
    batch.drafted_count += 1
    metrics.inc("jobs_eligible")

    all_review_reasons: list[str] = []
    for rr in [*tier_review_reasons, *(best_eval.review_reasons or [])]:
        if rr and rr not in all_review_reasons:
            all_review_reasons.append(rr)
    if verif_tier == "tier_3_third_party_claim" and not all_review_reasons:
        all_review_reasons.append("Third-party email application claim lacks official site corroboration")

    attachment_sha = selected_cv.derived_pdf_sha256 or selected_cv.sha256
    attachment_fname = (f"{selected_cv.original_filename.rsplit('.', 1)[0]}.pdf"
                        if selected_cv.mime_type == DOCX_MIME else selected_cv.original_filename)

    if all_review_reasons:
        initial_approval_state = "needs_human_review"
    else:
        initial_approval_state = "ready_for_approval"

    sighting.disposition = "drafted" if initial_approval_state != "needs_human_review" else "needs_review"
    previous_state = existing.approval_state if existing else ""
    app_rec = _application_with_fields(existing,
        primary_skip_code="", skip_codes_json=[], skip_or_error_reason="",
        approved_at=None, approved_content_hash="",
        user_id=user.id,
        batch_id=batch.id,
        job_posting_id=posting.id,
        job_match_id=selected_match.id if selected_match else None,
        selected_cv_version_id=selected_cv.id,
        frozen_cv_sha256=selected_cv.sha256,
        frozen_cv_filename=attachment_fname,
        attachment_pdf_sha256=attachment_sha,
        supporting_file_ids_json=attached_sf_ids,
        is_demo=cand_is_demo,
        pipeline_version=batch.pipeline_version or "vNext",
        source_verification_tier=verif_tier,
        approval_state=initial_approval_state,
        review_reasons_json=all_review_reasons,
        source_domain=posting.company_domain or "",
        company_name=posting.validated_employer or posting.company_name,
        role_title=posting.role_extracted or posting.title,
        country_code=posting.country_code,
        city=posting.city,
        recipient_email=posting.application_email,
        recipient_normalized=normalize_email_dedupe_key(posting.application_email),
        cc_emails_json=posting.cc_emails_json,
        subject=draft.subject,
        body_text=draft.body_text,
        language=draft.language,
        evidence_fact_ids_json=draft.evidence_fact_ids,
        llm_provider_used=draft.provider_used,
        llm_model_used=draft.model_used,
        transport_status="draft",
        discovery_category=posting.discovery_category,
        secondary_skip_codes_json=list(posting.secondary_skip_codes_json or []),
        discovered_at=now,
    )
    d_hash = sync_application_hash(app_rec)
    db.add(app_rec)
    db.flush()
    record_application_transition(
        db,
        user_id=user.id,
        application_id=app_rec.id,
        field_name="approval_state",
        from_state=previous_state,
        to_state=initial_approval_state,
        actor="system",
        reason="; ".join(all_review_reasons) if all_review_reasons else "Prepared for user review",
        metadata={"draft_content_hash": d_hash, "tier": verif_tier},
    )
    db.add(
        MessageEvent(
            user_id=user.id,
            application_id=app_rec.id,
            event_type="drafted",
            direction="internal",
            subject=draft.subject,
            snippet=draft.body_text[:400],
        )
    )

    return app_rec


def _goal_shortfall_reason(batch: Batch) -> str:
    if batch.mode == "demo_fixture" or batch.drafted_count >= batch.target_goal_count:
        return ""
    categories = batch.category_counts_json or {}
    labels = {
        "website_application_only": "portal-only",
        "informational_post": "informational",
        "incomplete_evidence": "with incomplete evidence",
        "rejected": "rejected",
        "demo_fixture": "demo",
    }
    groups = [f"{categories[key]} {label}" for key, label in labels.items() if categories.get(key)]
    reason = f"Target goal {batch.target_goal_count}: drafted {batch.drafted_count}."
    if groups:
        reason += f" Posting outcomes: {', '.join(groups)}."
    else:
        reason += " Limited verifiable public email invitations in the search budget."
    if batch.duplicates_removed_count:
        noun = "sighting" if batch.duplicates_removed_count == 1 else "sightings"
        reason += f" Filtered {batch.duplicates_removed_count} duplicate {noun}."
    return reason


def execute_batch_pipeline(
    db: Session,
    *,
    batch: Batch,
    search_providers_override: list[SearchProvider] | None = None,
    candidate_overrides: list[DiscoveredCandidate] | None = None,
    llm_provider_override: LLMProvider | None = None,
    skip_network_dns: bool = False,
    auto_process_outbox_sync: bool = False,
    transport_override=None,
    max_search_queries_override: int | None = None,
    dedupe_against_history: bool | None = None,
    lease_token: str | None = None,
) -> Batch:
    """
    Execute Email Application Discovery, verification, matching, CV selection, and grounded drafting.
    Every mode ends at review. auto_process_outbox_sync and dedupe_against_history are
    accepted for legacy callers. Source deduplication is batch-local and recipient history is always checked.
    """
    settings = get_settings()
    now = utcnow()
    if batch.status not in ("queued", "running"):
        return batch
    checkpoint = dict(batch.checkpoint_json or {})
    if checkpoint.get("pasted_candidate"):
        candidate_overrides = [restore_candidate(checkpoint["pasted_candidate"])]
    max_search_queries_override = checkpoint.get("max_search_queries", max_search_queries_override)

    # Evaluate the stored mode when renewing a lease, including after a concurrent stop.
    stop_allows_mode = or_(User.automation_stopped_at.is_(None), Batch.mode.in_(("dry_run", "demo_fixture")))

    def check_claim() -> None:
        if lease_token and not db.scalar(select(Batch.id).join(User, User.id == Batch.user_id).where(
            Batch.id == batch.id, Batch.status == "running", Batch.lease_token == lease_token,
            User.deleted_at.is_(None), User.is_suspended.is_(False), stop_allows_mode,
        )):
            db.rollback()
            raise BatchClaimLost()

    def commit_stage(stage: str) -> None:
        if lease_token:
            guard = update(Batch).where(
                Batch.id == batch.id, Batch.status == "running", Batch.lease_token == lease_token,
            )
            if stage != "cancelled":
                guard = guard.where(select(User.id).where(
                    User.id == batch.user_id, stop_allows_mode,
                    User.deleted_at.is_(None), User.is_suspended.is_(False),
                ).exists())
            with db.no_autoflush:
                renewed = db.execute(guard.values(lease_expires_at=utcnow() + BATCH_LEASE)
                                     .execution_options(synchronize_session=False))
            if renewed.rowcount != 1:
                db.rollback()
                raise BatchClaimLost()
        batch.discovery_stage = stage
        if batch.status != "running":
            batch.consecutive_failures = 0
            batch.lease_token = None
            batch.lease_expires_at = None
        db.commit()

    user = db.scalar(select(User).where(User.id == batch.user_id))
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == batch.user_id))
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == batch.user_id))

    if not user or user.deleted_at or user.is_suspended or (
        user.automation_stopped_at and batch.mode not in ("dry_run", "demo_fixture")
    ):
        batch.status = "cancelled"
        batch.pause_reason = "User automation stopped or account unavailable"
        batch.finished_at = now
        commit_stage(batch.status)
        return batch

    if not pref or not profile:
        batch.status = "failed"
        batch.error_message = "Missing search preferences or candidate profile"
        batch.finished_at = now
        commit_stage(batch.status)
        return batch

    batch.status = "running"
    batch.started_at = batch.started_at or now
    batch.pipeline_version = batch.pipeline_version or "vNext"
    batch.strategy_used = "demo_fixture" if batch.mode == "demo_fixture" else "email_application_discovery"

    include_standing = (
        bool(batch.budget_config_json["include_standing_invitations"])
        if isinstance(getattr(batch, "budget_config_json", None), dict)
        and "include_standing_invitations" in batch.budget_config_json
        else bool(pref.include_standing_career_invitations)
    )

    if (
        batch.mode == "live"
        and candidate_overrides is None
        and search_providers_override is None
        and not checkpoint.get("plan")
        and not checkpoint.get("research_done")
        and settings.environment != "test"
    ):
        active_conn = db.scalar(
            select(MailboxConnection)
            .where(
                MailboxConnection.user_id == user.id,
                MailboxConnection.status == "connected",
                MailboxConnection.revoked_at.is_(None),
            )
            .order_by(MailboxConnection.is_primary.desc())
        )
        if active_conn and active_conn.provider in ("gmail_oauth", "smtp_imap"):
            try:
                with Session(db.get_bind()) as sync_db:
                    sync_conn = sync_db.get(MailboxConnection, active_conn.id)
                    sync_gmail_sent_history(sync_db, conn=sync_conn, max_pages=3, page_size=50)
            except Exception:
                pass

    budget = resolve_staged_budget(
        pref,
        settings,
        max_search_queries_override=max_search_queries_override,
        target_goal_override=getattr(batch, "target_goal_count", None) or None,
        budget_override=getattr(batch, "budget_config_json", None) or None,
    )
    usage = StagedBudgetUsage(**(batch.budget_usage_json or {}))
    started_monotonic = time.monotonic() - checkpoint.get("research_seconds", 0.0)
    stage_status: list[dict[str, object]] = [{"stage": "plan", "status": "completed", "budget": budget.to_dict()}]
    configured = {**(pref.budget_config_json or {}), **(batch.budget_config_json or {})}
    if configured.get("max_batch_cost_usd") is not None:
        budget.max_batch_cost_usd = min(budget.max_batch_cost_usd, max(0, float(configured["max_batch_cost_usd"])))
    merged_budget_cfg = budget.to_dict()
    if configured.get("max_ai_calls") is not None:
        merged_budget_cfg["max_ai_calls"] = max(0, int(configured["max_ai_calls"]))
    merged_budget_cfg["include_standing_invitations"] = include_standing
    batch.budget_config_json = merged_budget_cfg
    batch.target_goal_count = budget.target_goal_count
    if checkpoint.get("research_done"):
        commit_stage("eligibility_and_drafting")
    elif "research_progress" in checkpoint:
        commit_stage("employer_followup_and_corroboration")
    else:
        commit_stage("discovery_search")

    cv_candidates, cv_role_titles = _load_active_cvs(db, user)
    if not cv_candidates:
        batch.status = "failed"
        batch.error_message = "No parsed CV versions available in CV Container"
        batch.finished_at = utcnow()
        commit_stage(batch.status)
        return batch

    monthly_limit = min(pref.max_monthly_cost_usd, settings.max_monthly_user_cost_usd)
    if (candidate_overrides is None and not checkpoint.get("research_done")
            and usage.search_queries_executed == 0 and "initial_query" not in checkpoint and "research_progress" not in checkpoint
            and batch.mode != "demo_fixture"
            and _user_monthly_cost(db, user.id) >= monthly_limit):
        batch.status = "quota_paused"
        batch.pause_reason = f"Monthly estimated provider cost limit (${monthly_limit:.2f}) reached"
        batch.finished_at = utcnow()
        commit_stage(batch.status)
        return batch

    _, target_country_list = resolve_user_search_locations(
        target_locations=pref.target_locations_json or [],
        residence_country_code=profile.residence_country_code or "",
        residence_city=profile.residence_city or "",
        work_authorizations=profile.work_authorizations_json or [],
        relocation_targets=profile.relocation_targets_json or [],
        willing_to_relocate=profile.willing_to_relocate,
        allow_relocation=pref.allow_relocation,
        search_languages=pref.search_languages_json or ["en"],
    )
    target_country_codes = {c.upper() for c in target_country_list if c}

    role_terms_for_extraction = [
        *(pref.target_titles_json or []),
        *(profile.preferred_roles_json or []),
        *cv_role_titles,
        *(pref.role_families_json or []),
    ]

    # --- 1. Email Application Discovery ---
    candidates = [restore_candidate(row) for row in checkpoint.get("candidates", [])]
    sources_searched = list(batch.sources_searched_json or [])
    source_errors = list(batch.source_errors_json or [])
    search_dup_sightings = list(checkpoint.get("research_sightings", []))
    search_hits_count = checkpoint.get("search_hits", 0)
    unique_source_urls_set = set(checkpoint.get("unique_urls", []))
    duplicates_removed_total = batch.duplicates_removed_count

    def save_research(stage: str) -> None:
        checkpoint.update(
            candidates=[serialize_candidate(c) for c in candidates],
            candidates_found=len(candidates),
            research_sightings=list(search_dup_sightings),
            research_seconds=time.monotonic() - started_monotonic,
            search_hits=search_hits_count,
            unique_urls=sorted(unique_source_urls_set),
        )
        batch.checkpoint_json = dict(checkpoint)
        batch.sources_searched_json = list(sources_searched)
        batch.source_errors_json = list(source_errors)
        batch.duplicates_removed_count = duplicates_removed_total
        batch.budget_usage_json = usage.to_dict()
        batch.stage_status_json = list(stage_status)
        commit_stage(stage)

    if checkpoint.get("research_done"):
        stage_status = list(batch.stage_status_json)
    elif candidate_overrides is not None:
        candidates = list(candidate_overrides)
        search_hits_count = len(candidates)
        unique_source_urls_set = {_canonicalize_url_for_hash(c.source_url) for c in candidates if c.source_url}
        sources_searched.append({"provider": "injected_candidates", "query": "direct", "source_group": "direct", "results": len(candidates)})
    elif batch.mode == "demo_fixture":
        candidates = get_isolated_demo_fixtures()
        search_hits_count = len(candidates)
        unique_source_urls_set = {_canonicalize_url_for_hash(c.source_url) for c in candidates if c.source_url}
        sources_searched.append(
            {
                "provider": "demo_fixture",
                "query": "isolated_demo_fixtures",
                "source_group": "demo",
                "results": len(candidates),
            }
        )
        batch.coverage_note = "Isolated demo fixture mode — synthetic test postings only; zero live web search or email sending."
    else:
        providers = (
            search_providers_override
            if search_providers_override is not None
            else _get_configured_search_providers(db, user.id)
        )
        resuming_search = "initial_query" in checkpoint or "research_progress" in checkpoint or usage.search_queries_executed > 0
        if not providers and not resuming_search:
            batch.coverage_note = (
                "No search provider is currently enabled for this workspace. "
                "Configure a search provider in Settings/Setup, paste a public posting manually, or run isolated demo fixtures."
            )
            source_errors.append({"provider": "search", "code": "PROVIDER_ERROR", "error": "No search provider configured"})
        else:
            max_q = budget.max_search_queries
            query_plan = build_email_application_discovery_plan(
                excluded_keywords=pref.excluded_keywords_json or [],
                target_titles=pref.target_titles_json or profile.preferred_roles_json or [],
                role_families=pref.role_families_json or [],
                target_locations=pref.target_locations_json or [],
                search_languages=pref.search_languages_json or ["en"],
                include_standing_invitations=include_standing,
                cv_titles=cv_role_titles,
                industries=pref.industries_json or [],
                residence_country_code=profile.residence_country_code or "",
                residence_city=profile.residence_city or "",
                work_authorizations=profile.work_authorizations_json or [],
                relocation_targets=profile.relocation_targets_json or [],
                willing_to_relocate=profile.willing_to_relocate,
                allow_relocation=pref.allow_relocation,
                allowed_remote_modes=pref.allowed_remote_modes_json or [],
                rotation_offset=batch.query_rotation_offset,
                max_queries=max_q,
            )

            async def _run_searches() -> None:
                nonlocal duplicates_removed_total, search_hits_count
                remaining_monthly = max(0.0, monthly_limit - _user_monthly_cost(db, user.id))
                batch_cap = min(budget.max_batch_cost_usd, batch.estimated_cost_usd + remaining_monthly)
                seen_batch_urls = set(checkpoint.get("seen_batch_urls", []))
                active_plan = list(query_plan)
                if (
                    search_providers_override is None
                    and max_search_queries_override is None
                    and 0 < len(active_plan) < 8
                ):
                    extra_wave = build_email_application_discovery_plan(
                        excluded_keywords=pref.excluded_keywords_json or [],
                        target_titles=pref.target_titles_json or profile.preferred_roles_json or [],
                        role_families=pref.role_families_json or [],
                        target_locations=pref.target_locations_json or [],
                        search_languages=pref.search_languages_json or ["en"],
                        include_standing_invitations=include_standing,
                        cv_titles=cv_role_titles,
                        industries=pref.industries_json or [],
                        residence_country_code=profile.residence_country_code or "",
                        residence_city=profile.residence_city or "",
                        work_authorizations=profile.work_authorizations_json or [],
                        relocation_targets=profile.relocation_targets_json or [],
                        willing_to_relocate=profile.willing_to_relocate,
                        allow_relocation=pref.allow_relocation,
                        allowed_remote_modes=pref.allowed_remote_modes_json or [],
                        rotation_offset=batch.query_rotation_offset + 2,
                        max_queries=4,
                    )
                    seen_q_str = {q.query for q in active_plan}
                    for eq in extra_wave:
                        if eq.query not in seen_q_str and eq.source_group != "social_hiring_posts":
                            active_plan.append(eq)
                            seen_q_str.add(eq.query)

                if checkpoint.get("plan") is not None:
                    active_plan = [PlannedSearchQuery(**q) for q in checkpoint["plan"]]
                else:
                    active_plan = active_plan[:max_q]
                    checkpoint["plan"] = [asdict(q) for q in active_plan]
                    save_research("discovery_search")

                # Stage 1 & 2: Initial discovery search + primary fetch (reserve final 35s for Stage 3/4/5)
                provider_map = {getattr(p, "name", f"prov-{i}"): p for i, p in enumerate(providers)}
                stage1_cutoff = started_monotonic + max(25.0, float(budget.deadline_seconds) - 35.0)
                pending = checkpoint.get("initial_query")
                # Older checkpoints counted only fully processed queries. A paid response
                # now increments usage before fetching, so its own index takes precedence.
                first_index = pending["index"] if pending else usage.search_queries_executed
                for q_idx in range(first_index, len(active_plan)):
                    if "research_progress" in checkpoint:
                        break
                    planned = active_plan[q_idx]
                    pending = checkpoint.get("initial_query")
                    if pending and pending.get("awaiting_response"):
                        source_errors.append({
                            "stage": "discovery_search", "provider": pending["provider"], "query": pending["query"],
                            "code": "RESEARCH_RESPONSE_UNCERTAIN", "cost_status": "unknown",
                            "error": "Worker interrupted before the provider response was recorded",
                        })
                        usage.search_queries_executed += 1
                        del checkpoint["initial_query"]
                        save_research("discovery_search")
                        continue
                    if pending is None:
                        if not providers:
                            source_errors.append({"provider": "search", "code": "PROVIDER_ERROR",
                                                  "error": "No provider configured for the remaining discovery queries"})
                            break
                        if batch.estimated_cost_usd >= batch_cap:
                            usage.spend_cap_hit = True
                            source_errors.append({
                                "provider": providers[0].name, "code": "PROVIDER_ERROR",
                                "error": "Search spend cap reached; stopped additional queries",
                            })
                            break
                        now_mono = time.monotonic()
                        if now_mono >= started_monotonic + budget.deadline_seconds:
                            usage.deadline_hit = True
                            break
                        if now_mono >= stage1_cutoff:
                            break
                        prov = provider_map.get(planned.provider_hint) or providers[q_idx % len(providers)]
                        formatted_query = format_query_for_provider(planned, prov.name)
                        checkpoint["initial_query"] = {
                            "index": q_idx, "provider": prov.name, "query": formatted_query, "awaiting_response": True,
                        }
                        save_research("discovery_search")
                        out = await _call_provider_search(prov, planned, formatted_query=formatted_query,
                                                          freshness_days=pref.freshness_days, max_results=10)
                        usage.search_queries_executed += 1
                        batch.estimated_cost_usd = round(batch.estimated_cost_usd + out.estimated_cost_usd, 6)
                        usage.estimated_cost_usd = batch.estimated_cost_usd
                        if out.estimated_cost_usd > 0:
                            db.add(UsageLedger(user_id=user.id, batch_id=batch.id, provider=prov.name,
                                               operation="search_query", units=1, estimated_cost_usd=out.estimated_cost_usd))
                        if out.error:
                            source_errors.append({"provider": prov.name, "code": "PROVIDER_ERROR",
                                                  "query": out.query, "error": out.error})
                        pending = {
                            "index": q_idx, "provider": prov.name, "query": out.query, "provider_error": bool(out.error),
                            "hits": [serialize_candidate(hit) for hit in out.candidates],
                        }
                        checkpoint["initial_query"] = pending
                        save_research("discovery_search")

                    check_claim()
                    provider_name = pending["provider"]
                    query_text = pending["query"]
                    successful_fetches = 0
                    q_dups = 0
                    q_email_hits = 0
                    q_new_candidates: list[DiscoveredCandidate] = []

                    # Prioritize email-first directory & company sources before generic ATS listings
                    ordered_raw = sorted(
                        (restore_candidate(hit) for hit in pending["hits"]),
                        key=lambda c: classify_source_priority_tier(
                            c.source_url,
                            jd_text=c.jd_text,
                            has_email_invite=("@" in (c.jd_text or "")),
                        ),
                    )

                    pending_fetches: list[tuple[DiscoveredCandidate, str, bool, str]] = []
                    for cand in ordered_raw:
                        search_hits_count += 1
                        canon_url = _canonicalize_url_for_hash(cand.source_url)
                        cand.canonical_url = canon_url
                        cand.search_query = cand.search_query or query_text
                        if canon_url:
                            unique_source_urls_set.add(canon_url)
                        u_hash = _hash_str(canon_url)
                        snip_hash = _hash_str(cand.jd_text[:1000])
                        pub_name, _ = classify_publisher_and_platform(cand.source_url)
                        cand_host = (urlparse(cand.source_url or "").hostname or cand.company_domain or "").lower()

                        if u_hash in seen_batch_urls:
                            q_dups += 1
                            duplicates_removed_total += 1
                            search_dup_sightings.append(
                                {
                                    "source_provider": provider_name,
                                    "search_query": cand.search_query,
                                    "source_url": cand.source_url,
                                    "canonical_url": canon_url,
                                    "canonical_url_hash": u_hash,
                                    "snippet_hash": snip_hash,
                                    "publisher": pub_name,
                                    "raw_title": cand.title,
                                    "raw_snippet": cand.jd_text,
                                    "dedupe_reason": "duplicate_in_batch",
                                    "is_demo": is_demo_email_or_domain(cand.company_domain),
                                }
                            )
                            continue

                        seen_batch_urls.add(u_hash)
                        domain_count = usage.domain_fetch_counts.get(cand_host, 0)
                        is_official_host = not any(nd in cand_host for nd in PORTAL_NOISE_DOMAINS) and classify_source_priority_tier(cand.source_url) == 1
                        base_dom_cap = get_recommended_domain_cap(cand_host, budget.per_domain_page_cap)
                        dom_cap = base_dom_cap + (2 if is_official_host and base_dom_cap == budget.per_domain_page_cap else 0)
                        low_u = (cand.source_url or "").lower()
                        is_media_noise = any(m in low_u for m in ("tiktok.com/", "youtube.com/", "youtu.be/", "/reel/", "/videos/"))
                        is_seo_or_index = is_non_vacancy_seo_or_index_url(cand.source_url, cand.title)
                        is_ats_portal_no_email = ("@" not in (cand.jd_text or "")) and any(pd in cand_host or pd in low_u for pd in PORTAL_DOMAINS)
                        if is_media_noise:
                            can_fetch = False
                            skip_fetch_reason = "Source is non-vacancy video or media page"
                        elif is_seo_or_index:
                            can_fetch = False
                            skip_fetch_reason = "Source is non-vacancy guide, resume template, or index page"
                        elif is_ats_portal_no_email:
                            can_fetch = False
                            skip_fetch_reason = "Website/ATS portal without direct email invitation"
                        elif (
                            usage.pages_fetched < budget.max_fetched_pages
                            and domain_count < dom_cap
                            and time.monotonic() < stage1_cutoff
                        ):
                            can_fetch = True
                            skip_fetch_reason = ""
                            usage.pages_fetched += 1
                            usage.domain_fetch_counts[cand_host] = domain_count + 1
                        else:
                            can_fetch = False
                            skip_fetch_reason = "budget_domain_cap" if domain_count >= dom_cap else "budget_fetch_cap"
                            usage.pages_skipped_domain_cap += 1
                        pending_fetches.append((cand, canon_url, can_fetch, skip_fetch_reason))

                    sem = asyncio.Semaphore(6)

                    async def _fetch_slot(
                        item: tuple[DiscoveredCandidate, str, bool, str],
                        limiter: asyncio.Semaphore = sem,
                    ):
                        c_item, c_url, allow_fetch, skip_r = item
                        if not allow_fetch:
                            return c_item, c_url, None, skip_r
                        async with limiter:
                            fetched_res, f_status = await fetch_and_parse_career_page(
                                c_item.source_url,
                                existing_snippet=c_item.jd_text,
                                seed_title=c_item.title,
                                seed_location=c_item.location_raw,
                                seed_location_confidence=c_item.location_confidence,
                                seed_posted_at=c_item.posted_at,
                                seed_date_source=c_item.date_source or "unknown",
                                seed_search_query=c_item.search_query,
                                target_role_terms=role_terms_for_extraction,
                                transport=transport_override,
                            )
                        return c_item, c_url, fetched_res, f_status

                    fetch_outcomes = await asyncio.gather(*[_fetch_slot(pf) for pf in pending_fetches])
                    for (cand, canon_url, fetched, fetch_status), (_, _, attempted, _) in zip(fetch_outcomes, pending_fetches, strict=True):
                        if attempted and fetched is None:
                            reason = str(fetch_status).lower()
                            if "robots" in reason:
                                usage.pages_blocked_robots += 1
                            elif "403" in reason or "429" in reason:
                                usage.pages_blocked_http += 1
                            else:
                                usage.pages_inaccessible += 1
                        if fetched:
                            if fetched.page_inaccessible_reason:
                                rlow = fetched.page_inaccessible_reason.lower()
                                if "robots" in rlow:
                                    usage.pages_blocked_robots += 1
                                elif "403" in rlow or "429" in rlow:
                                    usage.pages_blocked_http += 1
                                else:
                                    usage.pages_inaccessible += 1
                            else:
                                successful_fetches += 1
                            if not fetched.title and cand.title:
                                fetched.title = cand.title
                            if not fetched.location_raw and cand.location_raw:
                                fetched.location_raw = cand.location_raw
                                fetched.location_confidence = cand.location_confidence
                            if not fetched.posted_at and cand.posted_at:
                                fetched.posted_at = cand.posted_at
                                fetched.date_source = cand.date_source or "unknown"
                            if cand.is_standing_invitation or cand.source_type == "standing_invitation":
                                fetched.is_standing_invitation = True
                                fetched.source_type = "standing_invitation"
                            fetched.canonical_url = canon_url
                            fetched.search_query = cand.search_query
                            fetched.raw_metadata = {**cand.raw_metadata, **fetched.raw_metadata, "fetch_status": fetch_status}
                            if fetched.application_email:
                                q_email_hits += 1
                            q_new_candidates.append(fetched)
                        else:
                            ev = extract_structured_application_evidence(
                                cand.jd_text,
                                source_url=cand.source_url,
                                target_role_terms=role_terms_for_extraction,
                            )
                            cand.application_email = ev.primary_email
                            cand.cc_emails = ev.cc_emails
                            cand.subject_instruction = ev.subject_instruction
                            cand.evidence_quote = ev.evidence_quote
                            cand.attachment_requirements = ev.attachment_requirements
                            cand.is_standing_invitation = bool(
                                cand.is_standing_invitation
                                or cand.source_type == "standing_invitation"
                                or ev.is_standing_invitation
                            )
                            cand.qualifications = ev.qualifications
                            cand.portal_apply_detected = ev.portal_apply_detected
                            cand.rejected_emails = ev.rejected_emails
                            cand.is_multi_role_digest = ev.is_multi_role_digest
                            cand.access_status = infer_access_status(inaccessible_reason=fetch_status)
                            _populate_candidate_provenance(cand)
                            cand.source_priority_tier = classify_source_priority_tier(
                                cand.source_url,
                                jd_text=cand.jd_text,
                                has_email_invite=bool(ev.primary_email),
                                is_standing=ev.is_standing_invitation,
                            )
                            cand.page_inaccessible_reason = fetch_status
                            cand.raw_metadata["fetch_note"] = fetch_status
                            if cand.application_email:
                                q_email_hits += 1
                            q_new_candidates.append(cand)

                    candidates.extend(q_new_candidates)
                    sources_searched.append(
                        {
                            "provider": provider_name,
                            "query": query_text,
                            "source_group": planned.source_group,
                            "country_code": planned.country_code,
                            "page": planned.page,
                            "results": len(ordered_raw),
                            "unique_added": len(q_new_candidates),
                            "successful_fetches": successful_fetches,
                            "provider_error": pending["provider_error"],
                            "email_invitations_found": q_email_hits,
                            "duplicates_removed": q_dups,
                        }
                    )

                    batch.consecutive_failures = 0
                    checkpoint["seen_batch_urls"] = sorted(seen_batch_urls)
                    del checkpoint["initial_query"]
                    save_research("discovery_search")

                primary_stage = next((row for row in batch.stage_status_json or []
                                      if row.get("stage") == "discovery_search" and row.get("status") == "completed"), None)
                stage_status.append(primary_stage or {
                    "stage": "discovery_search", "status": "completed",
                    "queries_executed": usage.search_queries_executed, "pages_fetched": usage.pages_fetched,
                })

                # Stage 3 & 4: Reserved independent budgets for Employer Follow-Up & Official Email Corroboration
                def save_query(progress: dict, provider: str, cost: float) -> None:
                    checkpoint["research_progress"] = progress
                    if cost > 0:
                        db.add(UsageLedger(user_id=user.id, batch_id=batch.id, provider=provider,
                                           operation="employer_research", units=1, estimated_cost_usd=cost))
                    batch.estimated_cost_usd = usage.estimated_cost_usd
                    batch.consecutive_failures = 0
                    save_research("employer_followup_and_corroboration")

                remaining_monthly = max(0.0, monthly_limit - _user_monthly_cost(db, user.id))
                research_budget = replace(budget, max_batch_cost_usd=min(
                    budget.max_batch_cost_usd, usage.estimated_cost_usd + remaining_monthly
                ))
                extra_cands, corrob_log, extra_sightings, extra_errors = await run_employer_followup_and_corroboration(
                    candidates=candidates,
                    providers=providers,
                    budget=research_budget,
                    usage=usage,
                    target_country_codes=target_country_codes,
                    role_terms=role_terms_for_extraction,
                    freshness_days=pref.freshness_days,
                    started_monotonic=started_monotonic,
                    transport=transport_override,
                    checkpoint=checkpoint.get("research_progress"),
                    save_checkpoint=save_query,
                    check_claim=check_claim,
                )
                checkpoint.pop("research_progress", None)
                if extra_errors:
                    source_errors.extend(extra_errors)
                for c_extra in extra_cands:
                    ec_url = _canonicalize_url_for_hash(c_extra.source_url)
                    c_extra.canonical_url = ec_url
                    if ec_url:
                        unique_source_urls_set.add(ec_url)
                    candidates.append(c_extra)
                for es in extra_sightings:
                    es_url = str(es.get("source_url", ""))
                    es_canon = _canonicalize_url_for_hash(es_url)
                    search_dup_sightings.append(
                        {
                            "source_provider": es.get("source_provider", ""),
                            "search_query": es.get("search_query", ""),
                            "source_url": es_url,
                            "canonical_url": es_canon,
                            "canonical_url_hash": _hash_str(es_canon),
                            "snippet_hash": _hash_str(str(es.get("raw_snippet", ""))[:1000]),
                            "publisher": es.get("publisher", ""),
                            "raw_title": es.get("raw_title", ""),
                            "raw_snippet": es.get("raw_snippet", ""),
                            "access_status": es.get("access_status", "ok"),
                            "disposition": es.get("access_status", "ok"),
                            "is_duplicate": False,
                            "dedupe_reason": "",
                            "research_stage": es.get("research_stage", "employer_followup"),
                            "corroborated": bool(es.get("corroborated")),
                            "is_demo": False,
                        }
                    )
                stage_status.append(
                    {
                        "stage": "employer_followup_and_corroboration",
                        "status": "completed",
                        "followup_queries": usage.followup_queries_executed,
                        "corroboration_queries": usage.corroboration_queries_executed,
                        "corroborations_confirmed": len(corrob_log),
                    }
                )

            asyncio.run(_run_searches())

    if not checkpoint.get("research_done"):
        # Persist search duplicate & Stage 3/4 research sightings in DiscoverySourceSighting
        for dup in search_dup_sightings:
            is_dup_flag = bool(dup.get("is_duplicate", True))
            _add_discovery_sighting(
                db,
                user_id=user.id,
                batch_id=batch.id,
                source_provider=str(dup.get("source_provider", "")),
                search_query=str(dup.get("search_query", "")),
                source_url=str(dup.get("source_url", "")),
                canonical_url=str(dup.get("canonical_url", "")),
                canonical_url_hash=str(dup.get("canonical_url_hash", "")),
                snippet_hash=str(dup.get("snippet_hash", "")),
                publisher=str(dup.get("publisher", "")),
                raw_title=str(dup.get("raw_title", "")),
                raw_snippet=str(dup.get("raw_snippet", "")),
                access_status=str(dup.get("access_status", "skipped_duplicate" if is_dup_flag else "ok")),
                disposition=str(dup.get("disposition", "duplicate" if is_dup_flag else "evaluated")),
                is_duplicate=is_dup_flag,
                dedupe_reason=str(dup.get("dedupe_reason", "duplicate" if is_dup_flag else "")),
                is_demo=bool(dup.get("is_demo", False)),
                primary_skip_code="DUPLICATE_SOURCE" if is_dup_flag else "",
                research_stage=str(dup.get("research_stage", "discovery_search")),
                corroborated=bool(dup.get("corroborated", False)),
            )

        # Expand clear structured multi-role posts ONLY when roles & instructions are unambiguous
        expanded_candidates: list[DiscoveredCandidate] = []
        for raw_cand in candidates:
            if batch.mode == "demo_fixture":
                expanded_candidates.append(raw_cand)
            else:
                expanded_candidates.extend(split_clear_multi_role_post(raw_cand))
        candidates = expanded_candidates

        # Prioritize explicit email-inviting & Tier 1-4 sources cleanly before Tier 5 generic ATS listings
        candidates.sort(
            key=lambda c: (
                0 if c.application_email else 1,
                int(getattr(c, "source_priority_tier", 5)),
                -(c.posted_at.timestamp() if c.posted_at else 0.0),
            )
        )

        batch.consecutive_failures = 0
        checkpoint["research_done"] = True
        search_dup_sightings.clear()
        save_research("eligibility_and_drafting")

    if not batch.coverage_note:
        successful_fetches = sum(int(q.get("successful_fetches", 0)) for q in sources_searched)
        completed_queries = sum(1 for q in sources_searched if q.get("provider_error") is False)
        covered = sorted({q["country_code"] for q in sources_searched if q.get("country_code")
                          and q.get("results") and not q.get("provider_error")})
        batch.coverage_note = (
            f"Discovery queries completed: {completed_queries}/{usage.search_queries_executed}; "
            f"search hits: {search_hits_count}; successful primary page fetches: {successful_fetches}; "
            f"robots/HTTP blocked: {usage.pages_blocked_robots + usage.pages_blocked_http}; "
            f"inaccessible: {usage.pages_inaccessible}; recorded errors: {len(source_errors)}. "
            f"Query regions returning hits: {', '.join(covered) or 'none'}."
        )
    batch.sources_searched_json = sources_searched
    batch.source_errors_json = source_errors
    batch.duplicates_removed_count = duplicates_removed_total

    # Restore within-batch deduplication state when resuming a checkpoint.
    seen_url_hashes = set(checkpoint.get("seen_url_hashes", []))
    seen_content_hashes = set(checkpoint.get("seen_content_hashes", []))
    seen_vacancy_keys = set(checkpoint.get("seen_vacancy_keys", []))
    if checkpoint.get("candidates_processed") and "vacancy_identity_version" not in checkpoint:
        # Upgrade only the committed postings in this unfinished batch. Previously
        # discarded sightings stay behind the cursor and never become new openings on retry.
        seen_content_hashes.clear()
        seen_vacancy_keys.clear()
        # Old real postings retained only validated/purported employer names. The
        # processed snapshot also retains a provider-supplied company name.
        original_companies = {(c.source_url, c.title, c.jd_text): c.company_name
                              for c in candidates[:checkpoint["candidates_processed"]]}
        for row in db.scalars(select(JobPosting).where(JobPosting.batch_id == batch.id, JobPosting.user_id == user.id)):
            split_role = bool((row.raw_metadata_json or {}).get("split_role_index"))
            row.requisition_id = _effective_requisition_id(row, split_role=split_role)
            row.normalized_vacancy_key, row.content_hash = _vacancy_hashes(
                employer=row.validated_employer or row.purported_employer or row.company_name
                or original_companies.get((row.source_url, row.title, row.jd_text), ""),
                role=row.role_extracted or row.title, country=row.country_code, city=row.city,
                req_id=row.requisition_id, jd_text=row.jd_text,
            )
            seen_content_hashes.add(row.content_hash)
            if row.normalized_vacancy_key:
                seen_vacancy_keys.add(row.normalized_vacancy_key)

    enabled_supporting_files = list(
        db.scalars(
            select(SupportingFile).where(
                SupportingFile.user_id == user.id,
                SupportingFile.enabled_for_matching.is_(True),
            )
        ).all()
    )

    category_counts: dict[str, int] = {
        "verified_email_vacancy": 0,
        "verified_general_cv_invitation": 0,
        "website_application_only": 0,
        "informational_post": 0,
        "incomplete_evidence": 0,
        "rejected": 0,
        "demo_fixture": 0,
    }
    category_counts.update(batch.category_counts_json or {})
    tier_counts = dict(checkpoint.get("tier_counts", {}))
    skip_reason_counts = dict(batch.skip_reason_counts_json or {})
    verified_role_email_count = checkpoint.get("verified_role_email_count", 0)
    verified_general_cv_count = checkpoint.get("verified_general_cv_count", 0)
    needs_human_review_count = checkpoint.get("needs_human_review_count", 0)
    ready_for_approval_count = checkpoint.get("ready_for_approval_count", 0)
    demo_excluded_count = checkpoint.get("demo_excluded_count", 0)

    def _record_diag(cat: str, codes: list[str]) -> None:
        if cat in category_counts:
            category_counts[cat] += 1
        for code in codes:
            if code:
                skip_reason_counts[code] = skip_reason_counts.get(code, 0) + 1

    # --- 2. Process each candidate independently so one bad listing never blocks valid ones ---
    def remaining_candidates():
        for idx in range(checkpoint.get("candidates_processed", 0), len(candidates)):
            if batch.eligible_count >= pref.batch_size:
                break
            check_claim()
            yield candidates[idx]
            batch.consecutive_failures = 0
            checkpoint.update(
                candidates_processed=idx + 1, vacancy_identity_version=2, seen_url_hashes=sorted(seen_url_hashes),
                seen_content_hashes=sorted(seen_content_hashes), seen_vacancy_keys=sorted(seen_vacancy_keys),
                tier_counts=dict(tier_counts), verified_role_email_count=verified_role_email_count,
                verified_general_cv_count=verified_general_cv_count, needs_human_review_count=needs_human_review_count,
                ready_for_approval_count=ready_for_approval_count, demo_excluded_count=demo_excluded_count,
            )
            batch.checkpoint_json = dict(checkpoint)
            batch.category_counts_json = dict(category_counts)
            batch.skip_reason_counts_json = dict(skip_reason_counts)
            commit_stage("eligibility_and_drafting")

    for cand in remaining_candidates():
        canon_url = cand.canonical_url or _canonicalize_url_for_hash(cand.source_url)
        split_idx = (cand.raw_metadata or {}).get("split_role_index")
        url_key = f"{canon_url}#role-{split_idx}" if split_idx else canon_url
        url_hash = _hash_str(url_key)
        snip_hash = _hash_str(cand.jd_text[:1000])

        # Populate normalized publisher/employer/role provenance if candidate came via candidate_overrides
        if not cand.role_extracted and not cand.validated_employer and cand.source_provider != "demo_fixture":
            _populate_candidate_provenance(cand)
        elif not cand.publisher:
            cand.publisher, _ = classify_publisher_and_platform(cand.source_url)

        cand_is_demo = bool(
            batch.mode == "demo_fixture"
            or cand.source_provider == "demo_fixture"
            or is_demo_email_or_domain(cand.application_email)
            or is_demo_email_or_domain(cand.company_domain)
        )
        parsed_loc = parse_job_location(cand.location_raw, cand.jd_text, title=cand.title, source_url=cand.source_url)
        req_id = _effective_requisition_id(cand, split_role=bool(split_idx))
        vac_key, content_hash = _vacancy_hashes(
            employer=cand.validated_employer or cand.purported_employer or cand.company_name,
            role=cand.role_extracted or cand.title, country=parsed_loc.country_code, city=parsed_loc.city,
            req_id=req_id, jd_text=cand.jd_text,
        )

        dup_reason = ""
        if url_hash in seen_url_hashes or content_hash in seen_content_hashes:
            dup_reason = "duplicate_in_batch"
        elif vac_key and vac_key in seen_vacancy_keys:
            dup_reason = "duplicate_normalized_vacancy"

        if dup_reason:
            duplicates_removed_total += 1
            batch.duplicates_removed_count = duplicates_removed_total
            _add_discovery_sighting(
                db,
                user_id=user.id,
                batch_id=batch.id,
                source_provider=cand.source_provider,
                search_query=cand.search_query,
                source_url=cand.source_url,
                canonical_url=canon_url,
                canonical_url_hash=url_hash,
                snippet_hash=snip_hash,
                publisher=cand.publisher,
                raw_title=cand.title,
                raw_snippet=cand.jd_text,
                access_status=cand.access_status or "unknown",
                disposition="duplicate",
                is_duplicate=True,
                dedupe_reason=dup_reason,
                is_demo=cand_is_demo,
                primary_skip_code="DUPLICATE_SOURCE",
            )
            continue

        seen_url_hashes.add(url_hash)
        seen_content_hashes.add(content_hash)
        if vac_key:
            seen_vacancy_keys.add(vac_key)

        batch.discovered_count += 1
        metrics.inc("jobs_discovered")

        # Extract salary, location confidence and language
        sal_min, sal_max, sal_curr, sal_period, sal_basis = parse_salary_from_text(cand.jd_text)
        loc_conf = (
            cand.location_confidence
            if cand.location_confidence in ("explicit", "inferred_text") and parsed_loc.country_code
            else parsed_loc.location_confidence
        )
        lang_code, _ = detect_text_language(f"{cand.title}\n{cand.jd_text}")

        # Verify source trust, explicit email invitation, DNS, scams, category, and diagnostic skip codes
        verif = verify_discovered_candidate(
            cand,
            include_standing_invitations=include_standing,
            now_utc=now,
            skip_network_dns=skip_network_dns or batch.mode == "demo_fixture",
            target_country_codes=target_country_codes if target_country_codes else None,
            parsed_country_code=parsed_loc.country_code,
            parsed_remote_scope=parsed_loc.remote_scope,
            location_confidence=loc_conf,
            is_demo=cand_is_demo,
            allow_demo_sandbox=(batch.mode == "demo_fixture"),
        )
        if verif.discovery_category == "demo_fixture" and batch.mode != "demo_fixture":
            demo_excluded_count += 1

        # Publication date unknown check when policy skips unknown dates
        if not cand.posted_at and pref.unknown_dates_policy == "skip" and "POSTING_DATE_UNVERIFIED" not in verif.skip_codes:
            verif.skip_codes.append("POSTING_DATE_UNVERIFIED")
            if verif.primary_skip_code and "POSTING_DATE_UNVERIFIED" != verif.primary_skip_code:
                verif.secondary_skip_codes.append("POSTING_DATE_UNVERIFIED")

        # An earlier sighting supplies provenance only. Evaluate the current page and recipients.
        first_seen = db.scalar(select(func.min(JobPosting.first_seen_at)).where(
            JobPosting.user_id == user.id, JobPosting.batch_id != batch.id,
            JobPosting.canonical_url_hash == url_hash, JobPosting.is_demo == cand_is_demo,
        )) or now
        elig_reqs_extracted = extract_mandatory_eligibility_requirements(
            title=cand.role_extracted or cand.title,
            jd_text=cand.jd_text,
            subject_instruction=cand.subject_instruction,
            evidence_quote=cand.evidence_quote,
            location_raw=cand.location_raw or parsed_loc.raw_text,
            country_code=parsed_loc.country_code,
            structured_data={"qualifications": cand.qualifications, **cand.raw_metadata},
        )
        elig_reqs_dict = elig_reqs_extracted.to_dict()

        clean_employer = cand.validated_employer or (cand.company_name if cand_is_demo else "")
        access_st = cand.access_status or ("ok" if not cand.page_inaccessible_reason else infer_access_status(inaccessible_reason=cand.page_inaccessible_reason))
        date_src = cand.date_source or "unknown"

        verif_tier, corrob_status, tier_review_reasons = classify_source_verification_tier(
            cand,
            discovery_category=verif.discovery_category,
            verification_status=verif.status,
            corroboration_url=str((cand.raw_metadata or {}).get("corroboration_url", "")),
            corroborated_official=bool((cand.raw_metadata or {}).get("corroborated_official", False)),
        )
        tier_counts[verif_tier] = tier_counts.get(verif_tier, 0) + 1
        corrob_url = str((cand.raw_metadata or {}).get("corroboration_url", ""))
        corrob_ev = (
            {"corroboration_quote": str((cand.raw_metadata or {}).get("corroboration_quote", ""))[:300]}
            if corrob_url
            else {}
        )

        evidence_payload = {
            "company_name": clean_employer,
            "purported_employer": cand.purported_employer,
            "publisher": cand.publisher,
            "source_domain": cand.company_domain,
            "job_title": cand.role_extracted or cand.title,
            "role_extracted": cand.role_extracted,
            "location": cand.location_raw or parsed_loc.raw_text,
            "country_code": parsed_loc.country_code,
            "location_confidence": loc_conf,
            "remote_scope": parsed_loc.remote_scope,
            "qualifications": cand.qualifications,
            "published_at": cand.posted_at.isoformat() if cand.posted_at else None,
            "date_source": date_src,
            "expires_at": cand.closing_at.isoformat() if cand.closing_at else None,
            "application_email": verif.normalized_to or cand.application_email,
            "required_subject": cand.subject_instruction,
            "cc_emails": verif.normalized_ccs,
            "invitation_quote": cand.evidence_quote,
            "source_url": cand.source_url,
            "canonical_url": canon_url,
            "search_query": cand.search_query,
            "extraction_method": cand.extraction_method,
            "rejected_emails": cand.rejected_emails,
            "page_inaccessible_reason": cand.page_inaccessible_reason,
            "access_status": access_st,
            "is_multi_role_digest": cand.is_multi_role_digest,
            "eligibility_requirements": elig_reqs_dict,
        }

        posting = JobPosting(
            user_id=user.id,
            batch_id=batch.id,
            is_demo=cand_is_demo,
            canonical_url_hash=url_hash,
            content_hash=content_hash,
            source_provider=cand.source_provider,
            source_url=cand.source_url,
            canonical_url=canon_url,
            search_query=cand.search_query,
            source_type=cand.source_type,
            publisher=cand.publisher,
            company_name=clean_employer,
            company_domain=cand.company_domain,
            purported_employer=cand.purported_employer,
            validated_employer=clean_employer,
            employer_confidence=cand.employer_confidence or ("verified" if clean_employer else "unknown"),
            title=cand.title,
            role_extracted=cand.role_extracted,
            job_code=cand.job_code,
            jd_text=cand.jd_text,
            detected_language=lang_code,
            location_raw=cand.location_raw or parsed_loc.raw_text,
            country_code=parsed_loc.country_code,
            city=parsed_loc.city,
            location_confidence=loc_conf,
            remote_scope=parsed_loc.remote_scope,
            location_ambiguous=parsed_loc.is_ambiguous,
            employment_type=cand.employment_type,
            salary_min=sal_min,
            salary_max=sal_max,
            salary_currency=sal_curr,
            salary_period=sal_period,
            salary_basis=sal_basis,
            weekly_hours=cand.weekly_hours,
            posted_at=cand.posted_at,
            date_source=date_src,
            closing_at=cand.closing_at,
            retrieved_at=cand.retrieved_at,
            first_seen_at=first_seen,
            source_timezone=cand.source_timezone,
            application_email=verif.normalized_to or cand.application_email,
            cc_emails_json=verif.normalized_ccs,
            subject_instruction=cand.subject_instruction,
            attachment_requirements_json=cand.attachment_requirements,
            evidence_quote=cand.evidence_quote,
            is_standing_invitation=cand.is_standing_invitation,
            discovery_category=verif.discovery_category,
            source_priority_tier=cand.source_priority_tier,
            access_status=access_st,
            parsing_version="v3.0",
            verification_status=verif.status,
            dns_check_status=verif.dns_status,
            primary_skip_code=verif.primary_skip_code,
            skip_codes_json=verif.skip_codes,
            secondary_skip_codes_json=verif.secondary_skip_codes,
            skip_reason=verif.skip_reason,
            extraction_confidence=0.95 if (cand.evidence_quote and parsed_loc.country_code) else (0.85 if cand.evidence_quote else 0.35),
            extraction_evidence_json=evidence_payload,
            eligibility_requirements_json=elig_reqs_dict,
            pipeline_version=batch.pipeline_version or "vNext",
            source_verification_tier=verif_tier,
            corroboration_status=corrob_status,
            corroboration_url=corrob_url,
            corroboration_evidence_json=corrob_ev,
            requisition_id=req_id,
            normalized_vacancy_key=vac_key,
            vacancy_confidence="high" if (clean_employer and (cand.role_extracted or cand.title) and verif_tier == "tier_1_official") else ("medium" if verif.status == "verified" else "low"),
            research_status="corroborated" if corrob_url else ("verified" if verif.status == "verified" else "skipped"),
            raw_metadata_json={"warnings": verif.warnings, "prompt_injection_detected": verif.prompt_injection_detected, **cand.raw_metadata},
        )
        db.add(posting)
        db.flush()

        sighting = _add_discovery_sighting(
            db,
            user_id=user.id,
            batch_id=batch.id,
            job_posting_id=posting.id,
            source_provider=cand.source_provider,
            search_query=cand.search_query,
            source_url=cand.source_url,
            canonical_url=canon_url,
            canonical_url_hash=url_hash,
            snippet_hash=snip_hash,
            publisher=cand.publisher,
            raw_title=cand.title,
            raw_snippet=cand.jd_text,
            access_status=access_st,
            disposition=verif.discovery_category if verif.status != "verified" else "evaluated",
            is_duplicate=False,
            is_demo=cand_is_demo,
            primary_skip_code=verif.primary_skip_code,
            secondary_skip_codes=verif.secondary_skip_codes,
        )

        # Unverified / empty-recipient search hits remain in Discovery & Evidence ONLY (never pollute Application table)
        if verif.status != "verified":
            _record_diag(posting.discovery_category, posting.skip_codes_json)
            batch.skipped_count += 1
            metrics.inc("jobs_skipped")
            continue

        if verif.discovery_category == "verified_general_cv_invitation":
            verified_general_cv_count += 1
        elif verif.discovery_category == "verified_email_vacancy":
            verified_role_email_count += 1

        app_rec = _prepare_verified_application(
            db, user=user, profile=profile, pref=pref, batch=batch, posting=posting, sighting=sighting,
            cv_candidates=cv_candidates, enabled_supporting_files=enabled_supporting_files,
            tier_review_reasons=tier_review_reasons, now=now, llm_provider_override=llm_provider_override,
        )
        _record_diag(posting.discovery_category, posting.skip_codes_json)
        if app_rec.transport_status == "skipped":
            batch.skipped_count += 1
        elif app_rec.approval_state == "needs_human_review":
            needs_human_review_count += 1
        else:
            ready_for_approval_count += 1

    stage_status.append(
        {
            "stage": "eligibility_and_drafting",
            "status": "completed",
            "eligible": batch.eligible_count,
            "drafted": batch.drafted_count,
            "needs_human_review": needs_human_review_count,
            "ready_for_approval": ready_for_approval_count,
        }
    )

    batch.category_counts_json = category_counts
    batch.skip_reason_counts_json = skip_reason_counts
    batch.stage_status_json = stage_status
    batch.budget_usage_json = usage.to_dict()
    batch.goal_shortfall_reason = _goal_shortfall_reason(batch)

    batch_metrics = build_section_10_metrics(
        search_hits=search_hits_count,
        unique_urls=len(unique_source_urls_set),
        normalized_vacancies=len(seen_vacancy_keys) or int(batch.discovered_count or 0),
        usage=usage,
        budget=budget,
        verified_role_email=verified_role_email_count,
        verified_general_cv=verified_general_cv_count,
        tier_counts=tier_counts,
        category_counts=category_counts,
        skip_reason_counts=skip_reason_counts,
        eligible_count=batch.eligible_count,
        eligible_for_live_send=0,
        needs_human_review=needs_human_review_count,
        ready_for_approval=ready_for_approval_count,
        approved_for_send=0,
        drafted_count=batch.drafted_count,
        sent_accepted=0,
        uncertain_count=batch.uncertain_count,
        failed_count=0,
        skipped_count=batch.skipped_count,
        duplicates_count=duplicates_removed_total,
        demo_excluded=demo_excluded_count,
    )
    duplicate_search_hits = sum(row.get("duplicates_removed", 0) for row in sources_searched)
    batch_metrics["duplicate_search_hits"] = duplicate_search_hits
    batch_metrics["duplicate_vacancies_skipped"] = duplicates_removed_total - duplicate_search_hits
    batch.metrics_json = batch_metrics
    if batch.status == "running":
        batch.status = "completed"
        batch.finished_at = utcnow()
    batch.checkpoint_json = {key: checkpoint.get(key, 0) for key in ("candidates_found", "candidates_processed")}
    batch.error_message = ""
    commit_stage(batch.status)
    db.refresh(batch)
    return batch


def reprepare_duplicate_application(db: Session, *, app_rec: Application) -> None:
    """Re-evaluate this skipped vacancy in the caller's override transaction."""
    batch = db.scalar(select(Batch).where(Batch.id == app_rec.batch_id, Batch.user_id == app_rec.user_id).with_for_update())
    db.refresh(app_rec, with_for_update=True)
    if app_rec.transport_status != "skipped" or app_rec.primary_skip_code != "PREVIOUSLY_CONTACTED":
        raise ValueError("Only a previously contacted, skipped application can be re-prepared")
    override = db.scalar(select(MessageEvent).where(
        MessageEvent.user_id == app_rec.user_id, MessageEvent.application_id == app_rec.id,
        MessageEvent.event_type == "different_vacancy_override",
    ).order_by(MessageEvent.created_at.desc()))
    details = override.details_json if override else {}
    if (details.get("posting_id") != app_rec.job_posting_id
            or details.get("recipient") != normalize_email_dedupe_key(app_rec.recipient_email)):
        raise ValueError("A different-vacancy override for this application and recipient is required")
    posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id,
                            JobPosting.user_id == app_rec.user_id, JobPosting.batch_id == app_rec.batch_id))
    user = db.get(User, app_rec.user_id)
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == app_rec.user_id))
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == app_rec.user_id))
    if (not batch or batch.status != "completed" or not user or user.deleted_at or user.is_suspended
            or user.automation_stopped_at or not pref or not profile):
        raise ValueError("Discovery must be complete with an active account and search profile")
    if (not posting or posting.is_demo or posting.verification_status != "verified"
            or posting.primary_skip_code != "PREVIOUSLY_CONTACTED"
            or normalize_email_dedupe_key(posting.application_email) != app_rec.recipient_normalized):
        raise ValueError("Verified evidence for this exact vacancy and recipient is required")
    sighting = db.scalar(select(DiscoverySourceSighting).where(
        DiscoverySourceSighting.user_id == user.id, DiscoverySourceSighting.batch_id == batch.id,
        DiscoverySourceSighting.job_posting_id == posting.id))
    if not sighting:
        raise ValueError("Discovery evidence is missing")
    # Reconstruct the stored evidence and run the same verification policy at today's date.
    from dataclasses import fields

    values = {f.name: getattr(posting, f.name) for f in fields(DiscoveredCandidate) if hasattr(posting, f.name)}
    evidence = posting.extraction_evidence_json or {}
    values.update(cc_emails=list(posting.cc_emails_json or []), attachment_requirements=list(posting.attachment_requirements_json or []),
                  raw_metadata=dict(posting.raw_metadata_json or {}), qualifications=evidence.get("qualifications", []),
                  page_inaccessible_reason=evidence.get("page_inaccessible_reason", ""),
                  is_multi_role_digest=evidence.get("is_multi_role_digest", False))
    for field in ("retrieved_at", "posted_at", "closing_at"):
        values[field] = as_utc(values[field])
    cand = DiscoveredCandidate(**values)
    _, countries = resolve_user_search_locations(
        target_locations=pref.target_locations_json or [], residence_country_code=profile.residence_country_code,
        residence_city=profile.residence_city, work_authorizations=profile.work_authorizations_json or [],
        relocation_targets=profile.relocation_targets_json or [], willing_to_relocate=profile.willing_to_relocate,
        allow_relocation=pref.allow_relocation, search_languages=pref.search_languages_json or ["en"],
    )
    now = utcnow()
    verified = verify_discovered_candidate(cand, include_standing_invitations=pref.include_standing_career_invitations,
                                          now_utc=now, skip_network_dns=get_settings().environment == "test",
                                          target_country_codes=set(countries) or None, parsed_country_code=posting.country_code,
                                          parsed_remote_scope=posting.remote_scope, location_confidence=posting.location_confidence)
    if verified.status != "verified":
        raise ValueError(verified.skip_reason or "This vacancy no longer passes source verification")
    tier, _, review = classify_source_verification_tier(
        cand, discovery_category=verified.discovery_category, verification_status=verified.status,
        corroboration_url=posting.corroboration_url, corroborated_official=bool(cand.raw_metadata.get("corroborated_official")),
    )
    old_category = posting.discovery_category
    old_codes = list(posting.skip_codes_json or [])
    posting.source_verification_tier = tier
    posting.discovery_category = verified.discovery_category
    posting.skip_reason = sighting.primary_skip_code = posting.primary_skip_code = ""
    sighting.secondary_skip_codes_json = posting.secondary_skip_codes_json = list(verified.secondary_skip_codes or [])
    posting.skip_codes_json = list(verified.skip_codes or [])
    cvs, _ = _load_active_cvs(db, user)
    files = db.scalars(select(SupportingFile).where(SupportingFile.user_id == user.id,
                                                    SupportingFile.enabled_for_matching.is_(True))).all()
    _prepare_verified_application(db, user=user, profile=profile, pref=pref, batch=batch, posting=posting,
                                  sighting=sighting, cv_candidates=cvs, enabled_supporting_files=files,
                                  tier_review_reasons=review, now=now, existing=app_rec)
    if app_rec.primary_skip_code == "PREVIOUSLY_CONTACTED":
        raise ValueError("Recipient remains blocked by contact or suppression history")
    categories = dict(batch.category_counts_json or {})
    categories[old_category] = max(0, categories.get(old_category, 0) - 1)
    categories[posting.discovery_category] = categories.get(posting.discovery_category, 0) + 1
    batch.category_counts_json = categories
    skips = dict(batch.skip_reason_counts_json or {})
    for code in old_codes:
        skips[code] = max(0, skips.get(code, 0) - 1)
    for code in posting.skip_codes_json:
        skips[code] = skips.get(code, 0) + 1
    batch.skip_reason_counts_json = skips
    counts = dict(batch.metrics_json or {})
    if app_rec.transport_status == "draft":
        batch.skipped_count = max(0, batch.skipped_count - 1)
        counts[app_rec.approval_state] = counts.get(app_rec.approval_state, 0) + 1
    counts.update(drafted=batch.drafted_count, eligible_for_draft=batch.eligible_count,
                  eligible_hard_gates_passed=batch.eligible_count, skipped=batch.skipped_count,
                  goal_met=batch.drafted_count >= batch.target_goal_count)
    batch.metrics_json = counts
    batch.goal_shortfall_reason = _goal_shortfall_reason(batch)
    batch.stage_status_json = [
        {**row, "eligible": batch.eligible_count, "drafted": batch.drafted_count,
         "needs_human_review": counts.get("needs_human_review", 0),
         "ready_for_approval": counts.get("ready_for_approval", 0)}
        if row.get("stage") == "eligibility_and_drafting" else row for row in batch.stage_status_json or []
    ]
    db.flush()


def reevaluate_batch_eligibility(
    db: Session,
    *,
    batch_id: str,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """
    Re-run mandatory eligibility extraction and matching over an existing batch's persisted postings
    WITHOUT re-fetching URLs or sending any emails. Updates batches, job_postings, job_matches, and applications.
    """
    now = now_utc or utcnow()
    batch = db.scalar(select(Batch).where(Batch.id == batch_id))
    if not batch:
        raise ValueError(f"Batch not found: {batch_id}")

    user = db.scalar(select(User).where(User.id == batch.user_id))
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == batch.user_id))
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == batch.user_id))
    if not user or not profile or not pref:
        raise ValueError("Missing user, profile, or preference for batch re-evaluation")

    active_docs = db.scalars(
        select(CvDocument).where(CvDocument.user_id == user.id, CvDocument.is_archived.is_(False))
    ).all()
    cv_candidates: list[tuple[CvVersion, list[CvExtractedFact]]] = []
    for doc in active_docs:
        ver = db.scalar(
            select(CvVersion).where(
                CvVersion.cv_document_id == doc.id,
                CvVersion.user_id == user.id,
                CvVersion.parse_status == "parsed",
            ).order_by(CvVersion.version_number.desc())
        )
        if ver:
            facts = list(db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == ver.id)).all())
            cv_candidates.append((ver, facts))

    postings = db.scalars(select(JobPosting).where(JobPosting.batch_id == batch.id).order_by(JobPosting.created_at.asc())).all()
    category_counts: dict[str, int] = {
        "verified_email_vacancy": 0,
        "verified_general_cv_invitation": 0,
        "website_application_only": 0,
        "informational_post": 0,
        "incomplete_evidence": 0,
        "rejected": 0,
        "demo_fixture": 0,
    }
    skip_reason_counts: dict[str, int] = {}
    eligible_total = 0
    skipped_total = 0
    changed_to_skipped: list[dict[str, object]] = []

    for posting in postings:
        reqs, prof_elig = assess_posting_mandatory_eligibility(
            posting=posting,
            profile=profile,
            check_cv_requirements=False,
        )
        posting.eligibility_requirements_json = reqs.to_dict()
        ev_json = dict(posting.extraction_evidence_json or {})
        ev_json["eligibility_requirements"] = posting.eligibility_requirements_json
        posting.extraction_evidence_json = ev_json

        app_rec = db.scalar(
            select(Application).where(Application.batch_id == batch.id, Application.job_posting_id == posting.id)
        )

        if posting.verification_status == "verified" and posting.primary_skip_code != "PREVIOUSLY_CONTACTED":
            was_eligible = bool(app_rec and app_rec.transport_status != "skipped")

            if not prof_elig.passed:
                f_codes = list(prof_elig.failure_codes) or ["QUALIFICATION_MISMATCH"]
                primary_f = f_codes[0]
                sec_f = [c for c in f_codes if c != primary_f]
                reason = f"Rejected before CV selection: {'; '.join(prof_elig.failure_messages())}"
                posting.discovery_category = "demo_fixture" if posting.is_demo else "rejected"
                posting.primary_skip_code = primary_f
                posting.skip_codes_json = f_codes
                posting.secondary_skip_codes_json = sec_f
                posting.skip_reason = reason
                matches = db.scalars(select(JobMatch).where(JobMatch.job_posting_id == posting.id)).all()
                for m in matches:
                    m.hard_filter_passed = False
                    m.is_selected_cv = False
                    m.failure_codes_json = f_codes
                    m.hard_filter_failures_json = prof_elig.failure_messages()
                    m.decision_reason = reason
                if app_rec:
                    app_rec.transport_status = "skipped"
                    app_rec.discovery_category = posting.discovery_category
                    app_rec.primary_skip_code = primary_f
                    app_rec.skip_codes_json = f_codes
                    app_rec.secondary_skip_codes_json = sec_f
                    app_rec.skip_or_error_reason = reason
                    app_rec.selected_cv_version_id = None
                if was_eligible:
                    changed_to_skipped.append(
                        {
                            "posting_id": posting.id,
                            "application_id": app_rec.id if app_rec else None,
                            "recipient_email": posting.application_email,
                            "primary_skip_code": primary_f,
                            "skip_codes": f_codes,
                            "skip_reason": reason,
                        }
                    )
            else:
                best_eval, all_evals = select_best_cv_for_posting(
                    posting=posting,
                    cv_candidates=cv_candidates,
                    pref=pref,
                    profile=profile,
                    now_utc=now,
                )
                existing_matches = {
                    m.cv_version_id: m
                    for m in db.scalars(select(JobMatch).where(JobMatch.job_posting_id == posting.id)).all()
                }
                for ev in all_evals:
                    jm = existing_matches.get(ev.cv_version_id)
                    if jm:
                        jm.hard_filter_passed = ev.hard_filter_passed
                        jm.hard_filter_failures_json = ev.hard_filter_failures
                        jm.failure_codes_json = ev.failure_codes
                        jm.eligibility_check_json = ev.eligibility_check
                        jm.decision_reason = ev.decision_reason
                        jm.is_selected_cv = bool(best_eval and ev.cv_version_id == best_eval.cv_version_id and ev.hard_filter_passed)

                if not best_eval or not best_eval.hard_filter_passed:
                    f_codes = list(best_eval.failure_codes) if best_eval and best_eval.failure_codes else ["QUALIFICATION_MISMATCH"]
                    primary_f = f_codes[0]
                    sec_f = [c for c in f_codes if c != primary_f]
                    reason = best_eval.decision_reason if best_eval else "No eligible CV matched"
                    posting.discovery_category = (
                        "demo_fixture"
                        if posting.is_demo
                        else ("incomplete_evidence" if primary_f in ("JOB_LOCATION_UNKNOWN", "LOCATION_UNKNOWN") else "rejected")
                    )
                    posting.primary_skip_code = primary_f
                    posting.skip_codes_json = f_codes
                    posting.secondary_skip_codes_json = sec_f
                    posting.skip_reason = reason
                    if app_rec:
                        app_rec.transport_status = "skipped"
                        app_rec.discovery_category = posting.discovery_category
                        app_rec.primary_skip_code = primary_f
                        app_rec.skip_codes_json = f_codes
                        app_rec.secondary_skip_codes_json = sec_f
                        app_rec.skip_or_error_reason = reason
                    if was_eligible:
                        changed_to_skipped.append(
                            {
                                "posting_id": posting.id,
                                "application_id": app_rec.id if app_rec else None,
                                "recipient_email": posting.application_email,
                                "primary_skip_code": primary_f,
                                "skip_codes": f_codes,
                                "skip_reason": reason,
                            }
                        )

        cat = posting.discovery_category or "incomplete_evidence"
        if cat in category_counts:
            category_counts[cat] += 1
        if app_rec and app_rec.transport_status != "skipped" and posting.verification_status == "verified" and not posting.primary_skip_code:
            eligible_total += 1
        else:
            skipped_total += 1
            for code in (posting.skip_codes_json or []):
                if code:
                    skip_reason_counts[code] = skip_reason_counts.get(code, 0) + 1

    batch.eligible_count = eligible_total
    batch.skipped_count = skipped_total
    batch.drafted_count = eligible_total
    batch.category_counts_json = category_counts
    batch.skip_reason_counts_json = skip_reason_counts
    prev_metrics = dict(batch.metrics_json or {})
    prev_metrics.update(
        {
            "eligible_for_draft": eligible_total,
            "drafted": eligible_total,
            "skipped": skipped_total,
        }
    )
    batch.metrics_json = prev_metrics
    db.commit()
    return {
        "batch_id": batch.id,
        "discovered_count": batch.discovered_count,
        "eligible_count": batch.eligible_count,
        "skipped_count": batch.skipped_count,
        "category_counts": batch.category_counts_json,
        "skip_reason_counts": batch.skip_reason_counts_json,
        "metrics": batch.metrics_json,
        "newly_rejected": changed_to_skipped,
    }


def audit_existing_eligible_applications(
    db: Session,
    *,
    now_utc: datetime | None = None,
    include_demo: bool = False,
) -> list[dict[str, object]]:
    """Audit all existing non-skipped Application records across all batches against mandatory eligibility rules."""
    stmt = select(Application).where(Application.transport_status != "skipped")
    if not include_demo:
        stmt = stmt.where(Application.is_demo.is_(False))
    apps = db.scalars(stmt.order_by(Application.created_at.asc())).all()
    findings: list[dict[str, object]] = []
    for app_rec in apps:
        posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id))
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == app_rec.user_id))
        pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == app_rec.user_id))
        cv_ver = (
            db.scalar(select(CvVersion).where(CvVersion.id == app_rec.selected_cv_version_id))
            if app_rec.selected_cv_version_id
            else None
        )
        if not posting or not profile or not pref:
            continue
        facts = list(db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == cv_ver.id)).all()) if cv_ver else []
        reqs, res = assess_posting_mandatory_eligibility(
            posting=posting,
            profile=profile,
            cv_version=cv_ver,
            facts=facts,
            check_cv_requirements=True,
        )
        findings.append(
            {
                "application_id": app_rec.id,
                "batch_id": app_rec.batch_id,
                "recipient_email": app_rec.recipient_email,
                "company_name": app_rec.company_name,
                "eligible": res.passed,
                "status": res.status,
                "failure_codes": res.failure_codes,
                "failures": res.failure_messages(),
                "preference_notes": res.preference_notes,
            }
        )
    return findings
