"""Durable outbox, rolling 24h recipient budgets, pre-send re-verification, and crash/timeout recovery."""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.metrics import metrics
from app.core.security import normalize_email_dedupe_key, sanitize_header_value
from app.db.base import as_utc, new_uuid, utcnow
from app.db.models import (
    Application,
    ApplicationTransition,
    AuditEvent,
    Batch,
    BudgetConsumption,
    CandidateProfile,
    CvExtractedFact,
    CvVersion,
    JobPosting,
    MailboxConnection,
    MessageEvent,
    OutboxEntry,
    RecipientHistory,
    SearchPreference,
    SupportingFile,
    User,
)
from app.services.approval import application_draft_hash, record_application_transition
from app.services.dates import is_in_quiet_hours_or_outside_window
from app.services.dedupe import (
    DuplicateRecipientError,
    mark_recipients_sent,
    release_reserved_recipients_if_pre_handoff_failed,
    reserve_recipients_atomically,
)
from app.services.effective_config import resolve_effective_live_sending
from app.services.eligibility import assess_posting_mandatory_eligibility
from app.services.mailbox.base import (
    HandoffOutcome,
    MailboxTransport,
    OutboundEmailMessage,
    SendAttachment,
    SendResult,
    build_rfc_message_id,
)
from app.services.mailbox.gmail import GmailMailboxTransport
from app.services.mailbox.ms_graph import MsGraphMailboxTransport
from app.services.mailbox.smtp_imap import SmtpImapMailboxTransport
from app.services.matching import evaluate_job_against_cv
from app.services.pdf_renderer import verify_pdf_bytes_readable
from app.services.storage import StorageAdapter, compute_sha256, get_storage_adapter
from app.services.verification import NOREPLY_LOCAL_PARTS, PLACEHOLDER_EMAILS, is_demo_email_or_domain


def get_mailbox_transport(db: Session, conn: MailboxConnection) -> MailboxTransport:
    if conn.provider == "gmail_oauth":
        return GmailMailboxTransport(db, conn)
    if conn.provider == "smtp_imap":
        return SmtpImapMailboxTransport(conn)
    if conn.provider == "ms_graph_oauth":
        return MsGraphMailboxTransport(db, conn)
    raise ValueError(f"Unsupported mailbox provider: {conn.provider}")


def count_rolling_24h_recipients(db: Session, *, user_id: str, mailbox_connection_id: str | None = None) -> int:
    cutoff = utcnow() - timedelta(hours=24)
    stmt = select(func.count(BudgetConsumption.id)).where(
        BudgetConsumption.user_id == user_id,
        BudgetConsumption.consumed_at >= cutoff,
    )
    if mailbox_connection_id:
        stmt = stmt.where(BudgetConsumption.mailbox_connection_id == mailbox_connection_id)
    return int(db.scalar(stmt) or 0)


def effective_daily_limit(pref: SearchPreference, conn: MailboxConnection) -> int:
    hard_cap = get_settings().max_daily_recipients_hard_cap
    candidates = [pref.daily_recipient_limit, hard_cap]
    if conn.daily_limit_override is not None and conn.daily_limit_override > 0:
        candidates.append(conn.daily_limit_override)
    return max(1, min(candidates))


def check_and_consume_rolling_budget(
    db: Session,
    *,
    user_id: str,
    mailbox_connection_id: str,
    application_id: str,
    outbox_entry_id: str,
    to_email: str,
    cc_emails: list[str],
    kind: str,
    limit: int,
) -> tuple[bool, int, int]:
    """
    Atomically verify rolling 24h budget for To + all CCs and insert consumption rows.
    Returns (allowed, current_used, needed_count).
    """
    recipients = [to_email, *cc_emails]
    needed = len([r for r in recipients if r])

    # Lock mailbox row on PostgreSQL to serialize per-mailbox budget reservation across concurrent workers
    conn_stmt = select(MailboxConnection).where(MailboxConnection.id == mailbox_connection_id)
    if db.bind and db.bind.dialect.name == "postgresql":
        conn_stmt = conn_stmt.with_for_update()
    db.scalar(conn_stmt)

    used = count_rolling_24h_recipients(db, user_id=user_id, mailbox_connection_id=mailbox_connection_id)
    if used + needed > limit:
        return False, used, needed

    now = utcnow()
    for idx, email_addr in enumerate(recipients):
        if not email_addr:
            continue
        row_kind = f"{kind}_{'to' if idx == 0 else 'cc'}"
        db.add(
            BudgetConsumption(
                user_id=user_id,
                mailbox_connection_id=mailbox_connection_id,
                application_id=application_id,
                outbox_entry_id=outbox_entry_id,
                recipient_normalized=normalize_email_dedupe_key(email_addr),
                kind=row_kind,
                consumed_at=now,
            )
        )
    db.flush()
    return True, used + needed, needed


