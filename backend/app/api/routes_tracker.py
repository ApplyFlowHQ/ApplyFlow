"""Applications tracker, XLSX/CSV exports, history import, Google Sheets sync, operator admin, and health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import asc, desc, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_operator_user
from app.core.config import get_settings
from app.core.metrics import metrics
from app.core.security import normalize_email_dedupe_key
from app.db.base import as_utc
from app.db.models import (
    Application,
    ApplicationTransition,
    Batch,
    CvDocument,
    CvVersion,
    DiscoverySourceSighting,
    GmailSentRecord,
    JobMatch,
    JobPosting,
    MailboxConnection,
    MessageEvent,
    OutboxEntry,
    RecipientHistory,
    SearchPreference,
    SheetsConnection,
    User,
)
from app.db.session import get_db
from app.schemas import ApplicationPatchRequest
from app.services.approval import application_draft_hash
from app.services.dedupe import (
    find_existing_recipients,
    import_historical_recipients,
    record_duplicate_override,
    release_reserved_recipients_if_pre_handoff_failed,
)
from app.services.effective_config import (
    resolve_effective_ai_config,
    resolve_effective_google_oauth,
    resolve_effective_live_sending,
    resolve_effective_search_config,
)
from app.services.exports import (
    build_discovery_audit_rows,
    build_tracker_rows,
    export_applications_csv,
    export_discovery_csv,
    export_vnext_workbook_xlsx,
    parse_historical_import_file,
)
from app.services.followup import approve_followup, followup_view, prepare_followup
from app.services.outbox import (
    derive_application_queue_state,
    get_latest_outbox_map,
    reconcile_application_outbox_states,
)
from app.services.sheets import (
    build_sheets_oauth_url,
    complete_sheets_oauth_callback,
    sync_applications_to_google_sheet,
)

router = APIRouter(tags=["tracker-exports-ops"])


def _load_tracker_context(db: Session, user_id: str, apps: list[Application]):
    batch_ids = {a.batch_id for a in apps if a.batch_id}
    post_ids = {a.job_posting_id for a in apps if a.job_posting_id}
    match_ids = {a.job_match_id for a in apps if a.job_match_id}
    cv_ver_ids = {a.selected_cv_version_id for a in apps if a.selected_cv_version_id}

    batches = {b.id: b for b in db.scalars(select(Batch).where(Batch.id.in_(batch_ids), Batch.user_id == user_id)).all()} if batch_ids else {}
    postings = {p.id: p for p in db.scalars(select(JobPosting).where(JobPosting.id.in_(post_ids), JobPosting.user_id == user_id)).all()} if post_ids else {}
    matches = {m.id: m for m in db.scalars(select(JobMatch).where(JobMatch.id.in_(match_ids), JobMatch.user_id == user_id)).all()} if match_ids else {}

    cv_labels: dict[str, str] = {}
    if cv_ver_ids:
        vers = db.scalars(select(CvVersion).where(CvVersion.id.in_(cv_ver_ids), CvVersion.user_id == user_id)).all()
        doc_ids = {v.cv_document_id for v in vers}
        docs = {d.id: d for d in db.scalars(select(CvDocument).where(CvDocument.id.in_(doc_ids), CvDocument.user_id == user_id)).all()} if doc_ids else {}
        for v in vers:
            doc = docs.get(v.cv_document_id)
            cv_labels[v.id] = f"{doc.variant_label if doc else 'CV'} v{v.version_number} ({v.original_filename})"
    return batches, postings, matches, cv_labels


def _apply_scope_filter(stmt, is_demo_col, scope: str):
    if scope == "real":
        return stmt.where(is_demo_col.is_(False))
    if scope == "demo":
        return stmt.where(is_demo_col.is_(True))
    return stmt


@router.get("/applications")
def api_list_applications(
    q: str = "",
    transport_status: str = "",
    response_status: str = "",
    outcome_status: str = "",
    batch_id: str = "",
    country_code: str = "",
    scope: str = "real",
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 25,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    reconcile_application_outbox_states(db, user_id=user.id)
    stmt = _apply_scope_filter(
        select(Application).where(Application.user_id == user.id),
        Application.is_demo,
        scope,
    )
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Application.company_name.ilike(like),
                Application.role_title.ilike(like),
                Application.source_domain.ilike(like),
                Application.recipient_email.ilike(like),
                Application.city.ilike(like),
                Application.notes.ilike(like),
            )
        )
    if transport_status:
        stmt = stmt.where(Application.transport_status == transport_status)
    if response_status:
        stmt = stmt.where(Application.response_status == response_status)
    if outcome_status:
        stmt = stmt.where(Application.outcome_status == outcome_status)
    if batch_id:
        stmt = stmt.where(Application.batch_id == batch_id)
    if country_code:
        stmt = stmt.where(Application.country_code == country_code.upper())

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    demo_excluded_count = (
        db.scalar(
            select(func.count(Application.id)).where(
                Application.user_id == user.id,
                Application.is_demo.is_(True),
            )
        )
        or 0
    )

    sort_col_map = {
        "created_at": Application.created_at,
        "sent_at": Application.sent_at,
        "discovered_at": Application.discovered_at,
        "company_name": Application.company_name,
        "role_title": Application.role_title,
        "transport_status": Application.transport_status,
        "outcome_status": Application.outcome_status,
    }
    col = sort_col_map.get(sort_by, Application.created_at)
    stmt = stmt.order_by(asc(col) if sort_dir == "asc" else desc(col))

    safe_size = min(max(1, page_size), 100)
    safe_page = max(1, page)
    apps = list(db.scalars(stmt.offset((safe_page - 1) * safe_size).limit(safe_size)).all())
    batches, postings, matches, cv_labels = _load_tracker_context(db, user.id, apps)
    outbox_by_app = get_latest_outbox_map(db, [a.id for a in apps])

    return {
        "scope": scope,
        "total": int(total),
        "demo_excluded_count": int(demo_excluded_count),
        "page": safe_page,
        "page_size": safe_size,
        "applications": [
            {
                "id": a.id,
                "batch_id": a.batch_id,
                "batch_mode": batches[a.batch_id].mode if a.batch_id in batches else "",
                "is_demo": a.is_demo,
                "discovered_at": a.discovered_at.isoformat(),
                "sent_at": as_utc(a.sent_at).isoformat() if a.sent_at else None,
                "company_name": (postings[a.job_posting_id].validated_employer if a.job_posting_id in postings else "") or a.company_name,
                "source_domain": a.source_domain or (postings[a.job_posting_id].company_domain if a.job_posting_id in postings else ""),
                "publisher": postings[a.job_posting_id].publisher if a.job_posting_id in postings else "",
                "validated_employer": postings[a.job_posting_id].validated_employer if a.job_posting_id in postings else a.company_name,
                "purported_employer": postings[a.job_posting_id].purported_employer if a.job_posting_id in postings else "",
                "role_title": (postings[a.job_posting_id].role_extracted if a.job_posting_id in postings else "") or a.role_title,
                "role_extracted": postings[a.job_posting_id].role_extracted if a.job_posting_id in postings else "",
                "country_code": a.country_code,
                "city": a.city,
                "location_confidence": postings[a.job_posting_id].location_confidence if a.job_posting_id in postings else "",
                "date_source": postings[a.job_posting_id].date_source if a.job_posting_id in postings else "",
                "access_status": postings[a.job_posting_id].access_status if a.job_posting_id in postings else "",
                "recipient_email": a.recipient_email,
                "cc_emails": a.cc_emails_json,
                "selected_cv": cv_labels.get(a.selected_cv_version_id or "", a.frozen_cv_filename),
                "source_url": postings[a.job_posting_id].source_url if a.job_posting_id in postings else "",
                "canonical_url": postings[a.job_posting_id].canonical_url if a.job_posting_id in postings else "",
                "fit_score": matches[a.job_match_id].fit_score if a.job_match_id in matches else None,
                "discovery_category": a.discovery_category or (postings[a.job_posting_id].discovery_category if a.job_posting_id in postings else ""),
                "primary_skip_code": a.primary_skip_code or (postings[a.job_posting_id].primary_skip_code if a.job_posting_id in postings else ""),
                "secondary_skip_codes": a.secondary_skip_codes_json or (postings[a.job_posting_id].secondary_skip_codes_json if a.job_posting_id in postings else []),
                "transport_status": a.transport_status,
                "queue_state": derive_application_queue_state(a, outbox_by_app.get(a.id)),
                "outbox_id": outbox_by_app[a.id].id if a.id in outbox_by_app else None,
                "outbox_status": outbox_by_app[a.id].status if a.id in outbox_by_app else None,
                "outbox_attempt_count": int(outbox_by_app[a.id].attempt_count) if a.id in outbox_by_app else 0,
                "outbox_next_attempt_at": outbox_by_app[a.id].next_attempt_at.isoformat() if a.id in outbox_by_app and outbox_by_app[a.id].next_attempt_at else None,
                "outbox_last_error": outbox_by_app[a.id].last_error if a.id in outbox_by_app else "",
                "outbox_handoff_outcome": outbox_by_app[a.id].handoff_outcome if a.id in outbox_by_app else "none",
                "approval_state": getattr(a, "approval_state", "draft"),
                "source_verification_tier": getattr(a, "source_verification_tier", "tier_5_unrelated_or_prohibited"),
                "review_reasons": getattr(a, "review_reasons_json", []) or [],
                "attachment_pdf_sha256": getattr(a, "attachment_pdf_sha256", "") or a.frozen_cv_sha256,
                "draft_content_hash": getattr(a, "draft_content_hash", "") or application_draft_hash(a),
                "approved_content_hash": getattr(a, "approved_content_hash", "") or "",
                "approved_at": a.approved_at.isoformat() if getattr(a, "approved_at", None) else None,
                "provider_message_id": a.provider_message_id or "",
                "rfc_message_id": a.rfc_message_id or "",
                "why_this_cv": getattr(matches[a.job_match_id], "why_this_cv", "") if a.job_match_id in matches else "",
                "response_status": a.response_status,
                "outcome_status": a.outcome_status,
                "outcome_confidence": a.outcome_confidence,
                "follow_up_count": a.follow_up_count,
                "next_follow_up_at": as_utc(a.next_follow_up_at).isoformat() if a.next_follow_up_at else None,
                "skip_or_error_reason": a.skip_or_error_reason or (outbox_by_app[a.id].last_error if a.id in outbox_by_app else ""),
                "notes": a.notes,
            }
            for a in apps
        ],
    }


@router.get("/discovery-sources")
def api_list_discovery_sources(
    q: str = "",
    category: str = "",
    skip_code: str = "",
    batch_id: str = "",
    scope: str = "real",
    duplicates_only: bool = False,
    page: int = 1,
    page_size: int = 25,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Separated raw search discovery & evidence audit view (never impersonates Applications)."""
    safe_size = min(max(1, page_size), 100)
    safe_page = max(1, page)

    dup_stmt = _apply_scope_filter(
        select(func.count(DiscoverySourceSighting.id)).where(
            DiscoverySourceSighting.user_id == user.id,
            DiscoverySourceSighting.is_duplicate.is_(True),
        ),
        DiscoverySourceSighting.is_demo,
        scope,
    )
    if batch_id:
        dup_stmt = dup_stmt.where(DiscoverySourceSighting.batch_id == batch_id)
    duplicates_total = db.scalar(dup_stmt) or 0

    if duplicates_only:
        s_stmt = _apply_scope_filter(
            select(DiscoverySourceSighting).where(
                DiscoverySourceSighting.user_id == user.id,
                DiscoverySourceSighting.is_duplicate.is_(True),
            ),
            DiscoverySourceSighting.is_demo,
            scope,
        )
        if batch_id:
            s_stmt = s_stmt.where(DiscoverySourceSighting.batch_id == batch_id)
        if q.strip():
            like = f"%{q.strip()}%"
            s_stmt = s_stmt.where(
                or_(
                    DiscoverySourceSighting.source_url.ilike(like),
                    DiscoverySourceSighting.canonical_url.ilike(like),
                    DiscoverySourceSighting.title_raw.ilike(like),
                    DiscoverySourceSighting.source_domain.ilike(like),
                    DiscoverySourceSighting.publisher.ilike(like),
                    DiscoverySourceSighting.search_query.ilike(like),
                )
            )
        total = db.scalar(select(func.count()).select_from(s_stmt.subquery())) or 0
        sightings = list(
            db.scalars(
                s_stmt.order_by(DiscoverySourceSighting.seen_at.desc())
                .offset((safe_page - 1) * safe_size)
                .limit(safe_size)
            ).all()
        )
        return {
            "scope": scope,
            "duplicates_only": True,
            "total": int(total),
            "duplicates_total": int(duplicates_total),
            "page": safe_page,
            "page_size": safe_size,
            "items": [
                {
                    "id": s.id,
                    "batch_id": s.batch_id,
                    "is_demo": s.is_demo,
                    "source_provider": s.source_provider,
                    "publisher": s.publisher,
                    "source_domain": s.source_domain,
                    "validated_employer": "",
                    "purported_employer": "",
                    "role_extracted": "",
                    "raw_title": s.title_raw,
                    "country_code": "",
                    "city": "",
                    "location_confidence": "unknown",
                    "date_source": "unknown",
                    "access_status": s.access_status,
                    "discovery_category": "duplicate",
                    "is_duplicate": True,
                    "dedupe_reason": s.dedupe_reason,
                    "application_email": "",
                    "subject_instruction": "",
                    "source_url": s.source_url,
                    "canonical_url": s.canonical_url,
                    "search_query": s.search_query,
                    "primary_skip_code": s.primary_skip_code or "DUPLICATE_SOURCE",
                    "secondary_skip_codes": s.secondary_skip_codes_json,
                    "skip_reason": s.dedupe_reason,
                    "created_at": s.seen_at.isoformat(),
                }
                for s in sightings
            ],
        }

    p_stmt = _apply_scope_filter(
        select(JobPosting).where(JobPosting.user_id == user.id),
        JobPosting.is_demo,
        scope,
    )
    if batch_id:
        p_stmt = p_stmt.where(JobPosting.batch_id == batch_id)
    if category:
        p_stmt = p_stmt.where(JobPosting.discovery_category == category)
    if skip_code:
        p_stmt = p_stmt.where(JobPosting.primary_skip_code == skip_code)
    if q.strip():
        like = f"%{q.strip()}%"
        p_stmt = p_stmt.where(
            or_(
                JobPosting.title.ilike(like),
                JobPosting.role_extracted.ilike(like),
                JobPosting.company_name.ilike(like),
                JobPosting.validated_employer.ilike(like),
                JobPosting.company_domain.ilike(like),
                JobPosting.publisher.ilike(like),
                JobPosting.application_email.ilike(like),
                JobPosting.source_url.ilike(like),
                JobPosting.search_query.ilike(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(p_stmt.subquery())) or 0
    postings = list(
        db.scalars(
            p_stmt.order_by(JobPosting.created_at.desc())
            .offset((safe_page - 1) * safe_size)
            .limit(safe_size)
        ).all()
    )
    return {
        "scope": scope,
        "duplicates_only": False,
        "total": int(total),
        "duplicates_total": int(duplicates_total),
        "page": safe_page,
        "page_size": safe_size,
        "items": [
            {
                "id": p.id,
                "batch_id": p.batch_id,
                "is_demo": p.is_demo,
                "source_provider": p.source_provider,
                "publisher": p.publisher,
                "source_domain": p.company_domain,
                "validated_employer": p.validated_employer,
                "purported_employer": p.purported_employer,
                "role_extracted": p.role_extracted,
                "raw_title": p.title,
                "country_code": p.country_code,
                "city": p.city,
                "location_confidence": p.location_confidence,
                "date_source": p.date_source,
                "access_status": p.access_status,
                "discovery_category": p.discovery_category,
                "is_duplicate": False,
                "dedupe_reason": "",
                "application_email": p.application_email,
                "subject_instruction": p.subject_instruction,
                "source_url": p.source_url,
                "canonical_url": p.canonical_url or p.source_url,
                "search_query": p.search_query,
                "primary_skip_code": p.primary_skip_code,
                "secondary_skip_codes": p.secondary_skip_codes_json,
                "skip_reason": p.skip_reason,
                "created_at": p.created_at.isoformat(),
            }
            for p in postings
        ],
    }


class DuplicateOverrideRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class FollowupApprovalRequest(BaseModel):
    draft_id: str
    confirm_inbox: bool = False


def _owned_application(db, user, app_id):
    app = db.scalar(select(Application).where(Application.id == app_id, Application.user_id == user.id).with_for_update())
    if not app:
        raise HTTPException(404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})
    return app


@router.post("/applications/{app_id}/duplicate-override")
def api_duplicate_override(app_id: str, payload: DuplicateOverrideRequest,
                           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.batch_runner import reprepare_duplicate_application

    app = _owned_application(db, user, app_id)
    try:
        record_duplicate_override(db, app=app, reason=payload.reason)
        reprepare_duplicate_application(db, app_rec=app)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, detail={"code": "OVERRIDE_BLOCKED", "message": str(exc)}) from exc
    return {"id": app.id, "approval_state": app.approval_state, "transport_status": app.transport_status}


@router.post("/applications/{app_id}/followup/draft")
def api_prepare_followup(app_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    app = _owned_application(db, user, app_id)
    try:
        prepare_followup(db, app=app, manual=True)
        db.commit()
    except ValueError as exc:
        raise HTTPException(409, detail={"code": "FOLLOWUP_BLOCKED", "message": str(exc)}) from exc
    return followup_view(db, app)


@router.post("/applications/{app_id}/followup/approve")
def api_approve_followup(app_id: str, payload: FollowupApprovalRequest,
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    app = _owned_application(db, user, app_id)
    try:
        approve_followup(db, app=app, draft_id=payload.draft_id, confirm_inbox=payload.confirm_inbox)
    except ValueError as exc:
        raise HTTPException(409, detail={"code": "FOLLOWUP_BLOCKED", "message": str(exc)}) from exc
    return followup_view(db, app)


@router.get("/applications/{app_id}")
def api_get_application_detail(
    app_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    app = db.scalar(select(Application).where(Application.id == app_id, Application.user_id == user.id))
    if not app:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})
    posting = db.scalar(select(JobPosting).where(JobPosting.id == app.job_posting_id, JobPosting.user_id == user.id))
    match = db.scalar(select(JobMatch).where(JobMatch.id == app.job_match_id, JobMatch.user_id == user.id)) if app.job_match_id else None
    events = db.scalars(
        select(MessageEvent)
        .where(MessageEvent.application_id == app.id, MessageEvent.user_id == user.id)
        .order_by(MessageEvent.occurred_at.asc())
    ).all()
    contact_evidence = []
    if app.primary_skip_code == "PREVIOUSLY_CONTACTED":
        from app.services.mailbox.gmail_sent import classify_sent_message_for_dedup
        for key, record in find_existing_recipients(db, user_id=user.id,
                emails=[app.recipient_email, *(app.cc_emails_json or [])]).items():
            if isinstance(record, GmailSentRecord):
                classification, _, _ = classify_sent_message_for_dedup(sender_email="", recipient_email=key, subject=record.subject)
                contact_evidence.append({"address": key, "source": "sent_history", "classification": classification})
            elif isinstance(record, str):
                contact_evidence.append({"address": key, "source": "blocked", "classification": ""})
            else:
                contact_evidence.append({"address": key, "source": getattr(record, "source", "application"), "classification": ""})
    return {
        "followup": followup_view(db, app),
        "contact_evidence": contact_evidence,
        "application": {
            "approval_state": app.approval_state,
            "verification_status": posting.verification_status if posting else "unknown",
            "provider_message_id": app.provider_message_id,
            "rfc_message_id": app.rfc_message_id,
            "id": app.id,
            "batch_id": app.batch_id,
            "is_demo": app.is_demo,
            "source_domain": app.source_domain or (posting.company_domain if posting else ""),
            "company_name": (posting.validated_employer if posting else "") or app.company_name,
            "role_title": (posting.role_extracted if posting else "") or app.role_title,
            "country_code": app.country_code,
            "city": app.city,
            "recipient_email": app.recipient_email,
            "cc_emails": app.cc_emails_json,
            "subject": app.subject,
            "body_text": app.body_text,
            "language": app.language,
            "frozen_cv_filename": app.frozen_cv_filename,
            "frozen_cv_sha256": app.frozen_cv_sha256,
            "evidence_fact_ids": app.evidence_fact_ids_json,
            "llm_provider_used": app.llm_provider_used,
            "llm_model_used": app.llm_model_used,
            "discovery_category": app.discovery_category or (posting.discovery_category if posting else ""),
            "primary_skip_code": app.primary_skip_code or (posting.primary_skip_code if posting else ""),
            "secondary_skip_codes": app.secondary_skip_codes_json or (posting.secondary_skip_codes_json if posting else []),
            "transport_status": app.transport_status,
            "response_status": app.response_status,
            "outcome_status": app.outcome_status,
            "outcome_confidence": app.outcome_confidence,
            "follow_up_count": app.follow_up_count,
            "next_follow_up_at": as_utc(app.next_follow_up_at).isoformat() if app.next_follow_up_at else None,
            "skip_or_error_reason": app.skip_or_error_reason,
            "notes": app.notes,
            "discovered_at": app.discovered_at.isoformat(),
            "sent_at": as_utc(app.sent_at).isoformat() if app.sent_at else None,
        },
        "source_evidence": {
            "source_url": posting.source_url if posting else "",
            "canonical_url": posting.canonical_url if posting else "",
            "search_query": posting.search_query if posting else "",
            "source_provider": posting.source_provider if posting else "",
            "publisher": posting.publisher if posting else "",
            "purported_employer": posting.purported_employer if posting else "",
            "validated_employer": posting.validated_employer if posting else "",
            "employer_confidence": posting.employer_confidence if posting else "",
            "role_extracted": posting.role_extracted if posting else "",
            "location_confidence": posting.location_confidence if posting else "",
            "date_source": posting.date_source if posting else "",
            "access_status": posting.access_status if posting else "",
            "parsing_version": posting.parsing_version if posting else "",
            "evidence_quote": posting.evidence_quote if posting else "",
            "posted_at": posting.posted_at.isoformat() if posting and posting.posted_at else None,
            "retrieved_at": posting.retrieved_at.isoformat() if posting else None,
            "first_seen_at": posting.first_seen_at.isoformat() if posting else None,
            "location_raw": posting.location_raw if posting else "",
            "remote_scope": posting.remote_scope if posting else "",
            "salary_min": posting.salary_min if posting else None,
            "salary_max": posting.salary_max if posting else None,
            "salary_currency": posting.salary_currency if posting else "",
            "salary_period": posting.salary_period if posting else "",
        },
        "match_explanation": {
            "fit_score": match.fit_score if match else None,
            "score_components": match.score_components_json if match else {},
            "supporting_facts": match.supporting_facts_json if match else [],
            "missing_requirements": match.missing_requirements_json if match else [],
            "decision_reason": match.decision_reason if match else "",
            "why_this_cv": getattr(match, "why_this_cv", "") if match else "",
            "verified_skills": getattr(match, "verified_skills_json", []) if match else [],
            "required_gaps": getattr(match, "required_gaps_json", []) if match else [],
            "attachment_ready": bool(getattr(match, "attachment_ready", True)) if match else True,
        },
        "transitions": [
            {
                "id": tr.id,
                "field_name": tr.field_name,
                "from_state": tr.from_state,
                "to_state": tr.to_state,
                "actor": tr.actor,
                "reason": tr.reason,
                "created_at": tr.created_at.isoformat(),
            }
            for tr in db.scalars(
                select(ApplicationTransition)
                .where(ApplicationTransition.application_id == app.id, ApplicationTransition.user_id == user.id)
                .order_by(ApplicationTransition.created_at.asc())
            ).all()
        ],
        "events": [
            {
                "id": ev.id,
                "event_type": ev.event_type,
                "direction": ev.direction,
                "sender_email": ev.sender_email,
                "subject": ev.subject,
                "snippet": ev.snippet,
                "confidence": ev.classification_confidence,
                "occurred_at": ev.occurred_at.isoformat(),
            }
            for ev in events
        ],
    }


@router.patch("/applications/{app_id}")
def api_patch_application(
    app_id: str,
    payload: ApplicationPatchRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    app = db.scalar(select(Application).where(Application.id == app_id, Application.user_id == user.id))
    if not app:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})
    if payload.outcome_status is not None:
        app.outcome_status = payload.outcome_status
        app.outcome_confidence = 1.0
        if payload.outcome_status in ("interview", "rejected", "withdrawn", "archived"):
            app.next_follow_up_at = None
            db.query(OutboxEntry).filter(OutboxEntry.user_id == user.id, OutboxEntry.application_id == app.id,
                OutboxEntry.kind == "followup", OutboxEntry.status.in_(["awaiting_approval", "queued"])).update(
                    {"status": "cancelled", "last_error": f"Stopped by outcome {payload.outcome_status}"})
        db.add(
            MessageEvent(
                user_id=user.id,
                application_id=app.id,
                event_type="outcome_updated",
                direction="internal",
                snippet=f"Outcome set manually to '{payload.outcome_status}'",
            )
        )
    if payload.notes is not None:
        app.notes = payload.notes
    db.commit()
    return {"id": app.id, "outcome_status": app.outcome_status, "notes": app.notes}


@router.post("/applications/{app_id}/suppress")
def api_suppress_application_recipient(
    app_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    # Acquire an application write lock on both supported databases before observing delivery state.
    changed = db.execute(update(Application).where(Application.id == app_id, Application.user_id == user.id)
                         .values(next_follow_up_at=None).returning(Application.recipient_email))
    row = changed.first()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})
    if row.recipient_email:
        key = normalize_email_dedupe_key(row.recipient_email)
        db.scalar(select(User.id).where(User.id == user.id).with_for_update())
        pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id)
                         .with_for_update().execution_options(populate_existing=True))
        if not pref:
            pref = SearchPreference(user_id=user.id)
            db.add(pref)
        pref.excluded_recipients_json = sorted(set(pref.excluded_recipients_json or []) | {key})
    cancelled = db.scalars(update(OutboxEntry).where(
        OutboxEntry.application_id == app_id, OutboxEntry.user_id == user.id,
        OutboxEntry.status.in_(["awaiting_approval", "queued", "leased"]), OutboxEntry.handoff_outcome == "none",
    ).values(status="cancelled", lease_owner=None, lease_expires_at=None, last_error="Recipient suppressed by user")
      .returning(OutboxEntry.kind)).all()
    if "initial" in cancelled:
        changed = db.execute(update(Application).where(
            Application.id == app_id, Application.user_id == user.id, Application.sent_at.is_(None),
            Application.transport_status.in_(["draft", "ready", "queued", "leased"]),
        ).values(transport_status="cancelled", skip_or_error_reason="Recipient suppressed by user"))
        if changed.rowcount:
            release_reserved_recipients_if_pre_handoff_failed(db, user_id=user.id, application_id=app_id)
    db.commit()
    return {"ok": True}


