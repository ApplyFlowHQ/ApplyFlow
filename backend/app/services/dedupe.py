"""Durable transactional recipient deduplication and historical application import."""

from __future__ import annotations

from sqlalchemy import String, cast, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import normalize_email_dedupe_key, normalize_email_display
from app.db.base import utcnow
from app.db.models import (
    Application,
    GmailSentRecord,
    JobPosting,
    MailboxConnection,
    MessageEvent,
    RecipientHistory,
    SearchPreference,
)
from app.services.verification import is_demo_email_or_domain


class DuplicateRecipientError(ValueError):
    """Raised when a recipient (To or CC) is already in the user's contact or suppression history."""


def _override(db: Session, user_id: str, app: Application | None) -> tuple[str, set[str]]:
    if not app:
        return "", set()
    event = db.scalar(select(MessageEvent).where(
        MessageEvent.user_id == user_id, MessageEvent.application_id == app.id,
        MessageEvent.event_type == "different_vacancy_override",
    ).order_by(MessageEvent.created_at.desc()))
    address = normalize_email_dedupe_key(app.recipient_email)
    if event and event.details_json.get("posting_id") == app.job_posting_id and event.details_json.get("recipient") == address:
        return address, set(event.details_json.get("prior_applications", []))
    return "", set()


def record_duplicate_override(db: Session, *, app: Application, reason: str) -> None:
    from app.services.mailbox.gmail_sent import is_recruitment_address

    if not reason.strip() or len(reason) > 500:
        raise ValueError("Explain the different vacancy in 1 to 500 characters")
    if app.is_demo or app.transport_status != "skipped" or app.primary_skip_code != "PREVIOUSLY_CONTACTED":
        raise ValueError("This application is not excluded solely for previous contact")
    addr = normalize_email_dedupe_key(app.recipient_email)
    if not is_recruitment_address(addr):
        raise ValueError("A different-vacancy override requires a recruitment inbox")
    hits = find_existing_recipients(db, user_id=app.user_id, emails=[addr, *(app.cc_emails_json or [])])
    if set(hits) != {addr} or not _prior_contact(hits[addr]):
        raise ValueError("Only a prior contact at the primary recruitment address can be overridden")
    post = db.scalar(select(JobPosting).where(JobPosting.id == app.job_posting_id, JobPosting.user_id == app.user_id))
    if not post:
        raise ValueError("Vacancy evidence is missing")
    previous = db.execute(select(JobPosting, Application.id).join(Application, Application.job_posting_id == JobPosting.id).where(
        JobPosting.user_id == app.user_id, Application.user_id == app.user_id, Application.id != app.id,
        Application.recipient_normalized == addr, Application.transport_status.in_(["sent", "provider_accepted"]),
    )).all()
    for prior, _ in previous:
        if (prior.id == post.id or post.canonical_url_hash and prior.canonical_url_hash == post.canonical_url_hash
                or post.requisition_id and post.requisition_id == prior.requisition_id and post.company_name == prior.company_name):
            raise ValueError("A prior application already targets this vacancy")
    db.add(MessageEvent(user_id=app.user_id, application_id=app.id, event_type="different_vacancy_override",
                        snippet=reason.strip(), details_json={"posting_id": post.id, "recipient": addr,
                            "prior_applications": [app_id for _, app_id in previous]}))
    db.flush()


def _prior_contact(row) -> bool:
    if isinstance(row, RecipientHistory):
        return row.source in ("sent", "imported", "gmail_sent_sync")
    if isinstance(row, Application):
        return row.transport_status in ("sent", "provider_accepted")
    return isinstance(row, GmailSentRecord)