def enqueue_application_for_send(
    db: Session,
    *,
    user: User,
    batch: Batch,
    app_record: Application,
    conn: MailboxConnection,
    expected_hash: str | None = None,
) -> tuple[OutboxEntry | None, str]:
    """
    Reserve recipients in `RecipientHistory` and enqueue an OutboxEntry for live transmission.
    Must NEVER be called for dry_run or demo_fixture batches.
    """
    if batch.mode != "live" or batch.status in ("paused", "cancelled"): 
        return None, "Non-live batch cannot enqueue outbox transmission"

    if batch.user_id != user.id or conn.user_id != user.id or app_record.user_id != user.id:
        return None, "Mailbox or application is unavailable"
    # Acquire a write lock even on SQLite, where SELECT FOR UPDATE is ignored.
    db.execute(update(Application).where(Application.id == app_record.id, Application.user_id == user.id)
               .values(updated_at=Application.updated_at).execution_options(synchronize_session=False))
    db.refresh(app_record)
    if app_record.sent_at or app_record.transport_status in ("sending", "send_uncertain", "sent", "provider_accepted"):
        return None, "Application has an active or completed handoff"
    # Enforce approval-lock, draft integrity hash, & third-party verification tier guards
    if (app_record.approval_state or "draft") in ("needs_human_review", "rejected", "superseded"):
        return None, f"Application approval state '{app_record.approval_state}' blocks automated sending"
    current_draft_hash = application_draft_hash(app_record)
    if expected_hash is not None and current_draft_hash != expected_hash:
        return None, "The draft changed since confirmation. Review it again."
    if getattr(batch, "run_mode", "") == "approval_required":
        if app_record.approval_state != "approved" or not app_record.approved_content_hash:
            return None, "Batch run_mode is approval_required; explicit approval required before queuing"
        if app_record.approved_content_hash != current_draft_hash:
            return None, "APPROVAL_HASH_MISMATCH: Draft was edited after approval; re-approval required"
    elif app_record.approved_content_hash and app_record.approved_content_hash != current_draft_hash:
        return None, "APPROVAL_HASH_MISMATCH: Draft content hash changed after approval"
    if (
        getattr(app_record, "source_verification_tier", "") == "tier_3_third_party_claim"
        and app_record.approval_state != "approved"
    ):
        return None, "Uncorroborated third-party claim requires human approval before live sending"

    # Server-side guard: never enqueue demo fixtures or .example recipients for real sending
    posting = db.scalar(select(JobPosting).where(JobPosting.id == app_record.job_posting_id))
    all_target_emails = [app_record.recipient_email, *(app_record.cc_emails_json or [])]
    if (
        app_record.is_demo
        or (posting and posting.is_demo)
        or any(is_demo_email_or_domain(em) for em in all_target_emails if em)
    ):
        reason = "Demo fixture / .example recipient cannot be enqueued for live sending"
        app_record.transport_status = "skipped"
        app_record.discovery_category = "demo_fixture"
        app_record.primary_skip_code = "DEMO_FIXTURE"
        app_record.skip_codes_json = ["DEMO_FIXTURE"]
        app_record.skip_or_error_reason = reason
        if posting:
            posting.discovery_category = "demo_fixture"
            posting.primary_skip_code = "DEMO_FIXTURE"
            posting.skip_codes_json = ["DEMO_FIXTURE"]
            posting.skip_reason = reason
        db.add(
            MessageEvent(
                user_id=user.id,
                application_id=app_record.id,
                event_type="cancelled",
                direction="internal",
                snippet=reason,
            )
        )
        db.commit()
        return None, reason

    # Reject before queuing if mandatory eligibility fails (AI fit score can never override mandatory gates)
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    cv_ver = (
        db.scalar(select(CvVersion).where(CvVersion.id == app_record.selected_cv_version_id))
        if app_record.selected_cv_version_id
        else None
    )
    if posting and profile:
        facts = db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == cv_ver.id)).all() if cv_ver else []
        _, elig_res = assess_posting_mandatory_eligibility(
            posting=posting,
            profile=profile,
            cv_version=cv_ver,
            facts=facts,
            check_cv_requirements=bool(cv_ver),
        )
        hard_fail_msgs = list(elig_res.failure_messages())
        hard_fail_codes = list(elig_res.failure_codes)
        is_gemini_import = getattr(app_record, "import_origin", "") == "gemini_zip"
        if pref and cv_ver and elig_res.passed and not is_gemini_import:
            ev = evaluate_job_against_cv(posting=posting, cv_version=cv_ver, facts=facts, pref=pref, profile=profile)
            if not ev.hard_filter_passed:
                hard_fail_msgs = list(ev.hard_filter_failures)
                hard_fail_codes = list(ev.failure_codes)
        if hard_fail_msgs:
            reason = f"Rejected before queuing: {'; '.join(hard_fail_msgs)}"
            app_record.transport_status = "skipped"
            app_record.discovery_category = "rejected"
            app_record.primary_skip_code = hard_fail_codes[0] if hard_fail_codes else "QUALIFICATION_MISMATCH"
            app_record.skip_codes_json = hard_fail_codes or ["QUALIFICATION_MISMATCH"]
            app_record.skip_or_error_reason = reason
            posting.discovery_category = "rejected"
            posting.primary_skip_code = app_record.primary_skip_code
            posting.skip_codes_json = app_record.skip_codes_json
            posting.skip_reason = reason
            db.add(
                MessageEvent(
                    user_id=user.id,
                    application_id=app_record.id,
                    event_type="cancelled",
                    direction="internal",
                    snippet=reason,
                )
            )
            db.commit()
            return None, reason

    if app_record.approval_state != "approved" or app_record.approved_content_hash != current_draft_hash:
        return None, "Explicit approval of this draft is required before queuing"

    # Enforce installation-wide kill switch at enqueue boundary as well as final dispatch boundary
    live_enabled, live_reason = resolve_effective_live_sending(db)
    if not live_enabled:
        return None, f"LIVE_SENDING_DISABLED: {live_reason or 'Installation live sending toggle is disabled'}"

    # Guard against duplicate active outbox entries for the same application and keep application status synchronized
    existing_ob = db.scalar(
        select(OutboxEntry)
        .where(
            OutboxEntry.application_id == app_record.id,
            OutboxEntry.user_id == user.id,
            OutboxEntry.status.in_(["queued", "leased", "sending", "sent", "send_uncertain"]),
        )
        .order_by(OutboxEntry.created_at.desc())
    )
    if existing_ob:
        mapped_status = "queued" if existing_ob.status in ("queued", "leased") else existing_ob.status
        if app_record.transport_status != mapped_status:
            prev_ts = app_record.transport_status
            app_record.transport_status = mapped_status
            record_application_transition(
                db,
                user_id=user.id,
                application_id=app_record.id,
                field_name="transport_status",
                from_state=prev_ts,
                to_state=mapped_status,
                actor="system:outbox_sync",
                reason=f"Synchronized transport_status with existing outbox entry ({existing_ob.status})",
                metadata={"outbox_entry_id": existing_ob.id},
            )
        if existing_ob.last_error and not app_record.skip_or_error_reason:
            app_record.skip_or_error_reason = existing_ob.last_error
        db.commit()
        detail = f" — {existing_ob.last_error}" if existing_ob.last_error else ""
        return None, f"Application already has an active or completed outbox entry ({existing_ob.status}){detail}"

    try:
        reserve_recipients_atomically(
            db,
            user_id=user.id,
            application_id=app_record.id,
            to_email=app_record.recipient_email,
            cc_emails=app_record.cc_emails_json or [],
            company_name=app_record.company_name,
            reject_demo=True,
        )
    except DuplicateRecipientError as exc:
        app_record.transport_status = "skipped"
        app_record.skip_or_error_reason = str(exc)
        db.add(
            MessageEvent(
                user_id=user.id,
                application_id=app_record.id,
                event_type="cancelled",
                direction="internal",
                snippet=str(exc),
            )
        )
        db.commit()
        return None, str(exc)

    initial_window_note = ""
    if pref and get_settings().environment != "test":
        win_blocked, win_reason = is_in_quiet_hours_or_outside_window(
            now_utc=utcnow(),
            tz_name=pref.timezone or "UTC",
            window_start_hhmm=pref.send_window_start_hhmm,
            window_end_hhmm=pref.send_window_end_hhmm,
            quiet_start_hhmm=pref.quiet_hours_start_hhmm,
            quiet_end_hhmm=pref.quiet_hours_end_hhmm,
        )
        if win_blocked:
            initial_window_note = win_reason

    action_id = new_uuid()
    rfc_id = build_rfc_message_id(conn.sender_email, action_id)
    outbox = OutboxEntry(
        user_id=user.id,
        application_id=app_record.id,
        mailbox_connection_id=conn.id,
        kind="initial",
        action_id=action_id,
        rfc_message_id=rfc_id,
        status="queued",
        last_error=initial_window_note,
    )
    prev_transport = app_record.transport_status
    app_record.transport_status = "queued"
    app_record.skip_or_error_reason = initial_window_note
    app_record.rfc_message_id = rfc_id
    app_record.mailbox_connection_id = conn.id
    db.add(outbox)
    db.flush()
    record_application_transition(
        db,
        user_id=user.id,
        application_id=app_record.id,
        field_name="transport_status",
        from_state=prev_transport,
        to_state="queued",
        actor=f"user:{user.id}",
        reason=initial_window_note or "Enqueued in outbox for sending",
        metadata={"outbox_entry_id": outbox.id, "action_id": action_id, "confirmed_hash": current_draft_hash},
    )
    db.add(
        MessageEvent(
            user_id=user.id,
            application_id=app_record.id,
            event_type="reserved",
            direction="internal",
            rfc_message_id=rfc_id,
            snippet=f"Reserved recipient {app_record.recipient_email} and queued outbox action {action_id}",
        )
    )
    db.commit()
    db.refresh(outbox)
    return outbox, ""


