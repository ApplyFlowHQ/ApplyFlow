"""Automated follow-up evaluation with strict inbox-freshness, stop-rule, and thread-continuity enforcement."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import normalize_email_dedupe_key
from app.db.base import as_utc, new_uuid, utcnow
from app.db.models import (
    Application,
    CandidateProfile,
    MailboxConnection,
    MessageEvent,
    OutboxEntry,
    RecipientHistory,
    SearchPreference,
    User,
)
from app.services.mailbox.base import build_rfc_message_id
from app.services.mailbox.gmail_sent import is_nonreply_address
from app.services.mailbox.ms_graph import DELTA_COMPLETE, graph_can_read
from app.services.verification import is_demo_email_or_domain


def render_followup_body(*, app_rec: Application, profile: CandidateProfile) -> str:
    sig = (profile.signature_text or f"Best regards,\n{profile.full_name}").strip()
    if app_rec.language == "ar":
        return (
            f"السادة فريق التوظيف في {app_rec.company_name} المحترمين،\n\n"
            f"أود المتابعة بشأن طلبي لوظيفة {app_rec.role_title} المرسل سابقاً. "
            f"ما زلت مهتماً جداً بهذه الفرصة وسأكون سعيداً بتزويدكم بأي معلومات إضافية.\n\n{sig}"
        )
    return (
        f"Dear {app_rec.company_name} Hiring Team,\n\n"
        f"I am following up politely on my recent application for the {app_rec.role_title} position. "
        f"I remain very interested in the role and would be glad to share any additional details you may need.\n\n{sig}"
    )


def _latest(db, app):
    return db.scalar(select(OutboxEntry).where(
        OutboxEntry.application_id == app.id, OutboxEntry.user_id == app.user_id, OutboxEntry.kind == "followup"
    ).order_by(OutboxEntry.created_at.desc()))


def _event(db, entry, kind):
    return db.scalar(select(MessageEvent).where(
        MessageEvent.application_id == entry.application_id, MessageEvent.user_id == entry.user_id,
        MessageEvent.rfc_message_id == entry.rfc_message_id, MessageEvent.event_type == kind,
    ))


def _parent_id(conn, app):
    # Legacy Graph JSON sends stored a local RFC ID as the provider ID without transmitting it.
    if conn.provider == "ms_graph_oauth" and app.provider_message_id == app.rfc_message_id:
        return ""
    return app.rfc_message_id


def _fresh(db, conn, app):
    last = as_utc(conn.inbox_last_polled_at)
    readable = conn.provider != "gmail_oauth" or "https://www.googleapis.com/auth/gmail.readonly" in (conn.granted_scopes_json or [])
    if conn.provider == "ms_graph_oauth":
        readable = (graph_can_read(conn) and (conn.inbox_cursor or "").startswith(DELTA_COMPLETE)
                    and app.provider_message_id and app.provider_thread_id and _parent_id(conn, app))
        if readable and app.follow_up_count:
            sent = db.scalars(select(MessageEvent).where(
                MessageEvent.user_id == app.user_id, MessageEvent.application_id == app.id,
                MessageEvent.event_type == "followup_sent", MessageEvent.direction == "outbound")).all()
            readable = (len(sent) == app.follow_up_count and all(
                e.provider_message_id and e.rfc_message_id and e.provider_message_id != e.rfc_message_id
                and e.details_json.get("provider_thread_id") for e in sent))
    return bool(readable and conn.status == "connected" and not conn.revoked_at and conn.inbox_poll_enabled and conn.inbox_health_status == "healthy" and last
                and last >= utcnow() - timedelta(minutes=get_settings().inbox_stale_minutes))


def _check(db, app, conn):
    if app.is_demo or is_demo_email_or_domain(app.recipient_email):
        raise ValueError("Demo/example application is excluded from follow-up")
    user = db.get(User, app.user_id)
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == app.user_id))
    if not user or user.deleted_at or user.is_suspended or user.automation_stopped_at:
        raise ValueError("User automation stopped or unavailable")
    if not pref or app.follow_up_count >= min(pref.followup_max_count, 2):
        raise ValueError("Follow-ups disabled or maximum count reached")
    if app.transport_status != "sent":
        raise ValueError("Follow-up requires an accepted application")
    if app.response_status in ("human_reply", "permanent_bounce") or app.outcome_status != "open":
        raise ValueError("Follow-up stopped by response or outcome")
    if not conn or conn.id != app.mailbox_connection_id or conn.user_id != app.user_id or conn.revoked_at or conn.status != "connected":
        raise ValueError("Original sending mailbox unavailable")
    if conn.provider != "ms_graph_oauth" and not app.rfc_message_id and (conn.provider != "gmail_oauth" or not app.provider_thread_id):
        raise ValueError("Original message and thread identifiers are unavailable")
    key = normalize_email_dedupe_key(app.recipient_email)
    if is_nonreply_address(key):
        raise ValueError("Follow-up blocked for a non-reply address")
    suppressed = db.scalar(select(RecipientHistory.id).where(
        RecipientHistory.user_id == app.user_id, RecipientHistory.normalized_email == key, RecipientHistory.source == "suppressed"))
    if suppressed or key in {normalize_email_dedupe_key(e) for e in getattr(pref, "excluded_recipients_json", []) or []}:
        raise ValueError("Recipient is suppressed")


def followup_view(db, app):
    entry = _latest(db, app)
    if not entry:
        return None
    draft = _event(db, entry, "followup_draft")
    conn = db.get(MailboxConnection, app.mailbox_connection_id) if app.mailbox_connection_id else None
    return {"id": entry.id, "status": entry.status, "error": entry.last_error,
            "confirm_inbox_required": not (conn and conn.user_id == app.user_id and _fresh(db, conn, app)),
            **(draft.details_json if draft else {})}


def prepare_followup(db, *, app, manual=False):
    app = db.scalars(select(Application).where(Application.id == app.id, Application.user_id == app.user_id)
                     .with_for_update().execution_options(populate_existing=True)).one()
    conn = db.get(MailboxConnection, app.mailbox_connection_id) if app.mailbox_connection_id else None
    _check(db, app, conn)
    previous = _latest(db, app)
    if previous and previous.status != "sent" and not (manual and previous.status in ("cancelled", "failed")):
        return False
    if not app.next_follow_up_at or as_utc(app.next_follow_up_at) > utcnow():
        raise ValueError("Follow-up is not due")
    if not manual and not _fresh(db, conn, app):
        raise ValueError("Inbox monitoring is disabled or stale")
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == app.user_id))
    if not profile:
        raise ValueError("Candidate profile unavailable")
    action_id = new_uuid()
    rfc = build_rfc_message_id(conn.sender_email, action_id)
    subject = app.subject if app.subject.lower().startswith("re:") else f"Re: {app.subject}"
    db.add(OutboxEntry(user_id=app.user_id, application_id=app.id, mailbox_connection_id=conn.id, kind="followup",
                       action_id=action_id, rfc_message_id=rfc, status="awaiting_approval"))
    db.add(MessageEvent(user_id=app.user_id, application_id=app.id, event_type="followup_draft", rfc_message_id=rfc,
        details_json={"sender": conn.sender_email, "from_name": profile.full_name, "recipient": app.recipient_email,
                      "subject": subject, "body": render_followup_body(app_rec=app, profile=profile),
                      "thread": app.provider_thread_id, "parent": _parent_id(conn, app)}))
    db.flush()
    return True


def _check_snapshot(db, app, entry, conn):
    _check(db, app, conn)
    draft = _event(db, entry, "followup_draft")
    if not draft:
        raise ValueError("Follow-up has no reviewed draft")
    data = draft.details_json
    # Older Graph drafts can contain the local-only legacy RFC ID. Omit it on handoff.
    if (conn.provider == "ms_graph_oauth" and app.provider_message_id == app.rfc_message_id
            and data.get("parent") == app.rfc_message_id):
        data = {**data, "parent": ""}
    if (entry.mailbox_connection_id != conn.id or data.get("sender") != conn.sender_email
            or data.get("recipient") != app.recipient_email or data.get("thread") != app.provider_thread_id
            or data.get("parent") != _parent_id(conn, app)):
        raise ValueError("Follow-up sender, recipient, or original message changed")
    return data


def approve_followup(db, *, app, draft_id, confirm_inbox):
    entry = db.scalar(select(OutboxEntry).where(
        OutboxEntry.id == draft_id, OutboxEntry.application_id == app.id, OutboxEntry.user_id == app.user_id,
        OutboxEntry.kind == "followup").with_for_update().execution_options(populate_existing=True))
    if not entry:
        raise ValueError("Follow-up draft unavailable")
    if _event(db, entry, "followup_approved"):
        return entry
    if entry.status != "awaiting_approval":
        raise ValueError("Follow-up draft is no longer awaiting approval")
    conn = db.get(MailboxConnection, entry.mailbox_connection_id)
    _check_snapshot(db, app, entry, conn)
    if not _fresh(db, conn, app) and not confirm_inbox:
        raise ValueError("Check the original mailbox for replies before approving")
    entry.status = "queued"
    db.add(MessageEvent(user_id=app.user_id, application_id=app.id, event_type="followup_approved",
                        rfc_message_id=entry.rfc_message_id, details_json={"manual_inbox_check": confirm_inbox}))
    db.commit()
    return entry


def approved_followup_payload(db, app, entry, conn):
    data = _check_snapshot(db, app, entry, conn)
    approved = _event(db, entry, "followup_approved")
    if not approved:
        raise ValueError("Explicit follow-up approval required")
    if not _fresh(db, conn, app):
        manual = approved.details_json.get("manual_inbox_check")
        if not manual or as_utc(approved.occurred_at) < utcnow() - timedelta(minutes=get_settings().inbox_stale_minutes):
            raise ValueError("Inbox check expired before handoff")
    return data


def evaluate_and_queue_followups_for_user(db: Session, *, user_id: str) -> tuple[int, list[str]]:
    """Prepare due drafts for review. Only explicit user approval queues delivery."""
    apps = db.scalars(select(Application).where(Application.user_id == user_id, Application.is_demo.is_(False),
        Application.transport_status == "sent", Application.next_follow_up_at <= utcnow())).all()
    count = 0
    reasons = []
    for app in apps:
        try:
            count += int(prepare_followup(db, app=app))
        except ValueError as exc:
            reasons.append(f"{app.id}: {exc}")
    db.commit()
    return count, reasons
