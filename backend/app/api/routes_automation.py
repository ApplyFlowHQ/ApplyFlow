"""Mailbox connection, batch execution/control, and recurring schedule endpoints."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.encryption import encrypt_json
from app.core.ssrf import SSRFBlockedError, validate_mail_host
from app.db.base import as_utc, utcnow
from app.db.models import (
    Application,
    ApplicationTransition,
    AuditEvent,
    Batch,
    CandidateProfile,
    CvExtractedFact,
    CvVersion,
    DiscoverySourceSighting,
    JobMatch,
    JobPosting,
    MailboxConnection,
    OutboxEntry,
    Schedule,
    ScheduleOccurrence,
    SearchPreference,
    User,
)
from app.db.session import get_db
from app.schemas import (
    CancelOutboxRequest,
    DraftApproveRequest,
    DraftReviseRequest,
    DraftSendApprovedRequest,
    MailboxSelfTestRequest,
    ReconcileOutboxRequest,
    RetrySendRequest,
    ScheduleCreateRequest,
    SchedulePatchRequest,
    SmtpMailboxConnectRequest,
    StartBatchRequest,
)
from app.services.ai.grounding import generate_grounded_application_draft
from app.services.approval import (
    application_draft_hash,
    approve_application_for_send,
    record_application_transition,
    revise_application_draft,
    sync_application_hash,
)
from app.services.auth import AuthError, consume_google_state
from app.services.batch_runner import serialize_candidate
from app.services.bundle_import import (
    MAX_ARCHIVE_BYTES,
    BundleImportError,
    compute_batch_workflow_counts,
    import_gemini_bundle_zip,
    revalidate_application_draft,
)
from app.services.dates import ensure_valid_timezone
from app.services.dedupe import release_reserved_recipients_if_pre_handoff_failed
from app.services.discovery.career_page import parse_user_pasted_posting
from app.services.effective_config import (
    resolve_effective_google_oauth,
    resolve_effective_live_sending,
    resolve_effective_ms_graph_oauth,
)
from app.services.inbox_processor import poll_mailbox_connection
from app.services.mailbox.base import OutboundEmailMessage
from app.services.mailbox.gmail import (
    GMAIL_COMPOSE_SCOPE,
    build_gmail_oauth_url,
    complete_gmail_oauth_callback,
    create_gmail_draft,
    get_gmail_connection_health,
    get_mailbox_dedup_coverage,
    revoke_gmail_connection,
    sync_gmail_sent_history,
)
from app.services.mailbox.ms_graph import build_ms_graph_oauth_url, complete_ms_graph_callback
from app.services.outbox import (
    _load_verified_attachments,
    cancel_queued_outbox_entry,
    count_rolling_24h_recipients,
    derive_application_queue_state,
    effective_daily_limit,
    enqueue_application_for_send,
    get_latest_outbox_map,
    reconcile_application_outbox_states,
    reconcile_uncertain_outbox_entry,
    send_owner_self_test_email,
)
from app.services.scheduler import refresh_schedule_next_run
from app.services.storage import get_storage_adapter
from app.tasks.maintenance import local_batch_worker_lifespan
from app.tasks.pipeline import task_run_batch

router = APIRouter(tags=["automation"], lifespan=local_batch_worker_lifespan)


def _publish_discovery_batch(batch_id: str) -> None:
    # The committed row is the queue of record. Beat also publishes missed wakeups.
    from app.tasks.celery_app import celery_app

    try:
        celery_app.send_task(task_run_batch.name, args=[batch_id], retry=False)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("Batch publish deferred (%s)", type(exc).__name__)


def _dispatch_requested_batch(background_tasks: BackgroundTasks, db: Session, batch: Batch) -> None:
    settings = get_settings()
    if settings.environment == "test" and settings.celery_task_always_eager:
        task_run_batch.run(batch.id)
        db.refresh(batch)
    elif not settings.celery_task_always_eager:
        background_tasks.add_task(_publish_discovery_batch, batch.id)


def _serialize_batch(b: Batch) -> dict[str, object]:
    cat = b.category_counts_json or {}
    stored_metrics = b.metrics_json or {}
    fallback_metrics = {
        "search_hits": int(b.discovered_count or 0),
        "unique_source_urls": int(b.discovered_count or 0),
        "verified_role_email_invitations": int(cat.get("verified_email_vacancy", 0)),
        "verified_general_cv_invitations": int(cat.get("verified_general_cv_invitation", 0)),
        "eligible_for_draft": int(b.eligible_count or 0),
        "eligible_for_live_send": int(b.eligible_count or 0) if b.mode == "live" else 0,
        "drafted": int(b.drafted_count or 0),
        "sent_accepted": int(b.sent_count or 0),
        "failed_or_uncertain": int(b.uncertain_count or 0),
        "skipped": int(b.skipped_count or 0),
        "duplicates": int(b.duplicates_removed_count or 0),
        "demo_excluded": int(b.discovered_count or 0) if b.mode == "demo_fixture" else 0,
    }
    return {
        "id": b.id,
        "mode": b.mode,
        "run_mode": getattr(b, "run_mode", b.mode) or b.mode,
        "pipeline_version": getattr(b, "pipeline_version", "legacy") or "legacy",
        "target_goal_count": getattr(b, "target_goal_count", 10) or 10,
        "goal_shortfall_reason": getattr(b, "goal_shortfall_reason", "") or "",
        "stage": b.status if b.status not in ("queued", "running") else b.discovery_stage or b.status,
        "progress_counts": {
            "queries_executed": (b.budget_usage_json or {}).get("search_queries_executed", 0),
            "candidates_found": (b.checkpoint_json or {}).get("candidates_found", b.discovered_count),
            "candidates_processed": (b.checkpoint_json or {}).get("candidates_processed", b.discovered_count),
            "openings_discovered": b.discovered_count,
        },
        "stage_status": getattr(b, "stage_status_json", []) or [],
        "budget_config": getattr(b, "budget_config_json", {}) or {},
        "budget_usage": getattr(b, "budget_usage_json", {}) or {},
        "is_demo": b.mode == "demo_fixture",
        "status": b.status,
        "strategy_used": b.strategy_used,
        "import_origin": getattr(b, "import_origin", "") or "",
        "source_archive_sha256": getattr(b, "source_archive_sha256", "") or "",
        "discovered_count": b.discovered_count,
        "eligible_count": b.eligible_count,
        "skipped_count": b.skipped_count,
        "duplicates_removed_count": b.duplicates_removed_count,
        "category_counts": b.category_counts_json,
        "skip_reason_counts": b.skip_reason_counts_json,
        "metrics": stored_metrics if stored_metrics else fallback_metrics,
        "drafted_count": b.drafted_count,
        "sent_count": b.sent_count,
        "uncertain_count": b.uncertain_count,
        "estimated_cost_usd": b.estimated_cost_usd,
        "pause_reason": b.pause_reason,
        "error_message": b.error_message,
        "coverage_note": b.coverage_note,
        "sources_searched": b.sources_searched_json,
        "source_errors": b.source_errors_json,
        "created_at": b.created_at.isoformat(),
        "finished_at": b.finished_at.isoformat() if b.finished_at else None,
    }


def _sending_mailbox_query(user_id: str):
    return (
        select(MailboxConnection)
        .where(MailboxConnection.user_id == user_id, MailboxConnection.status == "connected", MailboxConnection.revoked_at.is_(None))
        .order_by(MailboxConnection.is_primary.desc(), MailboxConnection.created_at.desc(), MailboxConnection.id)
        .limit(1)
    )


@router.get("/mailboxes")
def api_list_mailboxes(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    conns = db.scalars(
        select(MailboxConnection).where(MailboxConnection.user_id == user.id).order_by(MailboxConnection.created_at.desc())
    ).all()
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    items = []
    for c in conns:
        used_24h = count_rolling_24h_recipients(db, user_id=user.id, mailbox_connection_id=c.id)
        limit = effective_daily_limit(pref, c) if pref else get_settings().max_daily_recipients_default
        items.append(
            {
                "id": c.id,
                "provider": c.provider,
                "label": c.label,
                "sender_email": c.sender_email,
                "granted_scopes": c.granted_scopes_json,
                "smtp_host": c.smtp_host,
                "smtp_port": c.smtp_port,
                "imap_host": c.imap_host,
                "is_primary": c.is_primary,
                "status": c.status,
                "last_error": c.last_error,
                "inbox_poll_enabled": c.inbox_poll_enabled,
                "inbox_health_status": c.inbox_health_status,
                "inbox_last_polled_at": c.inbox_last_polled_at.isoformat() if c.inbox_last_polled_at else None,
                "save_drafts_in_gmail": bool(getattr(c, "save_drafts_in_gmail", False)),
                "sent_sync_status": getattr(c, "sent_sync_status", "disabled"),
                "sent_last_synced_at": c.sent_last_synced_at.isoformat() if getattr(c, "sent_last_synced_at", None) else None,
                "sent_earliest_covered_at": c.sent_earliest_covered_at.isoformat() if getattr(c, "sent_earliest_covered_at", None) else None,
                "sent_records_count": int(getattr(c, "sent_records_count", 0) or 0),
                "dedup_coverage": get_mailbox_dedup_coverage(db, conn=c) if c.provider in ("gmail_oauth", "smtp_imap") else None,
                "connection_health": get_gmail_connection_health(c),
                "rolling_24h_used": used_24h,
                "rolling_24h_limit": limit,
            }
        )
    google_cfg = resolve_effective_google_oauth(db)
    ms_cfg = resolve_effective_ms_graph_oauth(db)
    sender = db.scalar(_sending_mailbox_query(user.id))
    return {
        "sending_mailbox_id": sender.id if sender else None,
        "mailboxes": items,
        "google_oauth_configured": bool(google_cfg.client_id and google_cfg.client_secret),
        "ms_graph_configured": bool(ms_cfg.client_id and ms_cfg.client_secret),
    }


@router.get("/mailboxes/gmail/start")
def api_gmail_oauth_start(
    include_inbox_readonly: bool = False,
    include_compose_drafts: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        url = build_gmail_oauth_url(
            db,
            user_id=user.id,
            include_inbox_readonly=include_inbox_readonly,
            include_compose_drafts=include_compose_drafts,
        )
        return {"authorization_url": url}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "OAUTH_NOT_CONFIGURED", "message": str(exc)}) from exc


@router.get("/mailboxes/gmail/callback")
def api_gmail_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from urllib.parse import urlencode

    app_base = get_settings().app_url.rstrip("/")
    wants_html = "text/html" in (request.headers.get("accept") or "").lower()

    try:
        if not state:
            raise AuthError("INVALID_OAUTH_STATE", "Invalid or expired Gmail connection request.")
        if error or not code:
            consume_google_state(db, state=state, flow="gmail_mailbox", user_id=user.id)
            if error == "access_denied":
                raise AuthError("GOOGLE_CONSENT_DENIED", "Gmail permission was cancelled. You can reconnect anytime.")
            raise AuthError("GMAIL_CALLBACK_INVALID", "Google could not complete the mailbox connection.")
        complete_gmail_oauth_callback(
            db,
            user_id=user.id,
            code=code,
            state=state,
        )
    except (AuthError, ValueError, TypeError, AttributeError, httpx.HTTPError) as exc:
        message = exc.message if isinstance(exc, AuthError) else "Unable to complete the Gmail connection. Please reconnect."
        code = exc.code if isinstance(exc, AuthError) else "GMAIL_CALLBACK_FAILED"
        if wants_html:
            return RedirectResponse(url=f"{app_base}/settings?{urlencode({'mailbox_error': message})}", status_code=302)
        raise HTTPException(status_code=400, detail={"code": code, "message": message}) from exc
    return RedirectResponse(url=f"{app_base}/settings?mailbox=gmail_connected", status_code=302)


@router.get("/mailboxes/ms-graph/start")
def api_ms_graph_start(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    try:
        url = build_ms_graph_oauth_url(db, user_id=user.id)
        return {"authorization_url": url}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "OAUTH_NOT_CONFIGURED", "message": str(exc)}) from exc


@router.get("/mailboxes/ms-graph/callback")
def api_ms_graph_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        complete_ms_graph_callback(db, user_id=user.id, code=None if error else code, state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "MS_GRAPH_FAILED", "message": "Microsoft Graph connection failed. Please start again."}) from exc
    return RedirectResponse(url=f"{get_settings().app_url.rstrip('/')}/settings?mailbox=ms_graph_connected", status_code=302)


@router.post("/mailboxes/smtp", status_code=status.HTTP_201_CREATED)
def api_connect_smtp(
    payload: SmtpMailboxConnectRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        validate_mail_host(payload.smtp_host, payload.smtp_port)
        if payload.imap_host:
            validate_mail_host(payload.imap_host, payload.imap_port)
    except SSRFBlockedError as exc:
        raise HTTPException(status_code=400, detail={"code": "UNSAFE_MAIL_HOST", "message": str(exc)}) from exc

    if payload.smtp_host.lower() == "smtp.gmail.com":
        # Enforce safe guidance: app passwords are required if not using OAuth; never imply normal password works
        pass

    creds = {"username": payload.username.strip() or payload.sender_email, "password": payload.password}
    for other in db.scalars(select(MailboxConnection).where(MailboxConnection.user_id == user.id)).all():
        other.is_primary = False

    inbox_enabled = bool(payload.inbox_poll_enabled and payload.imap_host.strip())
    conn = MailboxConnection(
        user_id=user.id,
        provider="smtp_imap",
        label=payload.label.strip(),
        sender_email=str(payload.sender_email),
        authorized_aliases_json=[a.strip() for a in payload.authorized_aliases if a.strip()],
        encrypted_credentials=encrypt_json(creds),
        smtp_host=payload.smtp_host.strip(),
        smtp_port=payload.smtp_port,
        smtp_use_tls=payload.smtp_use_tls,
        imap_host=payload.imap_host.strip(),
        imap_port=payload.imap_port,
        imap_use_tls=payload.imap_use_tls,
        is_primary=True,
        status="connected",
        inbox_poll_enabled=inbox_enabled,
        inbox_health_status="healthy" if inbox_enabled else "disabled",
    )
    db.add(conn)
    db.commit()
    return {"id": conn.id, "status": conn.status, "inbox_poll_enabled": conn.inbox_poll_enabled}


@router.post("/mailboxes/{mailbox_id}/poll-now")
def api_poll_mailbox_now(
    mailbox_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conn = db.scalar(select(MailboxConnection).where(MailboxConnection.id == mailbox_id, MailboxConnection.user_id == user.id))
    if not conn:
        raise HTTPException(status_code=404, detail={"code": "MAILBOX_NOT_FOUND", "message": "Mailbox not found"})
    count = poll_mailbox_connection(db, conn)
    return {"polled_events": count, "inbox_health_status": conn.inbox_health_status}


@router.get("/mailboxes/{mailbox_id}/dedup-coverage")
def api_mailbox_dedup_coverage(
    mailbox_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conn = db.scalar(select(MailboxConnection).where(MailboxConnection.id == mailbox_id, MailboxConnection.user_id == user.id))
    if not conn:
        raise HTTPException(status_code=404, detail={"code": "MAILBOX_NOT_FOUND", "message": "Mailbox not found"})
    return get_mailbox_dedup_coverage(db, conn=conn)


@router.post("/mailboxes/{mailbox_id}/sync-sent")
def api_sync_mailbox_sent(
    mailbox_id: str,
    force_full: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conn = db.scalar(select(MailboxConnection).where(MailboxConnection.id == mailbox_id, MailboxConnection.user_id == user.id))
    if not conn:
        raise HTTPException(status_code=404, detail={"code": "MAILBOX_NOT_FOUND", "message": "Mailbox not found"})
    if conn.provider not in ("gmail_oauth", "smtp_imap"):
        raise HTTPException(
            status_code=400,
            detail={"code": "UNSUPPORTED_PROVIDER", "message": "Sent history sync requires Gmail OAuth or an IMAP-enabled mailbox"},
        )
    res = sync_gmail_sent_history(db, conn=conn, force_full=force_full)
    res["dedup_coverage"] = get_mailbox_dedup_coverage(db, conn=conn)
    return res


@router.post("/mailboxes/{mailbox_id}/send-test")
def api_send_mailbox_self_test(
    mailbox_id: str,
    payload: MailboxSelfTestRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not payload.explicit_confirm:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONFIRMATION_REQUIRED",
                "message": "You must explicitly confirm before sending a self-test email through your mailbox",
            },
        )
    conn = db.scalar(
        select(MailboxConnection).where(
            MailboxConnection.id == mailbox_id,
            MailboxConnection.user_id == user.id,
        )
    )
    if not conn:
        raise HTTPException(status_code=404, detail={"code": "MAILBOX_NOT_FOUND", "message": "Mailbox not found"})
    try:
        return send_owner_self_test_email(
            db,
            user=user,
            conn=conn,
            recipient_email=payload.recipient_email,
            cv_version_id=payload.cv_version_id,
            subject=payload.subject,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "SELF_TEST_REJECTED", "message": str(exc)}) from exc


@router.post("/mailboxes/{mailbox_id}/disconnect")
def api_disconnect_mailbox(
    mailbox_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conn = db.scalar(select(MailboxConnection).where(MailboxConnection.id == mailbox_id, MailboxConnection.user_id == user.id))
    if not conn:
        raise HTTPException(status_code=404, detail={"code": "MAILBOX_NOT_FOUND", "message": "Mailbox not found"})

    if conn.provider == "gmail_oauth":
        revoke_gmail_connection(db, conn)
    else:
        conn.encrypted_credentials = ""
        conn.status = "revoked"
        conn.revoked_at = utcnow()
        conn.inbox_poll_enabled = False
        conn.inbox_health_status = "disabled"

    from app.services.pre_handoff import cancel_pending_for_applications

    reason = "Mailbox disconnected"
    for app, _, initial in cancel_pending_for_applications(db, reason, user_id=user.id, mailbox_id=conn.id):
        if initial and app.transport_status in ("draft", "ready", "leased", "queued"):
            app.transport_status = "cancelled"
            app.skip_or_error_reason = reason
            release_reserved_recipients_if_pre_handoff_failed(db, user_id=user.id, application_id=app.id)
    db.commit()
    return {"ok": True, "status": conn.status}


@router.get("/batches")
def api_list_batches(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    batches = db.scalars(
        select(Batch).where(Batch.user_id == user.id).order_by(Batch.created_at.desc()).limit(50)
    ).all()
    return {"batches": [_serialize_batch(b) for b in batches]}


@router.post("/batches", status_code=status.HTTP_201_CREATED)
def api_start_batch(
    payload: StartBatchRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    if payload.run_mode:
        effective_run_mode = payload.run_mode
    elif payload.mode in ("approval_required", "autopilot"):
        effective_run_mode = payload.mode
    elif payload.mode == "live":
        effective_run_mode = pref.campaign_mode if (pref and pref.campaign_mode in ("approval_required", "autopilot")) else "approval_required"
    else:
        effective_run_mode = payload.mode

    canonical_mode = "live" if effective_run_mode in ("approval_required", "autopilot") else effective_run_mode

    if user.automation_stopped_at is not None and canonical_mode == "live":
        raise HTTPException(
            status_code=409,
            detail={"code": "AUTOMATION_STOPPED", "message": "Global Stop Automation is active; resume automation first."},
        )
    if effective_run_mode == "autopilot" and (not pref or not pref.live_consent_granted):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LIVE_CONSENT_REQUIRED",
                "message": "Autopilot mode requires explicit user live-sending consent in Preferences.",
            },
        )

    if idempotency_key:
        existing = db.scalar(
            select(Batch).where(Batch.user_id == user.id, Batch.idempotency_key == idempotency_key)
        )
        if existing:
            return {
                "batch_id": existing.id,
                "status": existing.status,
                "mode": existing.mode,
                "run_mode": getattr(existing, "run_mode", existing.mode),
                "idempotent_replay": True,
            }

    prev_count = db.scalar(select(func.count(Batch.id)).where(Batch.user_id == user.id)) or 0
    b_cfg = dict(payload.budget_config or {})
    if payload.include_standing_invitations is not None:
        b_cfg["include_standing_invitations"] = payload.include_standing_invitations
    checkpoint = {"max_search_queries": payload.max_search_queries}
    if payload.pasted_posting:
        checkpoint["pasted_candidate"] = serialize_candidate(parse_user_pasted_posting(**payload.pasted_posting.model_dump()))
    batch = Batch(
        user_id=user.id,
        idempotency_key=idempotency_key,
        mode=canonical_mode,
        run_mode="approval_required" if canonical_mode == "live" else effective_run_mode,
        pipeline_version="vNext",
        target_goal_count=payload.target_goal_count or (pref.target_goal_count if pref else 10) or 10,
        budget_config_json=b_cfg,
        status="queued",
        discovery_stage="queued" if payload.run_immediately else "deferred",
        checkpoint_json=checkpoint,
        query_rotation_offset=int(prev_count),
    )
    db.add(batch)
    try:
        db.flush()
        db.add(AuditEvent(user_id=user.id, actor_user_id=user.id, action="batch_created",
                          target_type="batch", target_id=batch.id,
                          details_json={"mode": canonical_mode, "run_mode": batch.run_mode}))
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Batch).where(Batch.user_id == user.id, Batch.idempotency_key == idempotency_key))
        if not idempotency_key or existing is None:
            raise
        return {"batch_id": existing.id, "status": existing.status, "mode": existing.mode,
                "run_mode": existing.run_mode, "idempotent_replay": True}

    if payload.run_immediately:
        _dispatch_requested_batch(background_tasks, db, batch)

    return {"batch_id": batch.id, "status": batch.status, "mode": batch.mode, "run_mode": batch.run_mode}


@router.post("/batches/import-bundle")
def api_import_gemini_bundle(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    raw_bytes = file.file.read(MAX_ARCHIVE_BYTES + 1)
    settings = get_settings()
    try:
        res = import_gemini_bundle_zip(
            db,
            user=user,
            zip_bytes=raw_bytes,
            original_filename=file.filename or "gemini_bundle.zip",
            skip_network=(settings.environment == "test"),
        )
    except BundleImportError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    batch = res["batch"]
    return {
        "batch_id": batch.id,
        "batch": _serialize_batch(batch),
        "duplicate_import": res["duplicate_import"],
        "created_applications": res["created_applications"],
        "workflow_counts": res["counts"],
        "ignored_files": res["ignored_files"],
        "dedup_coverage": res.get("dedup_coverage"),
        "message": res["message"],
    }


@router.get("/batches/{batch_id}")
def api_get_batch_detail(
    batch_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    batch = db.scalar(select(Batch).where(Batch.id == batch_id, Batch.user_id == user.id))
    if not batch:
        raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND", "message": "Batch not found"})

    postings = db.scalars(
        select(JobPosting)
        .where(JobPosting.batch_id == batch.id, JobPosting.user_id == user.id)
        .order_by(JobPosting.source_priority_tier.asc(), JobPosting.created_at.asc())
    ).all()
    post_by_id = {p.id: p for p in postings}
    matches = db.scalars(select(JobMatch).where(JobMatch.batch_id == batch.id, JobMatch.user_id == user.id, JobMatch.is_selected_cv.is_(True))).all()
    match_by_post = {m.job_posting_id: m for m in matches}
    apps = db.scalars(
        select(Application)
        .where(Application.batch_id == batch.id, Application.user_id == user.id)
        .order_by(Application.created_at.asc())
    ).all()
    app_by_post = {a.job_posting_id: a for a in apps}
    cv_ver_ids = {a.selected_cv_version_id for a in apps if a.selected_cv_version_id}
    cv_doc_by_ver = (
        {
            v.id: v.cv_document_id
            for v in db.scalars(select(CvVersion).where(CvVersion.id.in_(cv_ver_ids))).all()
        }
        if cv_ver_ids
        else {}
    )
    live_enabled_now, live_reason_now = resolve_effective_live_sending(db)
    workflow_counts = compute_batch_workflow_counts(db, batch_id=batch.id, user_id=user.id)
    outbox_by_app = get_latest_outbox_map(db, [a.id for a in apps])
    sightings = db.scalars(
        select(DiscoverySourceSighting)
        .where(DiscoverySourceSighting.batch_id == batch.id, DiscoverySourceSighting.user_id == user.id)
        .order_by(DiscoverySourceSighting.seen_at.asc())
    ).all()

    serialized_all_apps = [
        _serialize_review_draft(
            a,
            post_by_id.get(a.job_posting_id),
            match_by_post.get(a.job_posting_id),
            batch,
            cv_doc_by_ver.get(a.selected_cv_version_id or "", ""),
            outbox_by_app.get(a.id),
        )
        for a in apps
    ]

    return {
        "batch": _serialize_batch(batch),
        "live_sending_enabled": live_enabled_now,
        "live_sending_reason": live_reason_now,
        "workflow_counts": workflow_counts,
        "draft_applications": [item for item in serialized_all_apps if item["transport_status"] != "skipped"],
        "all_applications": serialized_all_apps,
        "sightings_count": len(sightings),
        "sightings": [
            {
                "id": s.id,
                "source_provider": s.source_provider,
                "publisher": s.publisher,
                "source_url": s.source_url,
                "canonical_url": s.canonical_url,
                "access_status": s.access_status,
                "disposition": s.disposition,
                "is_duplicate": s.is_duplicate,
                "dedupe_reason": s.dedupe_reason,
                "primary_skip_code": s.primary_skip_code,
                "secondary_skip_codes": s.secondary_skip_codes_json,
                "research_stage": getattr(s, "research_stage", "discovery_search"),
                "corroborated": bool(getattr(s, "corroborated", False)),
            }
            for s in sightings
        ],
        "postings": [
            {
                "id": p.id,
                "is_demo": p.is_demo,
                "pipeline_version": getattr(p, "pipeline_version", "legacy"),
                "title": p.title,
                "role_extracted": p.role_extracted,
                "company_name": p.validated_employer or p.company_name,
                "company_domain": p.company_domain,
                "publisher": p.publisher,
                "purported_employer": p.purported_employer,
                "validated_employer": p.validated_employer,
                "employer_confidence": p.employer_confidence,
                "location_raw": p.location_raw,
                "country_code": p.country_code,
                "city": p.city,
                "location_confidence": p.location_confidence,
                "remote_scope": p.remote_scope,
                "source_provider": p.source_provider,
                "source_url": p.source_url,
                "canonical_url": p.canonical_url or p.source_url,
                "search_query": p.search_query,
                "date_source": p.date_source,
                "access_status": p.access_status,
                "parsing_version": p.parsing_version,
                "source_verification_tier": getattr(p, "source_verification_tier", "tier_5_unrelated_or_prohibited"),
                "corroboration_status": getattr(p, "corroboration_status", "uncorroborated"),
                "corroboration_url": getattr(p, "corroboration_url", ""),
                "requisition_id": getattr(p, "requisition_id", ""),
                "normalized_vacancy_key": getattr(p, "normalized_vacancy_key", ""),
                "application_email": p.application_email,
                "cc_emails": p.cc_emails_json,
                "subject_instruction": p.subject_instruction,
                "evidence_quote": p.evidence_quote,
                "is_standing_invitation": p.is_standing_invitation,
                "discovery_category": p.discovery_category,
                "source_priority_tier": p.source_priority_tier,
                "verification_status": p.verification_status,
                "primary_skip_code": p.primary_skip_code,
                "skip_codes": p.skip_codes_json,
                "secondary_skip_codes": p.secondary_skip_codes_json,
                "skip_reason": p.skip_reason or (app_by_post[p.id].skip_or_error_reason if p.id in app_by_post else ""),
                "extraction_evidence": p.extraction_evidence_json,
                "eligibility_requirements": p.eligibility_requirements_json,
                "fit_score": match_by_post[p.id].fit_score if p.id in match_by_post else None,
                "why_this_cv": getattr(match_by_post[p.id], "why_this_cv", "") if p.id in match_by_post else "",
                "verified_skills": getattr(match_by_post[p.id], "verified_skills_json", []) if p.id in match_by_post else [],
                "required_gaps": getattr(match_by_post[p.id], "required_gaps_json", []) if p.id in match_by_post else [],
                "application_id": app_by_post[p.id].id if p.id in app_by_post else None,
                "transport_status": app_by_post[p.id].transport_status if p.id in app_by_post else None,
                "approval_state": getattr(app_by_post[p.id], "approval_state", None) if p.id in app_by_post else None,
                "retrieved_at": p.retrieved_at.isoformat(),
                "posted_at": p.posted_at.isoformat() if p.posted_at else None,
            }
            for p in postings
        ],
    }


def _serialize_review_draft(
    app: Application,
    posting: JobPosting | None = None,
    match: JobMatch | None = None,
    batch: Batch | None = None,
    cv_document_id: str = "",
    latest_outbox: OutboxEntry | None = None,
) -> dict[str, object]:
    effective_transport = app.transport_status
    if latest_outbox and latest_outbox.status in ("queued", "leased") and effective_transport in ("ready", "draft"):
        effective_transport = "queued"
    elif latest_outbox and latest_outbox.status in ("sending", "sent", "send_uncertain") and effective_transport != latest_outbox.status:
        effective_transport = latest_outbox.status

    effective_reason = (
        app.skip_or_error_reason
        or (latest_outbox.last_error if latest_outbox and latest_outbox.status in ("queued", "leased", "failed", "send_uncertain") else "")
        or (posting.skip_reason if posting else "")
    )
    if not effective_reason and latest_outbox and latest_outbox.status == "queued":
        effective_reason = "Waiting for the outbox worker. If this persists, check the worker and Beat services."
    return {
        "id": app.id,
        "batch_id": app.batch_id,
        "run_mode": getattr(batch, "run_mode", batch.mode if batch else "") if batch else "",
        "import_origin": getattr(app, "import_origin", "") or "",
        "is_demo": app.is_demo,
        "company_name": (posting.validated_employer if posting else "") or app.company_name,
        "role_title": (posting.role_extracted if posting else "") or app.role_title,
        "country_code": app.country_code,
        "city": app.city,
        "location_raw": (posting.location_raw if posting else "") or f"{app.city} {app.country_code}".strip(),
        "recipient_email": app.recipient_email,
        "cc_emails": app.cc_emails_json,
        "subject": app.subject,
        "body_text": app.body_text,
        "language": app.language,
        "selected_cv_version_id": app.selected_cv_version_id or "",
        "selected_cv_document_id": cv_document_id,
        "frozen_cv_filename": app.frozen_cv_filename,
        "attachment_pdf_sha256": getattr(app, "attachment_pdf_sha256", "") or app.frozen_cv_sha256,
        "approval_state": getattr(app, "approval_state", "draft"),
        "transport_status": effective_transport,
        "queue_state": derive_application_queue_state(app, latest_outbox),
        "outbox_id": latest_outbox.id if latest_outbox else None,
        "outbox_status": latest_outbox.status if latest_outbox else None,
        "outbox_attempt_count": int(latest_outbox.attempt_count) if latest_outbox else 0,
        "outbox_max_attempts": int(latest_outbox.max_attempts) if latest_outbox else 3,
        "outbox_next_attempt_at": latest_outbox.next_attempt_at.isoformat() if latest_outbox and latest_outbox.next_attempt_at else None,
        "outbox_last_error": latest_outbox.last_error if latest_outbox else "",
        "outbox_handoff_outcome": latest_outbox.handoff_outcome if latest_outbox else "none",
        "outbox_created_at": latest_outbox.created_at.isoformat() if latest_outbox and latest_outbox.created_at else None,
        "outbox_updated_at": latest_outbox.updated_at.isoformat() if latest_outbox and latest_outbox.updated_at else None,
        "source_verification_tier": getattr(app, "source_verification_tier", "tier_5_unrelated_or_prohibited"),
        "corroboration_status": getattr(posting, "corroboration_status", "uncorroborated") if posting else "uncorroborated",
        "corroboration_url": getattr(posting, "corroboration_url", "") if posting else "",
        "dns_check_status": getattr(posting, "dns_check_status", "unchecked") if posting else "unchecked",
        "evidence_url": (posting.canonical_url or posting.source_url) if posting else "",
        "evidence_quote": posting.evidence_quote if posting else "",
        "verification_explanation": (posting.skip_reason or posting.verification_status) if posting else "",
        "posted_at": posting.posted_at.isoformat() if posting and posting.posted_at else None,
        "requested_subject": posting.subject_instruction if posting else "",
        "required_documents": posting.attachment_requirements_json if posting else [],
        "primary_skip_code": app.primary_skip_code or (posting.primary_skip_code if posting else ""),
        "skip_or_error_reason": effective_reason,
        "review_reasons": getattr(app, "review_reasons_json", []) or [],
        "review_notes": getattr(app, "review_notes", "") or "",
        "draft_content_hash": getattr(app, "draft_content_hash", "") or application_draft_hash(app),
        "approved_content_hash": getattr(app, "approved_content_hash", "") or "",
        "approved_at": app.approved_at.isoformat() if getattr(app, "approved_at", None) else None,
        "gmail_draft_id": getattr(app, "gmail_draft_id", "") or "",
        "provider_message_id": app.provider_message_id or "",
        "sent_at": app.sent_at.isoformat() if app.sent_at else None,
        "fit_score": match.fit_score if match else None,
        "why_this_cv": getattr(match, "why_this_cv", "") if match else "",
        "verified_skills": getattr(match, "verified_skills_json", []) if match else [],
        "required_gaps": getattr(match, "required_gaps_json", []) if match else [],
        "discovered_at": app.discovered_at.isoformat(),
    }


@router.get("/review-queue")
def api_list_review_queue(
    scope: str = "real",
    approval_state: str = "",
    batch_id: str = "",
    include_all: bool = False,
    offset: int = 0,
    limit: int = 100,
    application_ids: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    reconcile_application_outbox_states(db, user_id=user.id)
    stmt = select(Application).where(Application.user_id == user.id)
    if not include_all:
        stmt = stmt.where(Application.transport_status != "skipped")
    if scope == "real":
        stmt = stmt.where(Application.is_demo.is_(False))
    elif scope == "demo":
        stmt = stmt.where(Application.is_demo.is_(True))
    if approval_state:
        stmt = stmt.where(Application.approval_state == approval_state)
    elif not include_all:
        stmt = stmt.where(
            Application.approval_state.in_(["ready_for_approval", "needs_human_review", "approved", "draft"])
        )
    if batch_id:
        stmt = stmt.where(Application.batch_id == batch_id)

    if application_ids:
        ids = application_ids.split(",")
        if len(ids) > 100:
            raise HTTPException(422, detail="At most 100 application IDs can be requested at once")
        stmt = stmt.where(Application.id.in_(ids))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    apps = list(db.scalars(stmt.order_by(Application.created_at.desc(), Application.id.desc())
                           .offset(max(0, offset)).limit(min(100, max(1, limit)))).all())
    outbox_by_app = get_latest_outbox_map(db, [a.id for a in apps])
    post_ids = {a.job_posting_id for a in apps if a.job_posting_id}
    match_ids = {a.job_match_id for a in apps if a.job_match_id}
    batch_ids = {a.batch_id for a in apps if a.batch_id}
    cv_ver_ids = {a.selected_cv_version_id for a in apps if a.selected_cv_version_id}
    postings = {p.id: p for p in db.scalars(select(JobPosting).where(JobPosting.id.in_(post_ids))).all()} if post_ids else {}
    matches = {m.id: m for m in db.scalars(select(JobMatch).where(JobMatch.id.in_(match_ids))).all()} if match_ids else {}
    batches = {b.id: b for b in db.scalars(select(Batch).where(Batch.id.in_(batch_ids))).all()} if batch_ids else {}
    cv_doc_by_ver = (
        {v.id: v.cv_document_id for v in db.scalars(select(CvVersion).where(CvVersion.id.in_(cv_ver_ids))).all()}
        if cv_ver_ids
        else {}
    )
    live_enabled, live_reason = resolve_effective_live_sending(db)
    wf_counts = (
        compute_batch_workflow_counts(db, batch_id=batch_id, user_id=user.id)
        if batch_id
        else None
    )
    return {
        "total": total,
        "live_sending_enabled": live_enabled,
        "live_sending_reason": live_reason,
        "workflow_counts": wf_counts,
        "items": [
            _serialize_review_draft(
                a,
                postings.get(a.job_posting_id),
                matches.get(a.job_match_id or ""),
                batches.get(a.batch_id),
                cv_doc_by_ver.get(a.selected_cv_version_id or "", ""),
                outbox_by_app.get(a.id),
            )
            for a in apps
        ],
    }


@router.get("/opportunities")
def api_list_opportunities(
    batch_id: str = "",
    tier: str = "",
    scope: str = "real",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(JobPosting).where(JobPosting.user_id == user.id)
    if scope == "real":
        stmt = stmt.where(JobPosting.is_demo.is_(False))
    elif scope == "demo":
        stmt = stmt.where(JobPosting.is_demo.is_(True))
    if batch_id:
        stmt = stmt.where(JobPosting.batch_id == batch_id)
    if tier:
        stmt = stmt.where(JobPosting.source_verification_tier == tier)
    postings = list(db.scalars(stmt.order_by(JobPosting.created_at.desc()).limit(100)).all())
    return {
        "opportunities": [
            {
                "id": p.id,
                "batch_id": p.batch_id,
                "title": p.role_extracted or p.title,
                "company_name": p.validated_employer or p.company_name,
                "source_verification_tier": getattr(p, "source_verification_tier", "tier_5_unrelated_or_prohibited"),
                "corroboration_status": getattr(p, "corroboration_status", "uncorroborated"),
                "corroboration_url": getattr(p, "corroboration_url", ""),
                "discovery_category": p.discovery_category,
                "verification_status": p.verification_status,
                "application_email": p.application_email,
                "source_url": p.canonical_url or p.source_url,
                "requisition_id": getattr(p, "requisition_id", ""),
                "primary_skip_code": p.primary_skip_code,
                "skip_reason": p.skip_reason,
            }
            for p in postings
        ]
    }


@router.post("/drafts/{application_id}/revise")
def api_revise_draft(
    application_id: str,
    payload: DraftReviseRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    app_rec = db.scalar(select(Application).where(Application.id == application_id, Application.user_id == user.id).with_for_update())
    if not app_rec:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Draft application not found"})
    if app_rec.transport_status in ("sent", "sending", "send_uncertain"):
        raise HTTPException(status_code=409, detail={"code": "ALREADY_SENT", "message": "Sent or uncertain applications cannot be edited"})
    try:
        updated, invalidated = revise_application_draft(
            db,
            user=user,
            app_rec=app_rec,
            subject=payload.subject,
            body_text=payload.body_text,
            recipient_email=payload.recipient_email,
            cc_emails=payload.cc_emails,
            selected_cv_version_id=payload.selected_cv_version_id,
            review_notes=payload.review_notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_DRAFT", "message": str(exc)}) from exc
    db.commit()
    db.refresh(updated)
    posting = db.scalar(select(JobPosting).where(JobPosting.id == updated.job_posting_id))
    match = db.scalar(select(JobMatch).where(JobMatch.id == updated.job_match_id)) if updated.job_match_id else None
    batch = db.scalar(select(Batch).where(Batch.id == updated.batch_id))
    cv_ver = db.scalar(select(CvVersion).where(CvVersion.id == updated.selected_cv_version_id)) if updated.selected_cv_version_id else None
    latest_ob = get_latest_outbox_map(db, [updated.id]).get(updated.id)
    return {
        "approval_invalidated": invalidated,
        "draft": _serialize_review_draft(updated, posting, match, batch, cv_ver.cv_document_id if cv_ver else "", latest_ob),
    }


@router.post("/drafts/{application_id}/reject")
def api_reject_draft(
    application_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> dict:
    app = db.scalar(select(Application).where(
        Application.id == application_id, Application.user_id == user.id,
    ).with_for_update())
    if not app:
        raise HTTPException(404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})
    if app.transport_status not in ("draft", "ready", "failed", "cancelled"):
        raise HTTPException(409, detail={"code": "DRAFT_LOCKED", "message": "Only unscheduled drafts can be rejected"})
    if app.approval_state != "rejected":
        record_application_transition(
            db, user_id=user.id, application_id=app.id, field_name="approval_state",
            from_state=app.approval_state, to_state="rejected", actor=f"user:{user.id}",
            reason="Rejected during review",
        )
        app.approval_state = "rejected"
        app.approved_at = None
        app.approved_content_hash = ""
        app.skip_or_error_reason = "Rejected during review"
        db.commit()
    return {"id": app.id, "approval_state": app.approval_state}


@router.post("/drafts/{application_id}/revalidate")
def api_revalidate_draft(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    app_rec = db.scalar(select(Application).where(Application.id == application_id, Application.user_id == user.id).with_for_update())
    if not app_rec:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Draft application not found"})
    if app_rec.transport_status in ("sent", "sending", "send_uncertain"):
        raise HTTPException(status_code=409, detail={"code": "ALREADY_SENT", "message": "Sent or uncertain applications cannot be revalidated"})
    settings = get_settings()
    try:
        res = revalidate_application_draft(
            db,
            user=user,
            app_rec=app_rec,
            skip_network=(settings.environment == "test"),
        )
    except BundleImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    db.refresh(app_rec)
    posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id))
    match = db.scalar(select(JobMatch).where(JobMatch.id == app_rec.job_match_id)) if app_rec.job_match_id else None
    batch = db.scalar(select(Batch).where(Batch.id == app_rec.batch_id))
    cv_ver = db.scalar(select(CvVersion).where(CvVersion.id == app_rec.selected_cv_version_id)) if app_rec.selected_cv_version_id else None
    latest_ob = get_latest_outbox_map(db, [app_rec.id]).get(app_rec.id)
    return {
        **res,
        "draft": _serialize_review_draft(app_rec, posting, match, batch, cv_ver.cv_document_id if cv_ver else "", latest_ob),
    }


@router.post("/drafts/{application_id}/regenerate")
def api_regenerate_grounded_draft(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    app_rec = db.scalar(select(Application).where(Application.id == application_id, Application.user_id == user.id).with_for_update())
    if not app_rec:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Draft application not found"})
    if app_rec.transport_status in ("sent", "sending", "send_uncertain", "skipped"):
        raise HTTPException(status_code=409, detail={"code": "NOT_REGENERABLE", "message": "Only draft applications can be regenerated"})
    posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id, JobPosting.user_id == user.id))
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    cv_ver = db.scalar(select(CvVersion).where(CvVersion.id == app_rec.selected_cv_version_id, CvVersion.user_id == user.id)) if app_rec.selected_cv_version_id else None
    if not posting or not profile or not pref or not cv_ver:
        raise HTTPException(status_code=400, detail={"code": "MISSING_CONTEXT", "message": "Posting, profile, preferences, or CV missing"})
    facts = list(db.scalars(select(CvExtractedFact).where(CvExtractedFact.cv_version_id == cv_ver.id, CvExtractedFact.user_id == user.id)).all())
    draft = generate_grounded_application_draft(
        posting=posting,
        profile=profile,
        pref=pref,
        facts=facts,
        supporting_fact_ids=[f.id for f in facts[:6]],
        language=app_rec.language or "en",
        allow_provider=False,
    )
    updated, invalidated = revise_application_draft(
        db,
        user=user,
        app_rec=app_rec,
        subject=draft.subject if posting.subject_instruction else app_rec.subject or draft.subject,
        body_text=draft.body_text,
        review_notes=app_rec.review_notes,
    )
    updated.llm_provider_used = draft.provider_used
    updated.llm_model_used = draft.model_used
    updated.evidence_fact_ids_json = draft.evidence_fact_ids
    sync_application_hash(updated)
    db.commit()
    db.refresh(updated)
    match = db.scalar(select(JobMatch).where(JobMatch.id == updated.job_match_id)) if updated.job_match_id else None
    batch = db.scalar(select(Batch).where(Batch.id == updated.batch_id))
    latest_ob = get_latest_outbox_map(db, [updated.id]).get(updated.id)
    return {
        "approval_invalidated": invalidated,
        "draft": _serialize_review_draft(updated, posting, match, batch, cv_ver.cv_document_id, latest_ob),
    }


@router.post("/applications/{application_id}/cancel-outbox")
def api_cancel_application_outbox(
    application_id: str,
    payload: CancelOutboxRequest | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    app_rec = db.scalar(select(Application).where(Application.id == application_id, Application.user_id == user.id).with_for_update())
    if not app_rec:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})
    try:
        res = cancel_queued_outbox_entry(
            db,
            user=user,
            app_rec=app_rec,
            reason=(payload.reason if payload and payload.reason.strip() else "Cancelled by user from dashboard"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "CANCEL_NOT_ALLOWED", "message": str(exc)}) from exc
    db.refresh(app_rec)
    posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id))
    match = db.scalar(select(JobMatch).where(JobMatch.id == app_rec.job_match_id)) if app_rec.job_match_id else None
    batch = db.scalar(select(Batch).where(Batch.id == app_rec.batch_id))
    cv_ver = db.scalar(select(CvVersion).where(CvVersion.id == app_rec.selected_cv_version_id)) if app_rec.selected_cv_version_id else None
    latest_ob = get_latest_outbox_map(db, [app_rec.id]).get(app_rec.id)
    return {
        **res,
        "draft": _serialize_review_draft(app_rec, posting, match, batch, cv_ver.cv_document_id if cv_ver else "", latest_ob),
    }


@router.post("/applications/{application_id}/reconcile-outbox")
def api_reconcile_application_outbox(
    application_id: str,
    payload: ReconcileOutboxRequest | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    app_rec = db.scalar(select(Application).where(Application.id == application_id, Application.user_id == user.id).with_for_update())
    if not app_rec:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})
    try:
        res = reconcile_uncertain_outbox_entry(
            db,
            user=user,
            app_rec=app_rec,
            mark_failed_if_not_found=(payload.mark_failed_if_not_found if payload is not None else False),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "RECONCILE_NOT_ALLOWED", "message": str(exc)}) from exc
    db.refresh(app_rec)
    posting = db.scalar(select(JobPosting).where(JobPosting.id == app_rec.job_posting_id))
    match = db.scalar(select(JobMatch).where(JobMatch.id == app_rec.job_match_id)) if app_rec.job_match_id else None
    batch = db.scalar(select(Batch).where(Batch.id == app_rec.batch_id))
    cv_ver = db.scalar(select(CvVersion).where(CvVersion.id == app_rec.selected_cv_version_id)) if app_rec.selected_cv_version_id else None
    latest_ob = get_latest_outbox_map(db, [app_rec.id]).get(app_rec.id)
    return {
        **res,
        "draft": _serialize_review_draft(app_rec, posting, match, batch, cv_ver.cv_document_id if cv_ver else "", latest_ob),
    }


@router.post("/applications/{application_id}/retry-send")
def api_retry_failed_application(
    application_id: str,
    payload: RetrySendRequest | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if user.automation_stopped_at is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "AUTOMATION_STOPPED", "message": "Global Stop Automation is active; resume automation first."},
        )
    live_enabled, live_reason = resolve_effective_live_sending(db)
    if not live_enabled:
        raise HTTPException(
            status_code=409,
            detail={"code": "LIVE_SENDING_DISABLED", "message": live_reason or "Server LIVE_SENDING_ENABLED is false"},
        )
    reconcile_application_outbox_states(db, user_id=user.id)
    app_rec = db.scalar(select(Application).where(Application.id == application_id, Application.user_id == user.id).with_for_update())
    if not app_rec:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})

    latest_ob = get_latest_outbox_map(db, [app_rec.id]).get(app_rec.id)
    ob_status = (latest_ob.status if latest_ob else "") or ""

    # Never blindly retry sent, sending, or send_uncertain records!
    if app_rec.transport_status in ("sent", "send_uncertain", "sending") or ob_status in ("sent", "send_uncertain", "sending"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RETRY_NOT_ALLOWED",
                "message": (
                    f"Cannot retry send while status is '{ob_status or app_rec.transport_status}'. "
                    "Accepted or uncertain sends are never replayed without reconciliation."
                ),
            },
        )
    if app_rec.transport_status not in ("failed", "cancelled", "queued", "ready") and ob_status not in ("queued", "leased", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RETRY_NOT_ALLOWED",
                "message": f"Only failed, cancelled, or deferred queued sends can be retried; current status is '{app_rec.transport_status}'.",
            },
        )
    cur_hash = application_draft_hash(app_rec)
    if app_rec.approval_state != "approved" or app_rec.approved_content_hash != cur_hash:
        raise HTTPException(
            status_code=409,
            detail={"code": "APPROVAL_REQUIRED", "message": "Application must have a valid approval hash before retrying send"},
        )
    # Retry continues the original confirmed action. New content or a new sender needs a new confirmation.
    if not latest_ob or latest_ob.kind != "initial":
        raise HTTPException(status_code=409, detail={"code": "CONFIRM_SEND_REQUIRED", "message": "Review the sender and confirm Send Approved first."})
    prior_hash = db.scalar(select(ApplicationTransition.metadata_json["confirmed_hash"].as_string()).where(
        ApplicationTransition.user_id == user.id,
        ApplicationTransition.application_id == app_rec.id,
        ApplicationTransition.metadata_json["action_id"].as_string() == latest_ob.action_id,
    ))
    if prior_hash != cur_hash:
        raise HTTPException(status_code=409, detail={"code": "CONFIRM_SEND_REQUIRED", "message": "Confirm the current application in Send Approved."})
    conn = db.scalar(_sending_mailbox_query(user.id))
    if not conn or conn.id != latest_ob.mailbox_connection_id:
        raise HTTPException(status_code=409, detail={"code": "SENDER_CHANGED", "message": "Review the current sending mailbox and confirm again."})
    batch = db.scalar(select(Batch).where(Batch.id == app_rec.batch_id, Batch.user_id == user.id))
    if not batch:
        raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND", "message": "Parent batch not found"})
    if batch.mode != "demo_fixture":
        batch.mode = "live"

    # A queued or leased action already has an owner or a scheduled retry.
    if latest_ob and latest_ob.status in ("queued", "leased"):
        return {
            "ok": True,
            "application_id": app_rec.id,
            "outbox_entry_id": latest_ob.id,
            "outcome": "queued",
            "reused_existing_outbox": True,
        }

    prev_ts = app_rec.transport_status
    app_rec.transport_status = "draft"
    app_rec.skip_or_error_reason = ""
    entry, enq_err = enqueue_application_for_send(db, user=user, batch=batch, app_record=app_rec, conn=conn, expected_hash=cur_hash)
    if not entry:
        raise HTTPException(status_code=409, detail={"code": "RETRY_BLOCKED", "message": enq_err})
    record_application_transition(
        db,
        user_id=user.id,
        application_id=app_rec.id,
        field_name="transport_status",
        from_state=prev_ts,
        to_state="queued",
        actor=f"user:{user.id}",
        reason="Explicit retry of unsent application",
        metadata={"outbox_entry_id": entry.id},
    )
    db.commit()
    return {"ok": True, "application_id": app_rec.id, "outbox_entry_id": entry.id, "outcome": "queued", "reused_existing_outbox": False}


@router.post("/drafts/approve")
def api_approve_drafts(
    payload: DraftApproveRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    apps = list(
        db.scalars(
            select(Application).where(Application.id.in_(payload.application_ids), Application.user_id == user.id).order_by(Application.id).with_for_update()
        ).all()
    )
    app_map = {a.id: a for a in apps}
    approved_ids: list[str] = []
    failed: list[dict[str, str]] = []
    for aid in payload.application_ids:
        app_rec = app_map.get(aid)
        if not app_rec:
            failed.append({"id": aid, "error": "Application not found"})
            continue
        if payload.expected_hashes.get(aid) != application_draft_hash(app_rec):
            failed.append({"id": aid, "error": "Approval requires the current draft hash. Refresh and review this draft."})
            continue
        ok, msg = approve_application_for_send(
            db,
            user=user,
            app_rec=app_rec,
            review_notes=payload.review_notes,
            allow_third_party_override=payload.allow_third_party_override,
        )
        if ok:
            approved_ids.append(aid)
        else:
            failed.append({"id": aid, "error": msg})
    db.commit()
    return {"approved_ids": approved_ids, "failed": failed}


@router.post("/outbox/cancel-queued")
def api_cancel_all_queued_outbox(
    payload: CancelOutboxRequest | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Safely cancel all genuinely unsent queued outbox entries for the current user."""
    queued_entries = list(
        db.scalars(
            select(OutboxEntry)
            .where(
                OutboxEntry.user_id == user.id,
                OutboxEntry.status.in_(["queued", "leased"]),
                OutboxEntry.handoff_outcome == "none",
            )
            .order_by(OutboxEntry.created_at.asc())
        ).all()
    )
    cancelled_application_ids: list[str] = []
    cancelled_outbox_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    reason = (payload.reason if payload and payload.reason.strip() else "Cancelled unsent queued entries from dashboard")
    seen_apps: set[str] = set()
    for ob in queued_entries:
        if ob.application_id in seen_apps:
            continue
        seen_apps.add(ob.application_id)
        app_rec = db.scalar(select(Application).where(Application.id == ob.application_id, Application.user_id == user.id))
        if not app_rec:
            continue
        try:
            res = cancel_queued_outbox_entry(db, user=user, app_rec=app_rec, reason=reason)
            cancelled_application_ids.append(app_rec.id)
            cancelled_outbox_ids.extend(res.get("cancelled_outbox_ids", []))
        except ValueError as exc:
            skipped.append({"application_id": app_rec.id, "error": str(exc)})
    return {
        "ok": True,
        "cancelled_application_ids": cancelled_application_ids,
        "cancelled_outbox_ids": cancelled_outbox_ids,
        "skipped": skipped,
    }