def claim_next_outbox_entry(db: Session, *, worker_id: str, outbox_id: str | None = None) -> OutboxEntry | None:
    """Atomically claim a queued outbox entry with a bounded lease."""
    now = utcnow()
    lease_sec = get_settings().outbox_lease_seconds
    stmt = select(OutboxEntry).where(
        OutboxEntry.status == "queued",
        OutboxEntry.next_attempt_at <= now,
    )
    if outbox_id:
        stmt = stmt.where(OutboxEntry.id == outbox_id)
    stmt = stmt.order_by(OutboxEntry.created_at.asc()).limit(1)
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    entry = db.scalar(stmt)
    if not entry:
        return None

    # Atomic conditional update ensures single-owner lease even under SQLite/concurrent polls
    lease_token = f"{worker_id[:80]}:{new_uuid()}"
    updated = (
        db.query(OutboxEntry)
        .filter(OutboxEntry.id == entry.id, OutboxEntry.status == "queued")
        .update(
            {
                "status": "leased",
                "lease_owner": lease_token,
                "lease_expires_at": now + timedelta(seconds=lease_sec),
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if updated != 1:
        return None
    db.refresh(entry)
    return entry if entry.lease_owner == lease_token else None


def _record_followup_acceptance(db, app_rec, entry, result, now):
    """Record this action without replacing identifiers or counters for the initial submission."""
    app_rec.follow_up_count += 1
    app_rec.last_follow_up_at = now
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == entry.user_id))
    app_rec.next_follow_up_at = (now + timedelta(days=7) if pref and app_rec.follow_up_count < min(pref.followup_max_count, 2)
                                 and app_rec.outcome_status == "open" and app_rec.response_status not in ("human_reply", "permanent_bounce")
                                 else None)
    db.add(MessageEvent(user_id=entry.user_id, application_id=app_rec.id, direction="outbound",
                        event_type="followup_sent", provider_message_id=result.provider_message_id,
                        rfc_message_id=entry.rfc_message_id, details_json={"provider_thread_id": result.provider_thread_id},
                        snippet="Follow-up accepted by provider"))


def recover_stale_outbox_leases(
    db: Session,
    *,
    transport_factory=get_mailbox_transport,
) -> dict[str, int]:
    """
    Crash & timeout recovery for expired outbox leases:
    - If expired while `leased` (before handoff started): return to `queued` unless cancelled/stopped.
    - If expired while `sending` (crash/timeout during external handoff): NEVER blindly reset to `queued`!
      Try provider reconciliation by `rfc_message_id`; otherwise mark `send_uncertain`.
    """
    now = utcnow()
    stats = {"requeued_leased": 0, "reconciled_sent": 0, "marked_uncertain": 0, "cancelled": 0}
    expired = db.scalars(
        select(OutboxEntry).where(
            OutboxEntry.status.in_(["leased", "sending"]),
            OutboxEntry.lease_expires_at < now,
        )
    ).all()

    for entry in expired:
        if entry.status not in ("leased", "sending"):
            continue
        app_rec = db.scalar(select(Application).where(Application.id == entry.application_id, Application.user_id == entry.user_id))
        fenced = db.execute(update(OutboxEntry).where(
            OutboxEntry.id == entry.id, OutboxEntry.status == entry.status,
            OutboxEntry.lease_owner == entry.lease_owner, OutboxEntry.lease_expires_at < now,
        ).values(updated_at=now).execution_options(synchronize_session=False))
        if fenced.rowcount != 1:
            db.rollback()
            continue
        batch = db.scalar(select(Batch).where(Batch.id == app_rec.batch_id, Batch.user_id == entry.user_id)) if app_rec else None
        user = db.scalar(select(User).where(User.id == entry.user_id))

        if not user or user.deleted_at or user.is_suspended or user.automation_stopped_at or (batch and batch.status in ("cancelled", "paused")):
            if entry.status == "leased":
                entry.status = "cancelled"
                if entry.kind == "initial" and app_rec and app_rec.transport_status in ("ready", "queued", "sending"):
                    app_rec.transport_status = "cancelled"
                    release_reserved_recipients_if_pre_handoff_failed(db, user_id=entry.user_id, application_id=app_rec.id)
                stats["cancelled"] += 1
                db.commit()
                continue

        if entry.status == "leased":
            entry.status = "queued"
            entry.lease_owner = None
            entry.lease_expires_at = None
            if entry.kind == "initial" and app_rec and app_rec.transport_status in ("ready", "draft", "leased"):
                app_rec.transport_status = "queued"
            stats["requeued_leased"] += 1
            db.commit()
            continue

        # status == "sending": handoff was initiated before crash/timeout!
        conn = db.scalar(select(MailboxConnection).where(MailboxConnection.id == entry.mailbox_connection_id, MailboxConnection.user_id == entry.user_id))
        action_id, lease_owner, rfc_id = entry.action_id, entry.lease_owner, entry.rfc_message_id
        db.commit()  # Provider lookup can refresh tokens and commit. Reacquire ownership after I/O.
        reconciled: SendResult | None = None
        if conn:
            try:
                transport = transport_factory(db, conn)
                reconciled = transport.reconcile_sent_message(rfc_id, action_id)
            except Exception:
                reconciled = None

        won = db.execute(update(OutboxEntry).where(
            OutboxEntry.id == entry.id, OutboxEntry.action_id == action_id,
            OutboxEntry.lease_owner == lease_owner, OutboxEntry.status == "sending",
            OutboxEntry.lease_expires_at < now,
        ).values(updated_at=utcnow()).execution_options(synchronize_session=False))
        if won.rowcount != 1:
            db.rollback()
            continue
        db.refresh(entry)
        if app_rec:
            db.refresh(app_rec)
        if reconciled and reconciled.outcome == HandoffOutcome.PROVIDER_ACCEPTED:
            entry.status = "sent"
            entry.handoff_outcome = HandoffOutcome.PROVIDER_ACCEPTED.value
            if app_rec and entry.kind == "followup":
                _record_followup_acceptance(db, app_rec, entry, reconciled, now)
            elif app_rec:
                app_rec.transport_status = "sent"
                app_rec.provider_message_id = reconciled.provider_message_id
                app_rec.provider_thread_id = reconciled.provider_thread_id
                app_rec.sent_at = app_rec.sent_at or now
                app_rec.next_follow_up_at = as_utc(app_rec.sent_at) + timedelta(days=7)
                if batch:
                    batch.sent_count = Batch.sent_count + 1
                mark_recipients_sent(db, user_id=entry.user_id, emails=[app_rec.recipient_email, *(app_rec.cc_emails_json or [])])
                db.add(
                    MessageEvent(
                        user_id=entry.user_id,
                        application_id=app_rec.id,
                        event_type="reconciled",
                        direction="internal",
                        provider_message_id=reconciled.provider_message_id,
                        rfc_message_id=entry.rfc_message_id,
                        snippet="Recovered crashed send attempt via provider sent-message reconciliation",
                    )
                )
            stats["reconciled_sent"] += 1
        else:
            entry.status = "send_uncertain"
            entry.handoff_outcome = HandoffOutcome.AMBIGUOUS_HANDOFF.value
            entry.last_error = "Worker lease expired during external send handoff; held as send_uncertain to prevent duplicate mail"
            if app_rec and entry.kind == "initial":
                app_rec.transport_status = "send_uncertain"
                app_rec.skip_or_error_reason = entry.last_error
                if batch:
                    batch.uncertain_count = Batch.uncertain_count + 1
                db.add(
                    MessageEvent(
                        user_id=entry.user_id,
                        application_id=app_rec.id,
                        event_type="send_uncertain",
                        direction="internal",
                        rfc_message_id=entry.rfc_message_id,
                        snippet=entry.last_error,
                    )
                )
            metrics.inc("sends_uncertain")
            stats["marked_uncertain"] += 1
        db.commit()
    return stats


def _load_verified_attachments(
    db: Session,
    *,
    app_rec: Application,
    storage: StorageAdapter,
) -> list[SendAttachment]:
    if not app_rec.selected_cv_version_id:
        raise ValueError("Application has no selected CV version")
    cv_ver = db.scalar(
        select(CvVersion).where(
            CvVersion.id == app_rec.selected_cv_version_id,
            CvVersion.user_id == app_rec.user_id,
        )
    )
    if not cv_ver:
        raise ValueError("Selected CV version missing")
    cv_bytes = storage.get_bytes(cv_ver.storage_key)
    actual_sha = compute_sha256(cv_bytes)
    if actual_sha != app_rec.frozen_cv_sha256 or actual_sha != cv_ver.sha256:
        raise ValueError("CV file checksum mismatch against frozen application snapshot")

    send_bytes = cv_bytes
    send_mime = cv_ver.mime_type
    send_name = app_rec.frozen_cv_filename or cv_ver.original_filename
    send_sha = actual_sha

    if cv_ver.mime_type != "application/pdf":
        if (cv_ver.pdf_conversion_status != "ready" or not cv_ver.derived_pdf_storage_key
                or not app_rec.attachment_pdf_sha256):
            raise ValueError("Selected CV has no verified PDF attachment")
        pdf_bytes = storage.get_bytes(cv_ver.derived_pdf_storage_key)
        pdf_sha = compute_sha256(pdf_bytes)
        if pdf_sha != app_rec.attachment_pdf_sha256 or pdf_sha != cv_ver.derived_pdf_sha256:
            raise ValueError("Derived PDF checksum mismatch against frozen application snapshot")
        send_bytes = pdf_bytes
        send_mime = "application/pdf"
        if not send_name.lower().endswith(".pdf"):
            send_name = f"{send_name.rsplit('.', 1)[0]}.pdf"
        send_sha = pdf_sha

    if app_rec.attachment_pdf_sha256 and send_sha != app_rec.attachment_pdf_sha256:
        raise ValueError("PDF differs from the approved attachment")
    verify_pdf_bytes_readable(send_bytes, min_chars=0)

    attachments = [
        SendAttachment(
            filename=send_name,
            mime_type=send_mime,
            data=send_bytes,
            expected_sha256=send_sha,
        )
    ]

    for sf_id in app_rec.supporting_file_ids_json or []:
        sf = db.scalar(
            select(SupportingFile).where(
                SupportingFile.id == sf_id,
                SupportingFile.user_id == app_rec.user_id,
            )
        )
        if not sf:
            raise ValueError("Supporting file unavailable")
        sf_bytes = storage.get_bytes(sf.storage_key)
        if compute_sha256(sf_bytes) != sf.sha256:
            raise ValueError(f"Supporting file '{sf.original_filename}' checksum mismatch")
        attachments.append(
            SendAttachment(
                filename=sf.original_filename,
                mime_type=sf.mime_type,
                data=sf_bytes,
                expected_sha256=sf.sha256,
            )
        )
    return attachments


def process_outbox_entry(
    db: Session,
    *,
    entry: OutboxEntry,
    transport_override: MailboxTransport | None = None,
    storage_override: StorageAdapter | None = None,
    ignore_window_for_test: bool = False,
) -> str:
    """
    Execute a leased outbox entry with final pre-send rechecks, rolling budget enforcement,
    frozen attachment SHA-256 verification, and safe 4-outcome handling.
    """
    settings = get_settings()
    now = utcnow()
    # Serialize the final checks with edits and lease recovery. A stale object from
    # a previous claim must never authorize an external handoff.
    if not entry.lease_owner or entry.status != "leased":
        return "claim_lost"
    app_rec = db.scalar(select(Application).where(
        Application.id == entry.application_id, Application.user_id == entry.user_id,
    ).with_for_update().execution_options(populate_existing=True))
    fenced = db.execute(update(OutboxEntry).where(
        OutboxEntry.id == entry.id, OutboxEntry.status == "leased",
        OutboxEntry.lease_owner == entry.lease_owner, OutboxEntry.lease_expires_at > now,
    ).values(updated_at=now).execution_options(synchronize_session=False))
    if fenced.rowcount != 1:
        db.rollback()
        return "claim_lost"
    user = db.scalar(select(User).where(User.id == entry.user_id))
    batch = db.scalar(select(Batch).where(Batch.id == app_rec.batch_id, Batch.user_id == entry.user_id)) if app_rec else None
    conn = db.scalar(select(MailboxConnection).where(
        MailboxConnection.id == entry.mailbox_connection_id, MailboxConnection.user_id == entry.user_id,
    ))
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == entry.user_id))
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == entry.user_id))

    is_followup = entry.kind == "followup"

    def set_initial_status(status, reason):
        if not is_followup:
            app_rec.transport_status = status
            app_rec.skip_or_error_reason = reason

    def _cancel_before_handoff(reason: str, status_str: str = "cancelled") -> str:
        entry.status = "cancelled"
        entry.last_error = reason
        if app_rec:
            set_initial_status(status_str, reason)
            if entry.kind == "initial":
                release_reserved_recipients_if_pre_handoff_failed(db, user_id=entry.user_id, application_id=app_rec.id)
            db.add(
                MessageEvent(
                    user_id=entry.user_id,
                    application_id=app_rec.id,
                    event_type="cancelled",
                    direction="internal",
                    snippet=reason,
                )
            )
        db.commit()
        return status_str

    if not user or user.deleted_at or user.is_suspended:
        return _cancel_before_handoff("Account deleted or suspended prior to send")
    if user.automation_stopped_at is not None:
        return _cancel_before_handoff("Global Stop Automation is active")
    if not app_rec or not batch or not pref or not profile:
        return _cancel_before_handoff("Required application, batch, or preference record missing")
    if batch.mode != "live":
        return _cancel_before_handoff("Non-live batch blocked from external sending")
    posting_chk = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id)) if app_rec.job_posting_id else None
    all_target_emails = [app_rec.recipient_email, *(app_rec.cc_emails_json or [])]
    if (
        app_rec.is_demo
        or (posting_chk and posting_chk.is_demo)
        or any(is_demo_email_or_domain(em) for em in all_target_emails if em)
    ):
        app_rec.discovery_category = "demo_fixture"
        app_rec.primary_skip_code = "DEMO_FIXTURE"
        app_rec.skip_codes_json = ["DEMO_FIXTURE"]
        return _cancel_before_handoff("Demo fixture / .example recipient blocked from external sending", status_str="skipped")
    live_enabled, _ = resolve_effective_live_sending(db)
    if not live_enabled:
        if is_followup or getattr(app_rec, "approval_state", "") == "approved":
            entry.status = "queued"
            entry.next_attempt_at = now + timedelta(minutes=15)
            entry.lease_owner = None
            entry.lease_expires_at = None
            if not entry.last_error:
                entry.last_error = "Paused while Live Sending is disabled in Settings"
            set_initial_status("queued", entry.last_error)
            db.commit()
            return "paused_live_disabled"
        return _cancel_before_handoff("Installation live sending toggle is disabled", status_str="skipped")
    if batch.status in ("cancelled", "paused"):
        return _cancel_before_handoff(f"Batch is {batch.status}; queued work halted")
    if not conn or conn.status != "connected" or conn.revoked_at is not None:
        return _cancel_before_handoff("Mailbox disconnected or credentials revoked", status_str="failed")
    if app_rec.outcome_status in ("withdrawn", "rejected", "interview"):
        return _cancel_before_handoff(f"Application outcome is '{app_rec.outcome_status}'")

    targets = {normalize_email_dedupe_key(em) for em in all_target_emails}
    if (targets.intersection(pref.excluded_recipients_json or [])
            or any(key.partition("@")[0] in NOREPLY_LOCAL_PARTS or key in PLACEHOLDER_EMAILS for key in targets)):
        return _cancel_before_handoff("Blocked or automated recipient", status_str="skipped")
    # Check suppression list
    suppressed = db.scalar(
        select(RecipientHistory).where(
            RecipientHistory.user_id == entry.user_id,
            RecipientHistory.normalized_email.in_(targets),
            RecipientHistory.source == "suppressed",
        )
    )
    if suppressed:
        return _cancel_before_handoff("Recipient is on user suppression list", status_str="skipped")

    payload = {}
    if is_followup:
        from app.services.followup import approved_followup_payload
        try:
            payload = approved_followup_payload(db, app_rec, entry, conn)
        except ValueError as exc:
            return _cancel_before_handoff(str(exc))

    # Pre-handoff draft integrity & Gmail Sent dedup check
    if entry.kind == "initial":
        from app.services.dedupe import find_existing_recipients

        cur_hash = application_draft_hash(app_rec)
        if app_rec.approval_state in ("needs_human_review", "rejected", "superseded"):
            return _cancel_before_handoff(
                f"Pre-send check blocked because approval_state is '{app_rec.approval_state}'",
                status_str="cancelled",
            )
        if (
            app_rec.approval_state != "approved" or not app_rec.approved_content_hash
        ):
            return _cancel_before_handoff("Approval required before sending", status_str="cancelled")
        if app_rec.approved_content_hash and app_rec.approved_content_hash != cur_hash:
            return _cancel_before_handoff(
                "APPROVAL_HASH_MISMATCH: Draft was edited after approval",
                status_str="cancelled",
            )
        if batch.run_mode == "autopilot":
            manual_approval = db.scalar(select(ApplicationTransition.id).where(
                ApplicationTransition.user_id == user.id, ApplicationTransition.application_id == app_rec.id,
                ApplicationTransition.field_name == "approval_state", ApplicationTransition.to_state == "approved",
                ApplicationTransition.actor == f"user:{user.id}",
            ))
            if not manual_approval:
                return _cancel_before_handoff("Review and approve this legacy automatic draft before sending")
        existing_sent = find_existing_recipients(
            db,
            user_id=entry.user_id,
            emails=[app_rec.recipient_email, *(app_rec.cc_emails_json or [])],
            exclude_application_id=app_rec.id,
        )
        if existing_sent:
            return _cancel_before_handoff(
                f"Previously contacted in ledger/Gmail Sent ({', '.join(existing_sent.keys())})",
                status_str="skipped",
            )

    # Re-run mandatory eligibility & hard eligibility filters immediately before initial send
    if entry.kind == "initial":
        posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id))
        cv_ver = (
            db.scalar(select(CvVersion).where(CvVersion.id == app_rec.selected_cv_version_id))
            if app_rec.selected_cv_version_id
            else None
        )
        if posting:
            facts = db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == cv_ver.id)).all() if cv_ver else []
            _, elig_res = assess_posting_mandatory_eligibility(
                posting=posting,
                profile=profile,
                cv_version=cv_ver,
                facts=facts,
                check_cv_requirements=bool(cv_ver),
            )
            if not elig_res.passed:
                reason = f"Pre-send mandatory eligibility check failed: {'; '.join(elig_res.failure_messages())}"
                app_rec.discovery_category = "rejected"
                app_rec.primary_skip_code = elig_res.failure_codes[0]
                app_rec.skip_codes_json = list(elig_res.failure_codes)
                posting.discovery_category = "rejected"
                posting.primary_skip_code = elig_res.failure_codes[0]
                posting.skip_codes_json = list(elig_res.failure_codes)
                posting.skip_reason = reason
                return _cancel_before_handoff(reason, status_str="skipped")

            is_gemini_import = getattr(app_rec, "import_origin", "") == "gemini_zip"
            if cv_ver and not is_gemini_import:
                ev = evaluate_job_against_cv(
                    posting=posting,
                    cv_version=cv_ver,
                    facts=facts,
                    pref=pref,
                    profile=profile,
                    now_utc=now,
                )
                if not ev.hard_filter_passed:
                    reason = f"Pre-send hard filter recheck failed: {'; '.join(ev.hard_filter_failures)}"
                    f_codes = list(ev.failure_codes) or ["QUALIFICATION_MISMATCH"]
                    app_rec.discovery_category = "rejected"
                    app_rec.primary_skip_code = f_codes[0]
                    app_rec.skip_codes_json = f_codes
                    posting.discovery_category = "rejected"
                    posting.primary_skip_code = f_codes[0]
                    posting.skip_codes_json = f_codes
                    posting.skip_reason = reason
                    return _cancel_before_handoff(reason, status_str="skipped")
            if posting.closing_at:
                closing = as_utc(posting.closing_at)
                if closing and closing < now:
                    return _cancel_before_handoff("Job closing date passed before transmission", status_str="skipped")

    # Quiet hours & sending window
    if not (ignore_window_for_test and settings.environment == "test"):
        blocked, win_reason = is_in_quiet_hours_or_outside_window(
            now_utc=now,
            tz_name=pref.timezone or "UTC",
            window_start_hhmm=pref.send_window_start_hhmm,
            window_end_hhmm=pref.send_window_end_hhmm,
            quiet_start_hhmm=pref.quiet_hours_start_hhmm,
            quiet_end_hhmm=pref.quiet_hours_end_hhmm,
        )
        if blocked:
            entry.status = "queued"
            entry.next_attempt_at = now + timedelta(minutes=15)
            entry.lease_owner = None
            entry.lease_expires_at = None
            entry.last_error = win_reason
            set_initial_status("queued", win_reason)
            db.commit()
            return "deferred_window"

    # Load and hash-verify frozen CV bytes
    storage = storage_override or get_storage_adapter()
    try:
        attachments = _load_verified_attachments(db, app_rec=app_rec, storage=storage) if entry.kind == "initial" else []
    except Exception as exc:
        return _cancel_before_handoff(f"Attachment verification failed: {exc}", status_str="failed")

    # Atomic rolling 24h budget reservation
    limit = effective_daily_limit(pref, conn)
    allowed, used_after, needed = check_and_consume_rolling_budget(
        db,
        user_id=entry.user_id,
        mailbox_connection_id=conn.id,
        application_id=app_rec.id,
        outbox_entry_id=entry.id,
        to_email=app_rec.recipient_email,
        cc_emails=app_rec.cc_emails_json or [] if entry.kind == "initial" else [],
        kind=entry.kind,
        limit=limit,
    )
    if not allowed:
        entry.status = "queued"
        entry.next_attempt_at = now + timedelta(hours=1)
        entry.lease_owner = None
        entry.lease_expires_at = None
        entry.last_error = f"Rolling 24h recipient budget reached ({used_after}/{limit}, needed {needed})"
        set_initial_status("queued", entry.last_error)
        # Send deferrals belong to the outbox. Completed discovery stays completed.
        if batch.status == "running" and not batch.discovery_stage:
            batch.status = "quota_paused"
            batch.pause_reason = entry.last_error
        metrics.inc("quota_pauses")
        db.commit()
        return "quota_paused"

    # Transition to `sending` BEFORE external network handoff so crash recovery knows handoff started!
    entry.status = "sending"
    entry.attempt_count += 1
    entry.lease_expires_at = now + timedelta(seconds=settings.outbox_lease_seconds)
    if not is_followup:
        app_rec.transport_status = "sending"
    db.add(
        MessageEvent(
            user_id=entry.user_id,
            application_id=app_rec.id,
            event_type="send_attempted",
            direction="outbound",
            rfc_message_id=entry.rfc_message_id,
            snippet=f"Attempt {entry.attempt_count} via {conn.provider}",
        )
    )
    db.commit()

    if settings.send_pace_seconds > 0 and settings.environment != "test":
        time.sleep(min(settings.send_pace_seconds, 10.0))

    # Build outbound email message
    subj = payload["subject"] if is_followup else app_rec.subject
    body = payload["body"] if is_followup else app_rec.body_text

    outbound = OutboundEmailMessage(
        from_email=payload["sender"] if is_followup else conn.sender_email,
        from_name=payload["from_name"] if is_followup else profile.full_name,
        to_email=payload["recipient"] if is_followup else app_rec.recipient_email,
        cc_emails=(app_rec.cc_emails_json or []) if not is_followup else [],
        subject=subj,
        body_text=body,
        rfc_message_id=entry.rfc_message_id,
        attachments=attachments,
        thread_id=payload["thread"] if is_followup else "",
        in_reply_to=payload["parent"] if is_followup else "",
        references=payload["parent"] if is_followup else "",
        custom_action_id=entry.action_id,
    )

    claim_token = entry.lease_owner
    transport = transport_override or get_mailbox_transport(db, conn)
    try:
        result = transport.send_message(outbound)
    except Exception:
        # The provider may have accepted the message before the connection broke.
        result = SendResult(outcome=HandoffOutcome.AMBIGUOUS_HANDOFF, rfc_message_id=entry.rfc_message_id,
                            error_message="Connection interrupted during provider handoff")


    # Recovery can reconcile a slow external call before the original worker returns.
    # An acceptance may resolve uncertainty. A late rejection may never unlock replay.
    allowed_states = ["sending"]
    if result.outcome == HandoffOutcome.PROVIDER_ACCEPTED:
        allowed_states.append("send_uncertain")
    accepted_result = db.execute(update(OutboxEntry).where(
        OutboxEntry.id == entry.id, OutboxEntry.lease_owner == claim_token,
        OutboxEntry.status.in_(allowed_states),
    ).values(updated_at=utcnow()).execution_options(synchronize_session=False))
    if accepted_result.rowcount != 1:
        db.rollback()
        db.refresh(entry)
        return entry.status
    db.refresh(entry)
    if entry.kind == "initial" and entry.status == "send_uncertain":
        batch.uncertain_count = case((Batch.uncertain_count > 0, Batch.uncertain_count - 1), else_=0)

    if result.outcome == HandoffOutcome.PROVIDER_ACCEPTED:
        now = utcnow()
        entry.status = "sent"
        entry.handoff_outcome = HandoffOutcome.PROVIDER_ACCEPTED.value
        entry.last_error = ""
        if is_followup:
            _record_followup_acceptance(db, app_rec, entry, result, now)
        else:
            app_rec.transport_status = "sent"
            app_rec.skip_or_error_reason = ""
            app_rec.provider_message_id = result.provider_message_id
            app_rec.provider_thread_id = result.provider_thread_id or app_rec.provider_thread_id
            app_rec.sent_at = now
            app_rec.next_follow_up_at = now + timedelta(days=7)
            batch.sent_count = Batch.sent_count + 1
            mark_recipients_sent(db, user_id=entry.user_id, emails=[app_rec.recipient_email, *(app_rec.cc_emails_json or [])])
            db.add(
                MessageEvent(
                    user_id=entry.user_id,
                    application_id=app_rec.id,
                    event_type="sent",
                    direction="outbound",
                    provider_message_id=result.provider_message_id,
                    rfc_message_id=entry.rfc_message_id,
                    subject=subj,
                    snippet="Accepted by mail provider",
                )
            )
        metrics.inc("sends_accepted")
        db.commit()
        return "sent"

    if result.outcome == HandoffOutcome.AMBIGUOUS_HANDOFF:
        # NEVER blindly retry an ambiguous handoff!
        entry.status = "send_uncertain"
        entry.handoff_outcome = HandoffOutcome.AMBIGUOUS_HANDOFF.value
        entry.last_error = result.error_message or "Ambiguous provider handoff"
        set_initial_status("send_uncertain", entry.last_error)
        if not is_followup:
            batch.uncertain_count = Batch.uncertain_count + 1
        db.add(
            MessageEvent(
                user_id=entry.user_id,
                application_id=app_rec.id,
                event_type="send_uncertain",
                direction="internal",
                rfc_message_id=entry.rfc_message_id,
                snippet=entry.last_error,
            )
        )
        metrics.inc("sends_uncertain")
        db.commit()
        return "send_uncertain"

    if result.outcome == HandoffOutcome.PROVIDER_REJECTED and result.is_rate_limit and entry.attempt_count < entry.max_attempts:
        delay = max(60, result.retry_after_seconds or 300)
        entry.status = "queued"
        entry.lease_owner = None
        entry.lease_expires_at = None
        entry.next_attempt_at = now + timedelta(seconds=delay)
        entry.handoff_outcome = HandoffOutcome.PROVIDER_REJECTED.value
        entry.last_error = f"Provider rate limited; backed off for {delay}s: {result.error_message}"
        set_initial_status("queued", entry.last_error)
        # Send deferrals belong to the outbox. Completed discovery stays completed.
        if batch.status == "running" and not batch.discovery_stage:
            batch.status = "quota_paused"
            batch.pause_reason = entry.last_error
        metrics.inc("quota_pauses")
        db.commit()
        return "rate_limited"

    # Explicit rejection or pre-handoff failure
    entry.handoff_outcome = result.outcome.value
    entry.last_error = result.error_message
    if result.outcome == HandoffOutcome.PRE_HANDOFF_FAILURE and entry.attempt_count < entry.max_attempts:
        backoff = 60 * (2 ** entry.attempt_count)
        entry.status = "queued"
        entry.lease_owner = None
        entry.lease_expires_at = None
        entry.next_attempt_at = now + timedelta(seconds=backoff)
        set_initial_status("queued", entry.last_error)
        db.commit()
        return "retry_scheduled"

    entry.status = "failed"
    set_initial_status("failed", result.error_message)
    if entry.kind == "initial" and result.outcome in (
        HandoffOutcome.PRE_HANDOFF_FAILURE,
        HandoffOutcome.PROVIDER_REJECTED,
    ):
        release_reserved_recipients_if_pre_handoff_failed(db, user_id=entry.user_id, application_id=app_rec.id)
    if result.error_code in ("SMTP_AUTH_FAILED", "HTTP_401", "HTTP_403"):
        conn.status = "error"
        conn.last_error = result.error_message
        batch.status = "paused"
        batch.pause_reason = "Mailbox authentication failed; please reconnect your mailbox"
    db.add(
        AuditEvent(
            user_id=entry.user_id,
            action="outbox_send_failed",
            target_type="application",
            target_id=app_rec.id,
            details_json={"outcome": result.outcome.value, "error": result.error_message[:300]},
        )
    )
    db.commit()
    return "failed"


