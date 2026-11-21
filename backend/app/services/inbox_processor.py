"""Inbox polling, precise reply/bounce correlation, and response/outcome classification."""

from __future__ import annotations

import re
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.metrics import metrics
from app.core.security import normalize_email_dedupe_key
from app.db.base import utcnow
from app.db.models import Application, MailboxConnection, MessageEvent, OutboxEntry
from app.services.dedupe import suppress_recipient
from app.services.mailbox.base import InboundMessageRecord
from app.services.mailbox.ms_graph import DELTA_COMPLETE, DELTA_PENDING
from app.services.outbox import get_mailbox_transport

EMAIL_EXTRACT_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

BOUNCE_PATTERNS = re.compile(
    r"(delivery\s+status\s+notification\s*\(failure\)|550\s+5\.1\.1|user\s+unknown|address\s+not\s+found|"
    r"undeliverable|permanent\s+failure|mailbox\s+unavailable)",
    re.IGNORECASE,
)
DELAY_PATTERNS = re.compile(
    r"(delivery\s+status\s+notification\s*\(delay\)|delivery\s+delayed|will\s+retry|4\.\d\.\d)",
    re.IGNORECASE,
)
OOO_PATTERNS = re.compile(
    r"(out\s+of\s+(?:the\s+)?office|automatic\s+reply\s*:\s*ooo|on\s+(?:annual\s+)?leave|vacation\s+responder|إجازة)",
    re.IGNORECASE,
)
AUTO_ACK_PATTERNS = re.compile(
    r"(thank\s+you\s+for\s+(?:applying|your\s+application)|application\s+(?:has\s+been\s+)?received|"
    r"we\s+have\s+received\s+your\s+cv|automated\s+confirmation|do\s+not\s+reply\s+to\s+this\s+automated|تم\s+استلام\s+طلبك)",
    re.IGNORECASE,
)
INTERVIEW_PATTERNS = re.compile(
    r"(schedule\s+(?:an\s+)?interview|invite\s+you\s+(?:for|to)\s+(?:an\s+)?interview|available\s+for\s+a\s+call|"
    r"technical\s+interview|مقابلة\s+عمل)",
    re.IGNORECASE,
)
REJECTION_PATTERNS = re.compile(
    r"(unfortunately.{0,50}(?:cannot proceed|unsuccessful)|regret\s+to\s+inform|decided\s+to\s+move\s+forward\s+with\s+other|not\s+selected|"
    r"won't\s+be\s+proceeding|نأسف\s+لإبلاغك|لم\s+يتم\s+اختيارك)",
    re.IGNORECASE,
)


def _extract_email(raw_header: str) -> str:
    addr = parseaddr(raw_header or "")[1]
    return addr if EMAIL_EXTRACT_RE.fullmatch(addr) else ""


def classify_inbound_message(rec: InboundMessageRecord) -> tuple[str, str | None, float]:
    body = re.split(r"(?im)^On .+ wrote:\s*$|^[- ]*Original Message[- ]*$|^From:\s", rec.body_snippet or "", maxsplit=1)[0]
    body = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))
    blob = f"{rec.subject}\n{body}"
    sender = _extract_email(rec.sender_email).lower().partition("@")[0]
    report = sender in ("mailer-daemon", "postmaster") or "delivery-status" in str(rec.headers).lower()
    if report and DELAY_PATTERNS.search(blob):
        return "delivery_delay", None, 0.88
    if report and BOUNCE_PATTERNS.search(blob):
        return "permanent_bounce", None, 0.95
    if OOO_PATTERNS.search(blob):
        return "ooo", None, 0.88
    outcome_text = body if rec.subject.lower().startswith("re:") else blob
    if REJECTION_PATTERNS.search(outcome_text):
        return "human_reply", "rejected", 0.82
    if INTERVIEW_PATTERNS.search(outcome_text):
        return "human_reply", "interview", 0.82
    headers = {k.lower(): v.lower() for k, v in rec.headers.items()}
    if AUTO_ACK_PATTERNS.search(blob) or headers.get("auto-submitted", "no") != "no" or report:
        return "auto_ack", None, 0.85
    return "human_reply", None, 0.78


def correlate_inbound_record(db: Session, *, conn: MailboxConnection, rec: InboundMessageRecord) -> Application | None:
    """Resolve unique message evidence inside the polling tenant and sending mailbox."""
    scope = select(Application).where(Application.user_id == conn.user_id,
        Application.mailbox_connection_id == conn.id, Application.is_demo.is_(False),
        Application.transport_status.in_(["sent", "provider_accepted", "sending", "send_uncertain"]))
    refs = re.findall(r"<[^<>\s]+>", f"{rec.references} {rec.in_reply_to}")
    for ref in reversed(refs):
        outbound = select(OutboxEntry.application_id).where(OutboxEntry.user_id == conn.user_id,
            OutboxEntry.mailbox_connection_id == conn.id, OutboxEntry.rfc_message_id == ref,
            OutboxEntry.status.in_(["sent", "sending", "send_uncertain"]))
        apps = db.scalars(scope.where((Application.rfc_message_id == ref) | Application.id.in_(outbound)).limit(2)).all()
        if apps:
            return apps[0] if len(apps) == 1 else None
    if rec.provider_thread_id:
        sent_followups = select(MessageEvent.application_id).where(
            MessageEvent.user_id == conn.user_id, MessageEvent.event_type == "followup_sent",
            MessageEvent.direction == "outbound",
            MessageEvent.details_json["provider_thread_id"].as_string() == rec.provider_thread_id,
        )
        apps = db.scalars(scope.where((Application.provider_thread_id == rec.provider_thread_id)
                                      | Application.id.in_(sent_followups)).limit(2)).all()
        if len(apps) == 1:
            return apps[0]
    return None


