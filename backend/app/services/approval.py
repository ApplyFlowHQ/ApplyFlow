"""Application draft content hashing, review-queue state transitions, and approval-lock enforcement."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import normalize_email_dedupe_key, normalize_email_display, sanitize_header_value
from app.db.base import as_utc, utcnow
from app.db.models import (
    Application,
    ApplicationTransition,
    AuditEvent,
    Batch,
    CandidateProfile,
    CvExtractedFact,
    CvVersion,
    JobPosting,
    SearchPreference,
    User,
)
from app.services.dedupe import find_existing_recipients
from app.services.discovery.career_page import EMAIL_RE
from app.services.eligibility import assess_posting_mandatory_eligibility
from app.services.pdf_renderer import (
    DOCX_ONLY_REASON,
    PDF_MIME,
    derived_pdf_filename,
    ensure_cv_version_pdf_ready,
    requires_docx_only,
)
from app.services.pre_handoff import cancel_initial_for_locked_draft, lock_mutable_draft
from app.services.verification import NOREPLY_LOCAL_PARTS, PLACEHOLDER_EMAILS, is_demo_email_or_domain


def compute_draft_content_hash(
    *,
    subject: str,
    body_text: str,
    recipient_email: str,
    cc_emails: list[str] | None,
    selected_cv_version_id: str | None,
    attachment_pdf_sha256: str,
    supporting_file_ids: list[str] | None,
) -> str:
    """Compute a deterministic SHA-256 fingerprint over all send-critical application fields."""
    payload = {
        "subject": (subject or "").strip(),
        "body_text": (body_text or "").strip(),
        "recipient_email": (recipient_email or "").strip().lower(),
        "cc_emails": sorted((c or "").strip().lower() for c in (cc_emails or []) if (c or "").strip()),
        "selected_cv_version_id": selected_cv_version_id or "",
        "attachment_pdf_sha256": (attachment_pdf_sha256 or "").strip(),
        "supporting_file_ids": sorted(str(x) for x in (supporting_file_ids or []) if x),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def application_draft_hash(app_rec: Application) -> str:
    return compute_draft_content_hash(
        subject=app_rec.subject or "",
        body_text=app_rec.body_text or "",
        recipient_email=app_rec.recipient_email or "",
        cc_emails=list(app_rec.cc_emails_json or []),
        selected_cv_version_id=app_rec.selected_cv_version_id,
        attachment_pdf_sha256=app_rec.attachment_pdf_sha256 or app_rec.frozen_cv_sha256 or "",
        supporting_file_ids=list(app_rec.supporting_file_ids_json or []),
    )


def record_application_transition(
    db: Session,
    *,
    user_id: str,
    application_id: str,
    field_name: str,
    from_state: str,
    to_state: str,
    actor: str = "system",
    reason: str = "",
    metadata: dict | None = None,
) -> None:
    if from_state == to_state and not reason:
        return
    db.add(
        ApplicationTransition(
            user_id=user_id,
            application_id=application_id,
            field_name=field_name,
            from_state=from_state or "",
            to_state=to_state,
            actor=actor[:64],
            reason=reason[:1000],
            metadata_json=metadata or {},
        )
    )


def sync_application_hash(app_rec: Application) -> str:
    new_hash = application_draft_hash(app_rec)
    app_rec.draft_content_hash = new_hash
    return new_hash


def revise_application_draft(
    db: Session,
    *,
    user: User,
    app_rec: Application,
    subject: str | None = None,
    body_text: str | None = None,
    recipient_email: str | None = None,
    cc_emails: list[str] | None = None,
    selected_cv_version_id: str | None = None,
    review_notes: str | None = None,
) -> tuple[Application, bool]:
    """
    Revise draft content safely and invalidate any prior approval hash if send-critical content changed.
    Returns (updated_application, approval_invalidated).
    """
    app_rec = lock_mutable_draft(db, user.id, app_rec.id)
    prev_approval = app_rec.approval_state or "draft"
    prev_transport = app_rec.transport_status or "draft"
    had_approval = bool(app_rec.approved_content_hash)

    if subject is not None:
        cleaned_subj = sanitize_header_value(subject, 300)
        if len(cleaned_subj) < 2:
            raise ValueError("Subject must be at least 2 characters")
        app_rec.subject = cleaned_subj
    if body_text is not None:
        cleaned_body = body_text.strip()
        if len(cleaned_body) < 10:
            raise ValueError("Body text is too short")
        app_rec.body_text = cleaned_body
    if recipient_email is not None and recipient_email.strip():
        raw_recip = recipient_email.strip()
        if not EMAIL_RE.fullmatch(raw_recip):
            raise ValueError(f"Invalid recipient email address: {raw_recip}")
        norm = normalize_email_display(raw_recip)
        norm_key = normalize_email_dedupe_key(norm)
        if norm_key != (app_rec.recipient_normalized or ""):
            app_rec.recipient_email = norm
            app_rec.recipient_normalized = norm_key
            app_rec.source_verification_tier = "tier_3_third_party_claim"
            reasons = list(app_rec.review_reasons_json or [])
            edit_notice = "Recipient email was manually edited; revalidate source & MX or confirm human review"
            if edit_notice not in reasons:
                reasons.append(edit_notice)
            app_rec.review_reasons_json = reasons
            if app_rec.approval_state not in ("approved", "rejected"):
                app_rec.approval_state = "needs_human_review"
    if cc_emails is not None:
        cleaned_ccs: list[str] = []
        for raw_cc in cc_emails:
            if raw_cc and raw_cc.strip():
                cc_s = raw_cc.strip()
                if not EMAIL_RE.fullmatch(cc_s):
                    raise ValueError(f"Invalid CC email address: {cc_s}")
                cleaned_ccs.append(normalize_email_display(cc_s))
        app_rec.cc_emails_json = cleaned_ccs
    selected_id = (selected_cv_version_id or "").strip()
    cv_changed = bool(selected_id and selected_id != app_rec.selected_cv_version_id)
    if selected_id:
        from app.db.models import CvDocument

        version = db.scalar(select(CvVersion).join(CvDocument).where(
            CvVersion.id == selected_id, CvVersion.user_id == user.id,
            CvDocument.user_id == user.id,
        ))
        if not version or version.parse_status != "parsed" or (cv_changed and version.document.is_archived):
            raise ValueError("Select a parsed version from an active CV document")
        posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id, JobPosting.user_id == user.id))
        if not posting:
            raise ValueError("Posting unavailable for CV revision")
        if requires_docx_only(posting.attachment_requirements_json):
            raise ValueError(DOCX_ONLY_REASON)
        ready, message = ensure_cv_version_pdf_ready(version)
        if not ready:
            raise ValueError(f"Attachment not ready: {message}")
        app_rec.frozen_cv_sha256 = version.sha256
        app_rec.attachment_pdf_sha256 = version.derived_pdf_sha256
        if cv_changed or not (app_rec.frozen_cv_filename or "").lower().endswith(".pdf"):
            app_rec.frozen_cv_filename = (
                version.original_filename if version.mime_type == PDF_MIME else derived_pdf_filename(version.original_filename)
            )

    if cv_changed:
        from app.db.models import JobMatch
        from app.services.ai.grounding import generate_grounded_application_draft
        from app.services.matching import evaluate_job_against_cv

        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
        pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
        if not profile or not pref:
            raise ValueError("Profile and preferences are required to change CV")
        facts = list(db.scalars(select(CvExtractedFact).where(
            CvExtractedFact.cv_version_id == version.id, CvExtractedFact.user_id == user.id
        )).all())
        ev = evaluate_job_against_cv(posting=posting, cv_version=version, facts=facts, pref=pref, profile=profile,
                                     has_supporting_attachments=bool(app_rec.supporting_file_ids_json))
        draft = generate_grounded_application_draft(
            posting=posting, profile=profile, pref=pref, facts=facts, allow_provider=False,
            supporting_fact_ids=[f["fact_id"] for f in ev.supporting_facts], language=ev.application_language,
        )
        matches = db.scalars(select(JobMatch).where(
            JobMatch.user_id == user.id, JobMatch.batch_id == app_rec.batch_id, JobMatch.job_posting_id == posting.id,
        )).all()
        for other in matches:
            other.is_selected_cv = False
        match = next((item for item in matches if item.cv_version_id == version.id), None)
        if match is None:
            match = JobMatch(user_id=user.id, batch_id=app_rec.batch_id, job_posting_id=posting.id, cv_version_id=version.id)
            db.add(match)
        match.is_selected_cv = True
        match.fit_score = ev.fit_score
        match.hard_filter_passed = ev.hard_filter_passed
        match.hard_filter_failures_json = ev.hard_filter_failures
        match.failure_codes_json = ev.failure_codes
        match.eligibility_check_json = ev.eligibility_check
        match.score_components_json = ev.score_components
        match.supporting_facts_json = ev.supporting_facts
        match.missing_requirements_json = ev.missing_requirements
        match.decision_reason = ev.decision_reason
        match.why_this_cv = ev.why_this_cv
        match.verified_skills_json = ev.verified_skills
        match.required_gaps_json = ev.required_gaps
        match.attachment_ready = ev.attachment_ready
        db.flush()
        app_rec.job_match_id = match.id
        app_rec.selected_cv_version_id = version.id
        app_rec.subject = draft.subject if posting.subject_instruction else app_rec.subject or draft.subject
        app_rec.body_text = draft.body_text
        app_rec.language = draft.language
        app_rec.evidence_fact_ids_json = draft.evidence_fact_ids
        app_rec.llm_provider_used = draft.provider_used
        app_rec.llm_model_used = draft.model_used
        if ev.review_reasons or ev.hard_filter_failures:
            app_rec.review_reasons_json = list(dict.fromkeys([
                *(app_rec.review_reasons_json or []), *ev.review_reasons, *ev.hard_filter_failures,
            ]))
            app_rec.approval_state = "needs_human_review"
    if review_notes is not None:
        app_rec.review_notes = review_notes[:4000]

    new_hash = sync_application_hash(app_rec)
    invalidated = False
    if had_approval and app_rec.approved_content_hash != new_hash:
        invalidated = True
        app_rec.approved_content_hash = ""
        app_rec.approved_at = None
        next_approval = "needs_human_review" if (app_rec.review_reasons_json or []) else "ready_for_approval"
        app_rec.approval_state = next_approval
        record_application_transition(
            db,
            user_id=user.id,
            application_id=app_rec.id,
            field_name="approval_state",
            from_state=prev_approval,
            to_state=next_approval,
            actor=f"user:{user.id}",
            reason="Draft modified after prior approval; re-approval required before sending",
            metadata={"new_hash": new_hash},
        )
        if app_rec.transport_status in ("ready", "queued", "leased"):
            app_rec.transport_status = "draft"
            app_rec.skip_or_error_reason = ""
            record_application_transition(
                db,
                user_id=user.id,
                application_id=app_rec.id,
                field_name="transport_status",
                from_state=prev_transport,
                to_state="draft",
                actor=f"user:{user.id}",
                reason="Queued send returned to draft because approved draft was edited",
            )
        if cancel_initial_for_locked_draft(db, app_rec, "Approval invalidated due to draft revision"):
            from app.services.dedupe import release_reserved_recipients_if_pre_handoff_failed

            release_reserved_recipients_if_pre_handoff_failed(db, user_id=user.id, application_id=app_rec.id)
        db.add(
            AuditEvent(
                user_id=user.id,
                actor_user_id=user.id,
                action="draft_approval_invalidated",
                target_type="application",
                target_id=app_rec.id,
                details_json={"new_draft_hash": new_hash},
            )
        )
    else:
        record_application_transition(
            db,
            user_id=user.id,
            application_id=app_rec.id,
            field_name="approval_state",
            from_state=prev_approval,
            to_state=app_rec.approval_state,
            actor=f"user:{user.id}",
            reason="Draft revised",
            metadata={"draft_hash": new_hash},
        )

    return app_rec, invalidated


def approve_application_for_send(
    db: Session,
    *,
    user: User,
    app_rec: Application,
    batch: Batch | None = None,
    review_notes: str = "",
    allow_third_party_override: bool = False,
) -> tuple[bool, str]:
    """
    Validate all safety & attachment prerequisites and stamp `approved_content_hash` on an application.
    Returns (ok, error_or_status_message).
    """
    if app_rec.is_demo:
        return False, "Demo fixture applications cannot be approved for live sending"
    if app_rec.transport_status in ("sent", "sending", "send_uncertain", "skipped"):
        return False, f"Application transport status '{app_rec.transport_status}' cannot be approved for sending"
    if (app_rec.approval_state or "") == "rejected":
        return False, app_rec.skip_or_error_reason or "Blocked/rejected application (eligibility or duplicate failure) cannot be overridden"
    if not (app_rec.subject or "").strip() or not (app_rec.body_text or "").strip():
        return False, "Draft subject and body cannot be empty"
    if not (app_rec.recipient_email or "").strip():
        return False, "Missing verified recipient email"
    if not (app_rec.attachment_pdf_sha256 or app_rec.frozen_cv_sha256):
        return False, "CV attachment checksum is not ready"
    if (app_rec.source_verification_tier or "") in ("tier_4_non_email_or_closed", "tier_5_unrelated_or_prohibited"):
        return False, f"Source tier '{app_rec.source_verification_tier}' is not eligible for email application"

    addresses = [app_rec.recipient_email, *(app_rec.cc_emails_json or [])]
    for address in addresses:
        key = normalize_email_dedupe_key(address)
        if (not EMAIL_RE.fullmatch(address) or key.partition("@")[0] in NOREPLY_LOCAL_PARTS
                or key in PLACEHOLDER_EMAILS or is_demo_email_or_domain(address)):
            return False, "Invalid, automated or placeholder recipient"
    if find_existing_recipients(db, user_id=user.id, emails=addresses, exclude_application_id=app_rec.id):
        return False, "A recipient is blocked or previously contacted"
    posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id, JobPosting.user_id == user.id))
    cv = db.scalar(select(CvVersion).where(CvVersion.id == app_rec.selected_cv_version_id, CvVersion.user_id == user.id))
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if not posting or not cv or not profile:
        return False, "Posting, selected CV or candidate profile unavailable"
    if requires_docx_only(posting.attachment_requirements_json):
        return False, DOCX_ONLY_REASON
    if posting.closing_at and as_utc(posting.closing_at) < utcnow():
        return False, "Vacancy has closed"
    supported = {normalize_email_dedupe_key(address) for address in
                 [posting.application_email, *(posting.cc_emails_json or [])] if address}
    if any(normalize_email_dedupe_key(address) not in supported for address in addresses):
        return False, "Source evidence does not support the edited recipient or CC"
    facts = db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == cv.id)).all()
    _, eligibility = assess_posting_mandatory_eligibility(posting=posting, profile=profile, cv_version=cv,
                                                         facts=facts, check_cv_requirements=True)
    if not eligibility.passed:
        return False, "Missing mandatory requirement: " + ", ".join(eligibility.failure_messages())
    if app_rec.import_origin != "gemini_zip":
        from app.services.matching import evaluate_job_against_cv

        pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
        if not pref:
            return False, "Complete search preferences before approval"
        match = evaluate_job_against_cv(posting=posting, cv_version=cv, facts=list(facts), pref=pref,
                                        profile=profile, has_supporting_attachments=bool(app_rec.supporting_file_ids_json))
        if not match.hard_filter_passed:
            return False, ", ".join(match.hard_filter_failures) or "The selected CV does not meet mandatory requirements"
    from app.services.outbox import _load_verified_attachments
    from app.services.storage import get_storage_adapter

    try:
        _load_verified_attachments(db, app_rec=app_rec, storage=get_storage_adapter())
    except Exception:
        return False, "Verified attachment unavailable. Select a valid PDF and review the draft again."

    prev_state = app_rec.approval_state or "draft"
    current_hash = sync_application_hash(app_rec)
    now = utcnow()
    app_rec.approved_content_hash = current_hash
    app_rec.approved_at = now
    app_rec.approval_state = "approved"
    if review_notes and review_notes.strip():
        app_rec.review_notes = review_notes.strip()[:4000]

    auto_reason = (
        review_notes.strip()
        if (review_notes and review_notes.strip())
        else f"Approved by user {user.id} ({user.email}) at {now.isoformat()}"
    )

    record_application_transition(
        db,
        user_id=user.id,
        application_id=app_rec.id,
        field_name="approval_state",
        from_state=prev_state,
        to_state="approved",
        actor=f"user:{user.id}",
        reason=auto_reason,
        metadata={
            "approved_content_hash": current_hash,
            "approved_by_user_id": user.id,
            "approved_by_email": user.email,
            "application_id": app_rec.id,
            "approved_at": now.isoformat(),
            "batch_id": batch.id if batch else app_rec.batch_id,
        },
    )
    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="draft_approved",
            target_type="application",
            target_id=app_rec.id,
            details_json={
                "approved_content_hash": current_hash,
                "approved_by_user_id": user.id,
                "approved_by_email": user.email,
                "application_id": app_rec.id,
                "approved_at": now.isoformat(),
                "tier": app_rec.source_verification_tier,
                "notes": (review_notes or "")[:200],
            },
        )
    )
    return True, f"Approved ({current_hash[:12]})"