def send_owner_self_test_email(
    db: Session,
    *,
    user: User,
    conn: MailboxConnection,
    recipient_email: str | None = None,
    cv_version_id: str | None = None,
    subject: str = "ApplyFlow Connected Mailbox Self-Test",
    note: str = "",
    transport_override: MailboxTransport | None = None,
    storage_override: StorageAdapter | None = None,
) -> dict[str, object]:
    """
    Send ONE explicitly owner-authorized self-test email through the connected sending mailbox.
    Strictly locked to the owner's own connected mailbox or account email address — never contacts third parties.
    """
    if conn.status != "connected" or conn.revoked_at is not None:
        raise ValueError("Connected mailbox is not active")

    allowed_targets = {
        normalize_email_dedupe_key(em)
        for em in (conn.sender_email, user.email)
        if em and "@" in em
    }
    target = (recipient_email or conn.sender_email or user.email or "").strip()
    if not target or normalize_email_dedupe_key(target) not in allowed_targets:
        raise ValueError(
            "SELF_TEST_ONLY: Owner self-test emails may only be sent to your own connected mailbox or ApplyFlow account email"
        )

    storage = storage_override or get_storage_adapter()
    attachments: list[SendAttachment] = []
    attached_filename = ""
    attached_sha256 = ""
    if cv_version_id:
        cv_ver = db.scalar(select(CvVersion).where(CvVersion.id == cv_version_id, CvVersion.user_id == user.id))
        if not cv_ver:
            raise ValueError("Selected CV version not found for current user")
        if cv_ver.derived_pdf_storage_key and cv_ver.derived_pdf_sha256:
            pdf_bytes = storage.get_bytes(cv_ver.derived_pdf_storage_key)
            if compute_sha256(pdf_bytes) != cv_ver.derived_pdf_sha256:
                raise ValueError("CV derived PDF integrity checksum mismatch")
            attached_filename = (
                cv_ver.original_filename
                if cv_ver.original_filename.lower().endswith(".pdf")
                else f"{cv_ver.original_filename.rsplit('.', 1)[0]}.pdf"
            )
            attached_sha256 = cv_ver.derived_pdf_sha256
        else:
            pdf_bytes = storage.get_bytes(cv_ver.storage_key)
            if compute_sha256(pdf_bytes) != cv_ver.sha256:
                raise ValueError("CV file integrity checksum mismatch")
            attached_filename = cv_ver.original_filename
            attached_sha256 = cv_ver.sha256
        attachments.append(
            SendAttachment(
                filename=attached_filename,
                mime_type="application/pdf" if attached_filename.lower().endswith(".pdf") else cv_ver.mime_type,
                data=pdf_bytes,
                expected_sha256=attached_sha256,
            )
        )

    action_id = f"selftest-{new_uuid()}"
    rfc_msg_id = build_rfc_message_id(conn.sender_email, action_id)
    body_lines = [
        "This is an explicitly authorized ApplyFlow Global self-test email.",
        "",
        f"Connected mailbox provider: {conn.provider}",
        f"Sender / Self-test target: {target}",
        f"Action ID: {action_id}",
    ]
    if attached_filename:
        body_lines.append(f"Attached PDF CV: {attached_filename} (SHA-256: {attached_sha256})")
    if note.strip():
        body_lines.extend(["", f"Owner note: {note.strip()}"])
    body_lines.extend(["", "No third-party employer was contacted."])

    outbound = OutboundEmailMessage(
        from_email=conn.sender_email,
        from_name=user.full_name or "",
        to_email=target,
        cc_emails=[],
        subject=sanitize_header_value(subject, 200) or "ApplyFlow Connected Mailbox Self-Test",
        body_text="\n".join(body_lines),
        rfc_message_id=rfc_msg_id,
        custom_action_id=action_id,
        attachments=attachments,
    )

    transport = transport_override or get_mailbox_transport(db, conn)
    send_res = transport.send_message(outbound)

    reconciled_in_sent = False
    if send_res.outcome == HandoffOutcome.PROVIDER_ACCEPTED:
        try:
            rec = transport.reconcile_sent_message(rfc_msg_id, action_id)
            reconciled_in_sent = bool(rec and rec.outcome == HandoffOutcome.PROVIDER_ACCEPTED)
        except Exception:
            reconciled_in_sent = False

    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="mailbox_self_test_sent" if send_res.outcome == HandoffOutcome.PROVIDER_ACCEPTED else "mailbox_self_test_failed",
            target_type="mailbox_connection",
            target_id=conn.id,
            details_json={
                "provider": conn.provider,
                "target_email": target,
                "outcome": send_res.outcome.value,
                "provider_message_id": send_res.provider_message_id,
                "rfc_message_id": rfc_msg_id,
                "attachment_filename": attached_filename,
                "attachment_sha256": attached_sha256,
                "reconciled_in_sent": reconciled_in_sent,
                "error_message": send_res.error_message[:300],
            },
        )
    )
    db.commit()

    return {
        "ok": send_res.outcome == HandoffOutcome.PROVIDER_ACCEPTED,
        "outcome": send_res.outcome.value,
        "provider": conn.provider,
        "recipient_email": target,
        "provider_message_id": send_res.provider_message_id,
        "rfc_message_id": rfc_msg_id,
        "attachment_filename": attached_filename,
        "attachment_sha256": attached_sha256,
        "reconciled_in_sent": reconciled_in_sent,
        "error_message": send_res.error_message,
    }