@router.get("/exports/csv")
def api_export_csv(
    scope: str = "real",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    stmt = _apply_scope_filter(
        select(Application).where(Application.user_id == user.id),
        Application.is_demo,
        scope,
    ).order_by(Application.created_at.desc())
    apps = list(db.scalars(stmt).all())
    batches, postings, matches, cv_labels = _load_tracker_context(db, user.id, apps)
    rows = build_tracker_rows(
        apps,
        batches_by_id=batches,
        postings_by_id=postings,
        matches_by_id=matches,
        cv_labels_by_version_id=cv_labels,
        tz_name=user.timezone,
    )
    data = export_applications_csv(rows)
    fname = "applyflow-applications-demo.csv" if scope == "demo" else "applyflow-applications.csv"
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/exports/xlsx")
def api_export_xlsx(
    scope: str = "real",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    apps = list(
        db.scalars(
            _apply_scope_filter(
                select(Application).where(Application.user_id == user.id),
                Application.is_demo,
                scope,
            ).order_by(Application.created_at.desc())
        ).all()
    )
    all_batches_stmt = select(Batch).where(Batch.user_id == user.id).order_by(Batch.created_at.desc())
    if scope == "real":
        all_batches_stmt = all_batches_stmt.where(Batch.mode != "demo_fixture")
    elif scope == "demo":
        all_batches_stmt = all_batches_stmt.where(Batch.mode == "demo_fixture")
    batches_list = list(db.scalars(all_batches_stmt).all())

    postings_list = list(
        db.scalars(
            _apply_scope_filter(
                select(JobPosting).where(JobPosting.user_id == user.id),
                JobPosting.is_demo,
                scope,
            ).order_by(JobPosting.created_at.desc())
        ).all()
    )
    sightings_list = list(
        db.scalars(
            _apply_scope_filter(
                select(DiscoverySourceSighting).where(DiscoverySourceSighting.user_id == user.id),
                DiscoverySourceSighting.is_demo,
                scope,
            ).order_by(DiscoverySourceSighting.seen_at.desc())
        ).all()
    )
    recipient_histories = list(
        db.scalars(select(RecipientHistory).where(RecipientHistory.user_id == user.id).order_by(RecipientHistory.created_at.desc())).all()
    )
    gmail_sent = list(
        db.scalars(select(GmailSentRecord).where(GmailSentRecord.user_id == user.id).order_by(GmailSentRecord.created_at.desc())).all()
    )
    _, _, matches, cv_labels = _load_tracker_context(db, user.id, apps)
    data = export_vnext_workbook_xlsx(
        applications=apps,
        batches=batches_list,
        postings=postings_list,
        sightings=sightings_list,
        matches_by_id=matches,
        cv_labels_by_version_id=cv_labels,
        recipient_histories=recipient_histories if scope != "demo" else [],
        gmail_sent_records=gmail_sent if scope != "demo" else [],
        mailbox_senders=dict(db.execute(select(MailboxConnection.id, MailboxConnection.sender_email).where(
            MailboxConnection.user_id == user.id)).all()),
        tz_name=user.timezone,
    )
    fname = "applyflow-applications-demo.xlsx" if scope == "demo" else "applyflow-applications.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/exports/discovery.csv")
def api_export_discovery_csv(
    batch_id: str = "",
    scope: str = "real",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    s_stmt = _apply_scope_filter(
        select(DiscoverySourceSighting).where(DiscoverySourceSighting.user_id == user.id),
        DiscoverySourceSighting.is_demo,
        scope,
    )
    if batch_id:
        s_stmt = s_stmt.where(DiscoverySourceSighting.batch_id == batch_id)
    sightings = list(db.scalars(s_stmt.order_by(DiscoverySourceSighting.seen_at.desc())).all())

    p_stmt = _apply_scope_filter(
        select(JobPosting).where(JobPosting.user_id == user.id),
        JobPosting.is_demo,
        scope,
    )
    if batch_id:
        p_stmt = p_stmt.where(JobPosting.batch_id == batch_id)
    postings = list(db.scalars(p_stmt.order_by(JobPosting.created_at.desc())).all())

    # Include legacy historical JobPostings that predate DiscoverySourceSighting
    seen_post_ids = {s.job_posting_id for s in sightings if s.job_posting_id}
    for p in postings:
        if p.id not in seen_post_ids:
            sightings.append(
                DiscoverySourceSighting(
                    user_id=p.user_id,
                    batch_id=p.batch_id,
                    job_posting_id=p.id,
                    is_demo=p.is_demo,
                    source_provider=p.source_provider,
                    search_query=p.search_query,
                    source_url=p.source_url,
                    canonical_url=p.canonical_url or p.source_url,
                    canonical_url_hash=p.canonical_url_hash,
                    source_domain=p.company_domain,
                    publisher=p.publisher,
                    title_raw=p.title,
                    access_status=p.access_status or "unknown",
                    disposition=p.discovery_category or "evaluated",
                    is_duplicate=False,
                    primary_skip_code=p.primary_skip_code or "",
                    secondary_skip_codes_json=list(p.secondary_skip_codes_json or []),
                    seen_at=p.created_at,
                )
            )

    batch_ids = {s.batch_id for s in sightings if s.batch_id}
    batches_by_id = {b.id: b for b in db.scalars(select(Batch).where(Batch.id.in_(batch_ids), Batch.user_id == user.id)).all()} if batch_ids else {}
    postings_by_id = {p.id: p for p in postings}
    rows = build_discovery_audit_rows(
        sightings,
        batches_by_id=batches_by_id,
        postings_by_id=postings_by_id,
        tz_name=user.timezone,
    )
    data = export_discovery_csv(rows)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="applyflow-discovery-sources.csv"'},
    )


@router.post("/exports/import-history")
async def api_import_history(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    raw = await file.read()
    try:
        entries = parse_historical_import_file(file.filename or "history.csv", raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_IMPORT_FILE", "message": str(exc)}) from exc
    inserted, skipped = import_historical_recipients(db, user_id=user.id, entries=entries)
    return {"inserted": inserted, "skipped": skipped}


@router.get("/sheets")
def api_get_sheets_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    conn = db.scalar(select(SheetsConnection).where(SheetsConnection.user_id == user.id))
    google_cfg = resolve_effective_google_oauth(db)
    return {
        "connected": bool(conn and conn.status == "connected"),
        "spreadsheet_id": conn.spreadsheet_id if conn else "",
        "spreadsheet_url": conn.spreadsheet_url if conn else "",
        "last_synced_at": conn.last_synced_at.isoformat() if conn and conn.last_synced_at else None,
        "last_error": conn.last_error if conn else "",
        "google_oauth_configured": bool(google_cfg.client_id and google_cfg.client_secret),
    }


@router.get("/sheets/connect-url")
def api_sheets_connect_url(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    try:
        return {"authorization_url": build_sheets_oauth_url(db, user_id=user.id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "OAUTH_NOT_CONFIGURED", "message": str(exc)}) from exc


@router.get("/sheets/callback")
def api_sheets_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if not state:
        raise HTTPException(status_code=400, detail={"code": "SHEETS_OAUTH_FAILED", "message": "Missing OAuth state"})
    try:
        complete_sheets_oauth_callback(db, user_id=user.id, code=None if error else code, state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "SHEETS_OAUTH_FAILED", "message": "Google Sheets connection failed. Please start again."}) from exc
    return RedirectResponse(url=f"{get_settings().app_url.rstrip('/')}/tracker?sheets=connected", status_code=302)


@router.post("/sheets/sync")
def api_sheets_sync_now(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    try:
        return sync_applications_to_google_sheet(db, user_id=user.id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"code": "SHEETS_SYNC_FAILED", "message": str(exc)}) from exc


@router.post("/sheets/disconnect")
def api_sheets_disconnect(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    conn = db.scalar(select(SheetsConnection).where(SheetsConnection.user_id == user.id))
    if conn:
        conn.encrypted_credentials = ""
        conn.status = "disconnected"
        db.commit()
    return {"ok": True}


@router.get("/operator/overview")
def api_operator_overview(
    operator: User = Depends(get_operator_user),
    db: Session = Depends(get_db),
) -> dict:
    """Minimal operator administration dashboard: never exposes passwords, OAuth tokens, or private CV content."""
    users = db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc()).limit(100)).all()
    queued_outbox = db.scalar(select(func.count(OutboxEntry.id)).where(OutboxEntry.status == "queued")) or 0
    uncertain_outbox = db.scalar(select(func.count(OutboxEntry.id)).where(OutboxEntry.status == "send_uncertain")) or 0
    live_sending_enabled, _ = resolve_effective_live_sending(db)
    return {
        "metrics": metrics.snapshot(),
        "queue": {"queued_outbox": int(queued_outbox), "uncertain_outbox": int(uncertain_outbox)},
        "live_sending_enabled": live_sending_enabled,
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "is_operator": u.is_operator,
                "is_suspended": u.is_suspended,
                "automation_stopped": u.automation_stopped_at is not None,
                "created_at": u.created_at.isoformat(),
            }
            for u in users
        ],
    }