@router.post("/drafts/send-approved")
def api_send_approved_drafts(
    payload: DraftSendApprovedRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if user.automation_stopped_at is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "AUTOMATION_STOPPED", "message": "Global Stop Automation is active; resume automation first."},
        )
    live_enabled, live_reason = resolve_effective_live_sending(db)
    if not live_enabled:
        raise HTTPException(
            status_code=409,
            detail={"code": "LIVE_SENDING_DISABLED", "message": live_reason or "Server LIVE_SENDING_ENABLED is false"},
        )

    conn = db.scalar(_sending_mailbox_query(user.id).with_for_update())
    if not conn:
        raise HTTPException(status_code=409, detail={"code": "NO_MAILBOX", "message": "Connect a sending mailbox in Settings first"})

    if conn.id != payload.expected_mailbox_id:
        raise HTTPException(status_code=409, detail={"code": "SENDER_CHANGED", "message": "Review the current sending mailbox and confirm again."})
    if not payload.acknowledge_incomplete_history:
        coverage = get_mailbox_dedup_coverage(db, conn=conn) if conn.provider in ("gmail_oauth", "smtp_imap") else {}
        if coverage.get("coverage_mode") != "applyflow_ledger_plus_gmail_sent":
            raise HTTPException(status_code=409, detail={"code": "INCOMPLETE_SENT_HISTORY", "message": "Review and acknowledge incomplete Sent-history coverage."})

    reconcile_application_outbox_states(db, user_id=user.id)
    apps = list(
        db.scalars(
            select(Application).where(Application.id.in_(payload.application_ids), Application.user_id == user.id).order_by(Application.id).with_for_update()
        ).all()
    )
    app_map = {a.id: a for a in apps}
    outbox_by_app = get_latest_outbox_map(db, list(app_map.keys()))

    queued_ids: list[str] = []
    blocked: list[dict[str, str]] = []
    results: list[dict[str, object]] = []

    for aid in dict.fromkeys(payload.application_ids):
        app_rec = app_map.get(aid)
        if not app_rec:
            blocked.append({"id": aid, "error": "Application not found"})
            continue

        latest_ob = outbox_by_app.get(app_rec.id)
        ob_status = (latest_ob.status if latest_ob else "") or ""

        # Duplicate & uncertain-send protection: never create another send attempt for sent, sending, or uncertain
        if app_rec.transport_status in ("sent", "provider_accepted", "sending", "send_uncertain") or ob_status in (
            "sent",
            "sending",
            "send_uncertain",
        ):
            blocked.append(
                {
                    "id": app_rec.id,
                    "error": f"Application is already '{ob_status or app_rec.transport_status}' — duplicate send prevented.",
                }
            )
            continue

        cur_hash = application_draft_hash(app_rec)
        if app_rec.approval_state != "approved" or not app_rec.approved_content_hash:
            blocked.append({"id": app_rec.id, "error": "Draft must be approved in Review Queue before sending"})
            continue
        if payload.expected_hashes.get(aid) != cur_hash:
            blocked.append({"id": aid, "error": "The draft changed since confirmation. Review it again."})
            continue
        if app_rec.approved_content_hash != cur_hash:
            blocked.append({"id": app_rec.id, "error": "APPROVAL_HASH_MISMATCH: Draft was edited after approval"})
            continue
        batch = db.scalar(select(Batch).where(Batch.id == app_rec.batch_id, Batch.user_id == user.id))
        if not batch:
            blocked.append({"id": app_rec.id, "error": "Parent batch not found"})
            continue
        if batch.mode not in ("live", "demo_fixture"):
            batch.mode = "live"

        # Idempotently reuse an existing unsent queued outbox entry if one already exists
        if latest_ob and latest_ob.status in ("queued", "leased"):
            if latest_ob.mailbox_connection_id != conn.id:
                blocked.append({"id": aid, "error": "An earlier confirmation already queued this application with another mailbox."})
                continue
            entry = latest_ob
        else:
            if app_rec.transport_status in ("failed", "cancelled"):
                app_rec.transport_status = "draft"
                app_rec.skip_or_error_reason = ""
            entry, enq_err = enqueue_application_for_send(
                db, user=user, batch=batch, app_record=app_rec, conn=conn, expected_hash=cur_hash,
            )
            if not entry:
                blocked.append({"id": app_rec.id, "error": enq_err})
                continue

        queued_ids.append(app_rec.id)

        results.append(
            {
                "application_id": app_rec.id,
                "outbox_entry_id": entry.id,
                "outcome": "queued",
                "transport_status": app_rec.transport_status,
                "provider_message_id": app_rec.provider_message_id or "",
                "sent_at": app_rec.sent_at.isoformat() if app_rec.sent_at else None,
                "error": app_rec.skip_or_error_reason or entry.last_error or "",
            }
        )

    db.commit()
    return {
        "queued_ids": queued_ids,
        "sent_ids": [],
        "failed_ids": [],
        "uncertain_ids": [],
        "blocked": blocked,
        "results": results,
    }