def get_latest_outbox_map(db: Session, application_ids: list[str] | set[str]) -> dict[str, OutboxEntry]:
    """Return the most recent OutboxEntry per application_id in a single query."""
    ids = [aid for aid in application_ids if aid]
    if not ids:
        return {}
    entries = db.scalars(
        select(OutboxEntry)
        .where(OutboxEntry.application_id.in_(ids), OutboxEntry.kind == "initial")
        .order_by(OutboxEntry.created_at.asc())
    ).all()
    latest: dict[str, OutboxEntry] = {}
    for entry in entries:
        latest[entry.application_id] = entry
    return latest


def derive_application_queue_state(app_rec: Application, latest_outbox: OutboxEntry | None = None) -> str:
    """
    Canonical queue state across Application and OutboxEntry:
    'ready' | 'approved' | 'queued' | 'sending' | 'sent' | 'failed' | 'blocked' | 'uncertain'
    """
    ts = (app_rec.transport_status or "draft").lower()
    ap = (getattr(app_rec, "approval_state", None) or "draft").lower()
    ob_status = (latest_outbox.status or "").lower() if latest_outbox else ""

    if ts in ("sent", "provider_accepted") or ob_status == "sent":
        return "sent"
    if ts == "send_uncertain" or ob_status == "send_uncertain":
        return "uncertain"
    if ts == "sending" or ob_status == "sending":
        return "sending"
    if ts == "queued" or ob_status in ("queued", "leased"):
        return "queued"
    if ts == "failed" or ob_status == "failed":
        return "failed"
    if ts in ("skipped", "cancelled", "blocked") or ap in ("rejected", "blocked"):
        return "blocked"
    cur_hash = getattr(app_rec, "draft_content_hash", "") or application_draft_hash(app_rec)
    app_hash = getattr(app_rec, "approved_content_hash", "") or ""
    if ap == "approved" and app_hash and app_hash == cur_hash:
        return "approved"
    return "ready"