def find_existing_recipients(
    db: Session,
    *,
    user_id: str,
    emails: list[str],
    exclude_application_id: str | None = None,
) -> dict[str, RecipientHistory | GmailSentRecord | Application | str]:
    """Return mapping of normalized_email -> RecipientHistory or GmailSentRecord for any contacted/suppressed addresses."""
    norm_keys = [normalize_email_dedupe_key(e) for e in emails if e and not is_demo_email_or_domain(e)]
    if not norm_keys:
        return {}
    out: dict[str, RecipientHistory | GmailSentRecord | Application | str] = {}
    rows = db.scalars(
        select(RecipientHistory).where(
            RecipientHistory.user_id == user_id,
            RecipientHistory.normalized_email.in_(norm_keys),
        )
    ).all()
    for row in rows:
        if exclude_application_id and row.first_application_id == exclude_application_id and row.source == "reserved":
            continue
        out[row.normalized_email] = row

    from app.services.mailbox.gmail_sent import external_contact_key, is_nonreply_address

    sent_rows = db.execute(
        select(GmailSentRecord, MailboxConnection.sender_email).join(
            MailboxConnection, MailboxConnection.id == GmailSentRecord.mailbox_connection_id).where(
            GmailSentRecord.user_id == user_id, MailboxConnection.user_id == user_id,
            or_(GmailSentRecord.recipient_normalized.in_(norm_keys),
                *(cast(GmailSentRecord.contact_keys_json, String).contains(key, autoescape=True) for key in norm_keys)),
        )
    )
    for srow, sender in sent_rows:
        if not srow.contact_keys_json and srow.confidence_reason == "Self-sent message":
            continue
        for key in set(srow.contact_keys_json or [srow.recipient_normalized]).intersection(norm_keys):
            if external_contact_key(key, sender):
                current = out.get(key)
                if current is None or isinstance(current, RecipientHistory) and current.source == "gmail_sent_sync":
                    out[key] = srow
    app = db.scalar(select(Application).where(Application.id == exclude_application_id,
                                                Application.user_id == user_id)) if exclude_application_id else None
    override, known_prior = _override(db, user_id, app)
    if override in out and _prior_contact(out[override]):
        del out[override]
    # Legacy accepted applications may predate recipient reservations.
    prior_apps = db.scalars(select(Application).where(
        Application.user_id == user_id, Application.is_demo.is_(False),
        or_(Application.recipient_normalized.in_(norm_keys), func.lower(Application.recipient_email).in_(norm_keys),
            *(func.lower(cast(Application.cc_emails_json, String)).contains(key, autoescape=True) for key in norm_keys)),
        Application.transport_status.in_(["queued", "sending", "sent", "provider_accepted", "send_uncertain"]),
    )).all()
    for prior in prior_apps:
        if prior.id == exclude_application_id:
            continue
        for raw in [prior.recipient_email, *(prior.cc_emails_json or [])]:
            key = normalize_email_dedupe_key(raw)
            if key in norm_keys and (key != override or prior.id not in known_prior or not _prior_contact(prior)):
                if not _prior_contact(prior) or key not in out:
                    out[key] = prior
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user_id))
    blocked = {normalize_email_dedupe_key(e) for e in (getattr(pref, "excluded_recipients_json", None) or [])}
    for key in norm_keys:
        if key in blocked or is_nonreply_address(key):
            out[key] = "Blocked recipient or non-reply address"
    return out


def reserve_recipients_atomically(
    db: Session,
    *,
    user_id: str,
    application_id: str | None,
    to_email: str,
    cc_emails: list[str],
    company_name: str = "",
    reject_demo: bool = False,
) -> list[RecipientHistory]:
    """
    Transactionally reserve To and all CC recipients for `user_id` using DB unique constraints.
    Uses a SAVEPOINT so a uniqueness conflict cleanly rolls back only the reservation attempt.
    """
    all_emails = [to_email, *cc_emails]
    seen_norm: set[str] = set()
    to_insert: list[tuple[str, str]] = []
    for raw in all_emails:
        if not raw:
            continue
        if reject_demo and is_demo_email_or_domain(raw):
            raise ValueError(f"Demo/example recipients cannot be reserved for real sending ({raw})")
        display = normalize_email_display(raw)
        norm = normalize_email_dedupe_key(display)
        if norm not in seen_norm:
            seen_norm.add(norm)
            to_insert.append((norm, display))

    conflicts = find_existing_recipients(db, user_id=user_id, emails=all_emails, exclude_application_id=application_id)
    if conflicts:
        raise DuplicateRecipientError(f"Recipient already contacted, reserved or blocked ({', '.join(conflicts)})")
    app = db.scalar(select(Application).where(Application.id == application_id, Application.user_id == user_id)) if application_id else None
    override, _ = _override(db, user_id, app)
    created: list[RecipientHistory] = []
    try:
        with db.begin_nested():
            for norm, display in to_insert:
                if norm == override:
                    prior = db.scalar(select(RecipientHistory).where(
                        RecipientHistory.user_id == user_id, RecipientHistory.normalized_email == norm
                    ).with_for_update())
                    if prior and _prior_contact(prior):
                        # Recheck after the ledger lock: another transaction may have queued this inbox.
                        if norm in find_existing_recipients(db, user_id=user_id, emails=[display],
                                                            exclude_application_id=application_id):
                            raise DuplicateRecipientError("Recruitment inbox has an active or blocked application")
                        continue
                existing_res = (
                    db.scalar(
                        select(RecipientHistory).where(
                            RecipientHistory.user_id == user_id,
                            RecipientHistory.normalized_email == norm,
                            RecipientHistory.source == "reserved",
                            RecipientHistory.first_application_id == application_id,
                        )
                    )
                    if application_id
                    else None
                )
                if existing_res:
                    created.append(existing_res)
                    continue
                rec = RecipientHistory(
                    user_id=user_id,
                    normalized_email=norm,
                    display_email=display,
                    source="reserved",
                    first_application_id=application_id,
                    company_name=company_name[:200],
                )
                db.add(rec)
                created.append(rec)
            db.flush()
    except IntegrityError as exc:
        err_text = str(exc).lower()
        if "unique" in err_text or "uq_recipient_user_email" in err_text or "duplicate key" in err_text:
            raise DuplicateRecipientError(
                f"Recipient or CC already contacted or suppressed in prior history ({', '.join(d for _, d in to_insert)})"
            ) from exc
        raise
    return created