@router.post("/applications/{app_id}/create-gmail-draft")
def api_save_application_to_gmail_draft(
    app_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    app_rec = db.scalar(select(Application).where(Application.id == app_id, Application.user_id == user.id))
    if not app_rec:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND", "message": "Application not found"})
    conn = db.scalar(
        select(MailboxConnection).where(
            MailboxConnection.user_id == user.id,
            MailboxConnection.provider == "gmail_oauth",
            MailboxConnection.status == "connected",
            MailboxConnection.revoked_at.is_(None),
        )
    )
    if not conn:
        raise HTTPException(status_code=409, detail={"code": "NO_GMAIL_MAILBOX", "message": "No connected Gmail mailbox"})
    if GMAIL_COMPOSE_SCOPE not in (conn.granted_scopes_json or []):
        raise HTTPException(status_code=403, detail={"code": "GMAIL_COMPOSE_SCOPE_REQUIRED", "message": "Reconnect Gmail with optional Drafts (gmail.compose) scope enabled"})
    conn.save_drafts_in_gmail = True
    atts = _load_verified_attachments(db, app_rec=app_rec, storage=get_storage_adapter())
    draft_id = create_gmail_draft(
        db,
        conn=conn,
        outbound=OutboundEmailMessage(
            from_email=conn.sender_email,
            from_name=user.full_name or conn.sender_email,
            to_email=app_rec.recipient_email,
            cc_emails=list(app_rec.cc_emails_json or []),
            subject=app_rec.subject,
            body_text=app_rec.body_text,
            rfc_message_id="",
            attachments=atts,
        ),
    )
    app_rec.gmail_draft_id = draft_id
    db.commit()
    return {"gmail_draft_id": draft_id}


@router.post("/batches/{batch_id}/pause")
def api_pause_batch(batch_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    batch = db.scalar(select(Batch).where(Batch.id == batch_id, Batch.user_id == user.id).with_for_update())
    if not batch:
        raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND", "message": "Batch not found"})
    db.execute(update(Batch).where(Batch.id == batch.id, Batch.status.in_(["queued", "running"]))
               .values(status="paused", pause_reason="Paused by user", lease_token=None, lease_expires_at=None))
    db.commit()
    db.refresh(batch)
    return {"id": batch.id, "status": batch.status}


@router.post("/batches/{batch_id}/resume")
def api_resume_batch(batch_id: str, background_tasks: BackgroundTasks, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)) -> dict:
    batch = db.scalar(select(Batch).where(Batch.id == batch_id, Batch.user_id == user.id).with_for_update())
    if not batch:
        raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND", "message": "Batch not found"})
    if user.automation_stopped_at:
        raise HTTPException(status_code=409, detail={"code": "AUTOMATION_STOPPED", "message": "Resume automation first"})
    if batch.status in ("paused", "quota_paused"):
        # Mailbox authentication and legacy quota failures can pause an already finished batch.
        discovery_complete = (
            batch.discovery_stage == "completed"
            or any(row.get("stage") == "eligibility_and_drafting" and row.get("status") == "completed"
                   for row in batch.stage_status_json or [])
            or (batch.finished_at is not None and bool(batch.import_origin))
            or (batch.finished_at is not None and batch.discovery_stage is None
                and batch.checkpoint_json is None and batch.drafted_count > 0)
        )
        if discovery_complete:
            db.execute(update(Batch).where(Batch.id == batch.id, Batch.status == batch.status)
                       .values(status="completed", discovery_stage="completed", pause_reason="",
                               lease_token=None, lease_expires_at=None))
            db.commit()
            db.refresh(batch)
            return {"id": batch.id, "status": batch.status}
    if batch.status in ("paused", "quota_paused") or batch.discovery_stage == "deferred" and batch.status == "queued":
        if batch.import_origin or (batch.checkpoint_json is None and batch.discovered_count):
            raise HTTPException(status_code=409, detail={"code": "NO_CHECKPOINT", "message": "This batch has no discovery checkpoint"})
        resumed = db.execute(update(Batch).where(Batch.id == batch_id, Batch.status == batch.status)
                             .values(status="queued", discovery_stage="queued", finished_at=None,
                                     pause_reason="", lease_token=None, lease_expires_at=None))
        db.commit()
        if resumed.rowcount:
            _dispatch_requested_batch(background_tasks, db, batch)
    return {"id": batch.id, "status": batch.status}


@router.post("/batches/{batch_id}/cancel")
def api_cancel_batch(batch_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    now = utcnow()
    changed = db.execute(update(Batch).where(Batch.id == batch_id, Batch.user_id == user.id)
                         .values(status="cancelled", discovery_stage="cancelled", lease_token=None,
                                 lease_expires_at=None, pause_reason="Cancelled by user", finished_at=now))
    if not changed.rowcount:
        raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND", "message": "Batch not found"})
    # Lock the rows against a concurrent claim. The normal helper commits, so isolate its
    # commits in savepoints under this transaction. Workers retain ownership of active leases.
    pending = db.scalars(select(OutboxEntry).join(Application, Application.id == OutboxEntry.application_id)
                         .where(Application.batch_id == batch_id, Application.user_id == user.id,
                                OutboxEntry.user_id == user.id, OutboxEntry.status.in_(["queued", "leased"]))
                         .order_by(OutboxEntry.id).with_for_update(of=OutboxEntry)).all()
    cancelled = 0
    protected = 0
    for entry in pending:
        if entry.status == "leased" and (entry.lease_expires_at is None or as_utc(entry.lease_expires_at) > now):
            protected += 1
            continue
        with Session(bind=db.connection(), join_transaction_mode="create_savepoint") as nested:
            try:
                result = cancel_queued_outbox_entry(
                    nested, user=user, app_rec=nested.get(Application, entry.application_id),
                    reason="Batch cancelled by user",
                )
            except ValueError:
                protected += 1
            else:
                cancelled += len(result["cancelled_outbox_ids"])
    db.commit()
    return {"id": batch_id, "status": "cancelled", "cancelled_outbox_count": cancelled,
            "protected_outbox_count": protected}


@router.get("/schedules")
def api_list_schedules(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    scheds = db.scalars(select(Schedule).where(Schedule.user_id == user.id).order_by(Schedule.created_at.desc())).all()
    items = []
    for s in scheds:
        occs = db.scalars(
            select(ScheduleOccurrence)
            .where(ScheduleOccurrence.schedule_id == s.id, ScheduleOccurrence.user_id == user.id)
            .order_by(ScheduleOccurrence.scheduled_for_utc.desc())
            .limit(5)
        ).all()
        items.append(
            {
                "id": s.id,
                "name": s.name,
                "mode": s.mode,
                "days_of_week": s.days_of_week_json,
                "local_time_hhmm": s.local_time_hhmm,
                "timezone": s.timezone,
                "catch_up_window_minutes": s.catch_up_window_minutes,
                "is_active": s.is_active,
                "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
                "recent_occurrences": [
                    {
                        "id": o.id,
                        "scheduled_for_utc": o.scheduled_for_utc.isoformat(),
                        "local_date_str": o.local_date_str,
                        "status": o.status,
                        "dst_note": o.dst_note,
                    }
                    for o in occs
                ],
            }
        )
    return {"schedules": items}


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
def api_create_schedule(
    payload: ScheduleCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if payload.mode == "demo_fixture":
        raise HTTPException(
            status_code=400,
            detail={"code": "DEMO_FIXTURE_SCHEDULE_FORBIDDEN", "message": "Demo fixtures cannot be scheduled for automated runs."},
        )
    try:
        ensure_valid_timezone(payload.timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_TIMEZONE", "message": str(exc)}) from exc

    sched = Schedule(
        user_id=user.id,
        name=payload.name.strip(),
        mode=payload.mode,
        days_of_week_json=sorted({d for d in payload.days_of_week if 0 <= d <= 6}),
        local_time_hhmm=payload.local_time_hhmm,
        timezone=payload.timezone,
        catch_up_window_minutes=payload.catch_up_window_minutes,
        is_active=payload.is_active,
    )
    refresh_schedule_next_run(sched)
    db.add(sched)
    db.commit()
    db.refresh(sched)
    return {
        "id": sched.id,
        "next_run_at": sched.next_run_at.isoformat() if sched.next_run_at else None,
    }


@router.patch("/schedules/{schedule_id}")
def api_patch_schedule(
    schedule_id: str,
    payload: SchedulePatchRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    sched = db.scalar(select(Schedule).where(Schedule.id == schedule_id, Schedule.user_id == user.id))
    if not sched:
        raise HTTPException(status_code=404, detail={"code": "SCHEDULE_NOT_FOUND", "message": "Schedule not found"})
    if payload.name is not None:
        sched.name = payload.name.strip()
    if payload.mode is not None:
        if payload.mode == "demo_fixture":
            raise HTTPException(
                status_code=400,
                detail={"code": "DEMO_FIXTURE_SCHEDULE_FORBIDDEN", "message": "Demo fixtures cannot be scheduled for automated runs."},
            )
        sched.mode = payload.mode
    if payload.days_of_week is not None:
        sched.days_of_week_json = sorted({d for d in payload.days_of_week if 0 <= d <= 6})
    if payload.local_time_hhmm is not None:
        sched.local_time_hhmm = payload.local_time_hhmm
    if payload.timezone is not None:
        try:
            ensure_valid_timezone(payload.timezone)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": "INVALID_TIMEZONE", "message": str(exc)}) from exc
        sched.timezone = payload.timezone
    if payload.is_active is not None:
        sched.is_active = payload.is_active
    refresh_schedule_next_run(sched)
    db.commit()
    return {"id": sched.id, "is_active": sched.is_active, "next_run_at": sched.next_run_at.isoformat() if sched.next_run_at else None}


@router.delete("/schedules/{schedule_id}")
def api_delete_schedule(
    schedule_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    sched = db.scalar(select(Schedule).where(Schedule.id == schedule_id, Schedule.user_id == user.id))
    if not sched:
        raise HTTPException(status_code=404, detail={"code": "SCHEDULE_NOT_FOUND", "message": "Schedule not found"})
    db.delete(sched)
    db.commit()
    return {"ok": True}