def ingest_inbound_messages(
    db: Session,
    *,
    conn: MailboxConnection,
    records: list[InboundMessageRecord],
    next_cursor: str | None = None,
) -> int:
    """Process inbound messages for `conn.user_id`, deduplicating events by provider_message_id."""
    processed = 0
    for rec in records:
        app = correlate_inbound_record(db, conn=conn, rec=rec)
        if not app:
            continue

        # Ignore our own outbound messages echoed in threads
        sender_addr = _extract_email(rec.sender_email)
        if not sender_addr or normalize_email_dedupe_key(sender_addr) == normalize_email_dedupe_key(conn.sender_email):
            continue

        if not rec.provider_message_id and not rec.rfc_message_id:
            continue
        identity = (MessageEvent.provider_message_id == rec.provider_message_id if rec.provider_message_id
                    else MessageEvent.rfc_message_id == rec.rfc_message_id)
        if rec.rfc_message_id:
            identity = identity | (MessageEvent.rfc_message_id == rec.rfc_message_id)
        existing_evt = db.scalar(
            select(MessageEvent).where(
                MessageEvent.application_id == app.id,
                MessageEvent.user_id == conn.user_id, identity,
                MessageEvent.direction == "inbound",
            )
        )
        if existing_evt:
            continue

        resp_type, inferred_outcome, conf = classify_inbound_message(rec)
        if app.response_status not in ("human_reply", "permanent_bounce") or resp_type in ("human_reply", "permanent_bounce"):
            app.response_status = resp_type
        if inferred_outcome and app.outcome_status == "open":
            app.outcome_status = inferred_outcome
            app.outcome_confidence = conf

        # Stop pending follow-ups immediately on human reply, permanent bounce, or inferred rejection/interview
        if resp_type in ("human_reply", "permanent_bounce") or inferred_outcome in ("rejected", "interview"):
            app.next_follow_up_at = None
            db.query(OutboxEntry).filter(OutboxEntry.user_id == conn.user_id, OutboxEntry.application_id == app.id,
                OutboxEntry.kind == "followup", OutboxEntry.status.in_(["awaiting_approval", "queued"])).update(
                    {"status": "cancelled", "last_error": f"Stopped by {resp_type}"})
        if resp_type == "permanent_bounce" and app.recipient_email:
            targets = {normalize_email_dedupe_key(a) for a in [app.recipient_email, *(app.cc_emails_json or [])]}
            reported = {normalize_email_dedupe_key(a) for a in EMAIL_EXTRACT_RE.findall(rec.body_snippet or "")}
            failed = targets & reported
            if not reported and len(targets) == 1:
                failed = targets
            for addr in failed:
                suppress_recipient(db, user_id=conn.user_id, email=addr, notes="Permanent bounce detected in inbox", commit=False)

        db.add(
            MessageEvent(
                user_id=conn.user_id,
                application_id=app.id,
                event_type=resp_type,
                direction="inbound",
                provider_message_id=rec.provider_message_id,
                rfc_message_id=rec.rfc_message_id,
                in_reply_to=rec.in_reply_to,
                sender_email=sender_addr[:254],
                subject=rec.subject[:300],
                snippet=rec.body_snippet[:1000],
                classification_confidence=conf,
                occurred_at=rec.received_at,
                details_json={"inferred_outcome": inferred_outcome},
            )
        )
        if resp_type == "permanent_bounce":
            metrics.inc("bounces_detected")
        elif resp_type == "human_reply":
            metrics.inc("human_replies_detected")
        db.flush()  # Session autoflush is disabled; the next record may repeat this provider ID.
        processed += 1

    if next_cursor is not None:
        conn.inbox_cursor = next_cursor
        if next_cursor.startswith("pending:"):
            conn.inbox_health_status = "partial"
        else:
            conn.inbox_last_polled_at = utcnow()
            conn.inbox_health_status = "healthy"
    db.commit()
    return processed


def poll_mailbox_connection(db: Session, conn: MailboxConnection) -> int:
    """Poll one mailbox incrementally and update inbox health status."""
    lock = select(MailboxConnection).where(
        MailboxConnection.id == conn.id, MailboxConnection.user_id == conn.user_id
    ).with_for_update().execution_options(populate_existing=True)
    conn = db.scalars(lock).one()
    if not conn.inbox_poll_enabled or conn.status != "connected" or conn.revoked_at:
        return 0
    try:
        transport = get_mailbox_transport(db, conn)
        cursor, sender = conn.inbox_cursor, conn.sender_email
        records, next_cursor = transport.poll_inbox(cursor)
        # Credential refresh can commit and release the original mailbox lock.
        conn = db.scalars(lock).one()
        if (conn.inbox_cursor != cursor or conn.sender_email != sender or not conn.inbox_poll_enabled
                or conn.revoked_at or conn.status != "connected"):
            return 0
        if conn.provider == "ms_graph_oauth" and not next_cursor.startswith((DELTA_COMPLETE, DELTA_PENDING)):
            raise ValueError("Missing Graph inbox checkpoint")
        return ingest_inbound_messages(db, conn=conn, records=records, next_cursor=next_cursor)
    except Exception as exc:
        db.rollback()
        conn.inbox_health_status = "error"
        conn.last_error = f"Inbox polling error ({type(exc).__name__})"
        db.commit()
        return 0