def mark_recipients_sent(db: Session, *, user_id: str, emails: list[str]) -> None:
    norm_keys = [normalize_email_dedupe_key(e) for e in emails if e]
    if not norm_keys:
        return
    db.execute(update(RecipientHistory).where(
        RecipientHistory.user_id == user_id,
        RecipientHistory.normalized_email.in_(norm_keys),
        RecipientHistory.source == "reserved",
    ).values(source="sent").execution_options(synchronize_session="fetch"))


def release_reserved_recipients_if_pre_handoff_failed(
    db: Session,
    *,
    user_id: str,
    application_id: str,
) -> None:
    """Release temporary reservations ONLY when failed cleanly before any external handoff."""
    db.flush()  # Keep a suppression made earlier in the same transaction.
    db.execute(delete(RecipientHistory).where(
        RecipientHistory.user_id == user_id,
        RecipientHistory.first_application_id == application_id,
        RecipientHistory.source == "reserved",
    ).execution_options(synchronize_session="fetch"))


def suppress_recipient(db: Session, *, user_id: str, email: str, notes: str = "", commit: bool = True) -> RecipientHistory:
    display = normalize_email_display(email)
    norm = normalize_email_dedupe_key(display)
    existing = db.scalar(
        select(RecipientHistory).where(
            RecipientHistory.user_id == user_id,
            RecipientHistory.normalized_email == norm,
        )
    )
    if existing:
        existing.source = "suppressed"
        if notes:
            existing.notes = notes
        if commit:
            db.commit()
        return existing
    rec = RecipientHistory(
        user_id=user_id,
        normalized_email=norm,
        display_email=display,
        source="suppressed",
        notes=notes,
    )
    db.add(rec)
    db.flush()
    if commit:
        db.commit()
    return rec


def import_historical_recipients(
    db: Session,
    *,
    user_id: str,
    entries: list[dict[str, str]],
) -> tuple[int, int]:
    """Import historical prior-application recipients for deduplication. Returns (inserted, skipped_existing)."""
    inserted = 0
    skipped = 0
    now = utcnow()
    for entry in entries:
        raw_email = (entry.get("email") or "").strip()
        if not raw_email:
            continue
        try:
            display = normalize_email_display(raw_email)
        except ValueError:
            skipped += 1
            continue
        norm = normalize_email_dedupe_key(display)
        try:
            with db.begin_nested():
                db.add(
                    RecipientHistory(
                        user_id=user_id,
                        normalized_email=norm,
                        display_email=display,
                        source="imported",
                        company_name=(entry.get("company") or "")[:200],
                        notes=(entry.get("notes") or "Imported prior application")[:500],
                        imported_at=now,
                    )
                )
                db.flush()
            inserted += 1
        except IntegrityError:
            skipped += 1
    db.commit()
    return inserted, skipped