def reconcile_application_outbox_states(db: Session, *, user_id: str | None = None) -> int:
    """
    Ensure Application.transport_status and Application.skip_or_error_reason stay
    in strict lockstep with any active or completed OutboxEntry rows.
    """
    stmt = select(OutboxEntry).where(
        OutboxEntry.kind == "initial", OutboxEntry.status.in_(["queued", "leased", "sending", "sent", "send_uncertain"])
    )
    if user_id:
        stmt = stmt.where(OutboxEntry.user_id == user_id)
    entries = list(db.scalars(stmt.order_by(OutboxEntry.created_at.asc())).all())
    if not entries:
        return 0

    latest_by_app: dict[str, OutboxEntry] = {}
    for entry in entries:
        latest_by_app[entry.application_id] = entry

    apps = {
        a.id: a
        for a in db.scalars(select(Application).where(Application.id.in_(list(latest_by_app.keys())))).all()
    }
    updated_count = 0
    for app_id, entry in latest_by_app.items():
        app_rec = apps.get(app_id)
        if not app_rec:
            continue
        target_ts = "queued" if entry.status in ("queued", "leased") else entry.status
        if app_rec.sent_at and target_ts != "sent":
            continue
        if app_rec.transport_status in ("sent", "provider_accepted", "send_uncertain") and target_ts != "sent":
            continue
        reason = app_rec.skip_or_error_reason
        if target_ts in ("queued", "send_uncertain") and entry.last_error:
            reason = entry.last_error
        elif target_ts == "sent":
            reason = ""
        previous = app_rec.transport_status
        if previous == target_ts and app_rec.skip_or_error_reason == reason:
            continue
        latest_id = (select(OutboxEntry.id).where(
            OutboxEntry.application_id == app_id, OutboxEntry.kind == "initial", OutboxEntry.user_id == entry.user_id,
        ).order_by(OutboxEntry.created_at.desc(), OutboxEntry.id.desc()).limit(1).scalar_subquery())
        # A GET racing cancellation or acceptance must not restore a stale queue state.
        updated = db.execute(update(Application).where(
            Application.id == app_id, Application.user_id == entry.user_id,
            Application.transport_status == previous, latest_id == entry.id,
            select(OutboxEntry.id).where(OutboxEntry.id == entry.id,
                                         OutboxEntry.status == entry.status).exists(),
        ).values(transport_status=target_ts, skip_or_error_reason=reason).execution_options(synchronize_session=False))
        if updated.rowcount == 1:
            db.refresh(app_rec)
            if previous != target_ts:
                record_application_transition(
                    db, user_id=app_rec.user_id, application_id=app_id, field_name="transport_status",
                    from_state=previous, to_state=target_ts, actor="system:outbox_sync",
                    reason="Synchronized with the current outbox entry", metadata={"outbox_entry_id": entry.id},
                )
            updated_count += 1

    if updated_count > 0:
        db.commit()
    return updated_count