@router.post("/operator/users/{target_user_id}/suspend")
def api_operator_suspend_user(
    target_user_id: str,
    operator: User = Depends(get_operator_user),
    db: Session = Depends(get_db),
) -> dict:
    target = db.scalar(select(User).where(User.id == target_user_id))
    if not target:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "User not found"})
    target.is_suspended = True
    db.commit()
    return {"id": target.id, "is_suspended": True}


@router.post("/operator/users/{target_user_id}/unsuspend")
def api_operator_unsuspend_user(
    target_user_id: str,
    operator: User = Depends(get_operator_user),
    db: Session = Depends(get_db),
) -> dict:
    target = db.scalar(select(User).where(User.id == target_user_id))
    if not target:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "User not found"})
    target.is_suspended = False
    db.commit()
    return {"id": target.id, "is_suspended": False}


@router.get("/health")
def api_health(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    db_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    live_sending_enabled, _ = resolve_effective_live_sending(db)
    ai_cfg = resolve_effective_ai_config(db)
    search_cfg = resolve_effective_search_config(db)
    return {
        "status": "ok" if db_ok else "degraded",
        "database_ok": db_ok,
        "database_dialect": "sqlite" if settings.is_sqlite else "postgresql",
        "live_sending_enabled": live_sending_enabled,
        "environment": settings.environment,
        "search_provider": search_cfg.provider,
        "llm_provider": ai_cfg.provider,
    }


@router.get("/health/metrics")
def api_health_metrics(operator: User = Depends(get_operator_user)) -> dict:
    return metrics.snapshot()