def cancel_queued_outbox_entry(
    db: Session,
    *,
    user: User,
    app_rec: Application,
    reason: str = "Cancelled by user from dashboard",
) -> dict[str, object]:
    """
    Safely cancel a genuinely unsent queued outbox entry, release its reserved recipient lock,
    and return the application to 'draft' transport_status (preserving its 'approved' state).
    Refuses to cancel entries that are currently in external handoff ('sending'), 'sent', or 'send_uncertain'.
    """
    # Take the same application lock as dispatch before reading any cached ORM state.
    current = db.execute(update(Application).where(Application.id == app_rec.id, Application.user_id == user.id)
                         .values(updated_at=Application.updated_at).execution_options(synchronize_session=False))
    if current.rowcount != 1:
        raise ValueError("CANCEL_NOT_ALLOWED: Application unavailable")
    db.refresh(app_rec)
    if app_rec.sent_at or app_rec.transport_status in ("sending", "sent", "provider_accepted", "send_uncertain"):
        raise ValueError(f"CANCEL_NOT_ALLOWED: Handoff already started ({app_rec.transport_status})")
    scope = (OutboxEntry.application_id == app_rec.id, OutboxEntry.user_id == user.id, OutboxEntry.kind == "initial")
    protected = db.scalar(select(OutboxEntry.id).where(*scope, OutboxEntry.status.in_(("sending", "sent", "send_uncertain"))))
    if protected:
        raise ValueError("CANCEL_NOT_ALLOWED: Handoff already started")
    pending = db.scalars(select(OutboxEntry.id).where(*scope, OutboxEntry.status.in_(("queued", "leased")))).all()
    now = utcnow()
    cancelled_ids = []
    if pending:
        cancel = update(OutboxEntry).where(*scope, OutboxEntry.id.in_(pending),
            (OutboxEntry.status == "queued") | ((OutboxEntry.status == "leased") & (OutboxEntry.lease_expires_at <= now)),
        ).values(status="cancelled", lease_owner=None, lease_expires_at=None, last_error=reason).returning(OutboxEntry.id)
        cancelled_ids = list(db.scalars(cancel.execution_options(synchronize_session=False)))
        if len(cancelled_ids) != len(pending):
            db.rollback()
            raise ValueError("CANCEL_NOT_ALLOWED: A worker currently owns this send")
        release_reserved_recipients_if_pre_handoff_failed(db, user_id=user.id, application_id=app_rec.id)
    prev_ts = app_rec.transport_status
    if app_rec.transport_status in ("queued", "ready", "leased"):
        app_rec.transport_status = "draft"
    app_rec.skip_or_error_reason = ""

    if cancelled_ids or prev_ts != app_rec.transport_status:
        record_application_transition(
            db,
            user_id=user.id,
            application_id=app_rec.id,
            field_name="transport_status",
            from_state=prev_ts,
            to_state=app_rec.transport_status,
            actor=f"user:{user.id}",
            reason=reason,
            metadata={"cancelled_outbox_ids": cancelled_ids},
        )
        db.add(
            MessageEvent(
                user_id=user.id,
                application_id=app_rec.id,
                event_type="cancelled",
                direction="internal",
                snippet=reason,
            )
        )
        db.add(
            AuditEvent(
                user_id=user.id,
                actor_user_id=user.id,
                action="outbox_entry_cancelled",
                target_type="application",
                target_id=app_rec.id,
                details_json={"cancelled_outbox_ids": cancelled_ids, "reason": reason},
            )
        )
    db.commit()
    return {
        "ok": True,
        "application_id": app_rec.id,
        "cancelled_outbox_ids": cancelled_ids,
        "already_cancelled": len(cancelled_ids) == 0,
        "transport_status": app_rec.transport_status,
        "approval_state": app_rec.approval_state,
    }


def reconcile_uncertain_outbox_entry(
    db: Session,
    *,
    user: User,
    app_rec: Application,
    transport_override: MailboxTransport | None = None,
    mark_failed_if_not_found: bool = False,
) -> dict[str, object]:
    """
    Reconcile a 'send_uncertain' outbox entry against the mail provider's Sent history by RFC Message-ID / Action ID.
    - If confirmed accepted by provider: marks 'sent' and records provider_message_id.
    An inconclusive lookup preserves the uncertainty and reservation.
    """
    entry = db.scalar(
        select(OutboxEntry)
        .where(
            OutboxEntry.application_id == app_rec.id,
            OutboxEntry.user_id == user.id,
            OutboxEntry.status == "send_uncertain",
        )
        .order_by(OutboxEntry.created_at.desc()).with_for_update().execution_options(populate_existing=True)
    )
    if not entry and app_rec.transport_status != "send_uncertain":
        raise ValueError("RECONCILE_NOT_APPLICABLE: Application is not in 'send_uncertain' state.")

    mailbox_id = entry.mailbox_connection_id if entry else app_rec.mailbox_connection_id
    conn = db.scalar(select(MailboxConnection).where(MailboxConnection.id == mailbox_id, MailboxConnection.user_id == user.id))
    if not conn:
        raise ValueError("NO_MAILBOX: No connected mailbox available to reconcile uncertain send.")

    transport = transport_override or get_mailbox_transport(db, conn)
    rfc_id = (entry.rfc_message_id if entry else app_rec.rfc_message_id) or ""
    act_id = (entry.action_id if entry else "") or ""
    db.commit()  # Release row locks while reading provider Sent records.
    reconciled = transport.reconcile_sent_message(rfc_id, act_id)
    now = utcnow()
    batch = db.scalar(select(Batch).where(Batch.id == app_rec.batch_id))

    if entry:
        claim = update(OutboxEntry).where(OutboxEntry.id == entry.id, OutboxEntry.action_id == act_id,
                                         OutboxEntry.status == "send_uncertain")
        won = db.execute(claim.values(updated_at=now).execution_options(synchronize_session=False))
        db.refresh(entry)
    else:
        won = db.execute(update(Application).where(
            Application.id == app_rec.id, Application.user_id == user.id,
            Application.transport_status == "send_uncertain", Application.rfc_message_id == rfc_id,
        ).values(updated_at=now).execution_options(synchronize_session=False))
    db.refresh(app_rec)
    if won.rowcount != 1:
        status = entry.status if entry else app_rec.transport_status
        if status == "sent":
            return {"ok": True, "application_id": app_rec.id, "reconciled_outcome": "sent",
                    "transport_status": app_rec.transport_status,
                    "provider_message_id": None if entry and entry.kind == "followup" else app_rec.provider_message_id}
        raise ValueError("RECONCILE_NOT_APPLICABLE: Attempt changed during provider lookup.")
    if reconciled and reconciled.outcome == HandoffOutcome.PROVIDER_ACCEPTED:
        if entry:
            entry.status = "sent"
            entry.handoff_outcome = HandoffOutcome.PROVIDER_ACCEPTED.value
            entry.last_error = ""
        if entry and entry.kind == "followup":
            _record_followup_acceptance(db, app_rec, entry, reconciled, now)
            db.commit()
            return {"ok": True, "application_id": app_rec.id, "reconciled_outcome": "sent",
                    "transport_status": app_rec.transport_status, "provider_message_id": reconciled.provider_message_id}
        prev_ts = app_rec.transport_status
        app_rec.transport_status = "sent"
        app_rec.skip_or_error_reason = ""
        app_rec.provider_message_id = reconciled.provider_message_id
        app_rec.provider_thread_id = reconciled.provider_thread_id or app_rec.provider_thread_id
        app_rec.sent_at = app_rec.sent_at or now
        app_rec.next_follow_up_at = as_utc(app_rec.sent_at) + timedelta(days=7)
        if batch:
            batch.sent_count = Batch.sent_count + 1
            batch.uncertain_count = case((Batch.uncertain_count > 0, Batch.uncertain_count - 1), else_=0)
        mark_recipients_sent(db, user_id=user.id, emails=[app_rec.recipient_email, *(app_rec.cc_emails_json or [])])
        record_application_transition(
            db,
            user_id=user.id,
            application_id=app_rec.id,
            field_name="transport_status",
            from_state=prev_ts,
            to_state="sent",
            actor=f"user:{user.id}",
            reason="Reconciled uncertain send: confirmed present in provider Sent folder",
            metadata={"provider_message_id": reconciled.provider_message_id},
        )
        db.add(
            MessageEvent(
                user_id=user.id,
                application_id=app_rec.id,
                event_type="reconciled",
                direction="internal",
                provider_message_id=reconciled.provider_message_id,
                rfc_message_id=rfc_id,
                snippet="Confirmed sent via explicit provider Sent reconciliation",
            )
        )
        db.commit()
        return {
            "ok": True,
            "application_id": app_rec.id,
            "reconciled_outcome": "sent",
            "transport_status": "sent",
            "provider_message_id": reconciled.provider_message_id,
        }

    # An absent Sent result cannot establish that the provider rejected a handoff.
    return {
        "ok": True,
        "application_id": app_rec.id,
        "reconciled_outcome": "still_uncertain",
        "transport_status": app_rec.transport_status,
        "provider_message_id": None,
    }

