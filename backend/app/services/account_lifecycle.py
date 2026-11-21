"""Account-wide stop automation, export, deletion, and operator administration."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import (
    Application,
    AuditEvent,
    Batch,
    CandidateProfile,
    CvDocument,
    CvVersion,
    MailboxConnection,
    RecipientHistory,
    Schedule,
    SearchPreference,
    SheetsConnection,
    SupportingFile,
    User,
    UserSession,
)
from app.services.dedupe import release_reserved_recipients_if_pre_handoff_failed
from app.services.mailbox.gmail import revoke_gmail_connection
from app.services.pre_handoff import cancel_pending_for_applications
from app.services.storage import get_storage_adapter


def activate_stop_automation(db: Session, *, user: User, reason: str = "User triggered Stop Automation") -> dict[str, int]:
    """Pause/cancel all pending user batches, queued outbox work, and active schedules."""
    now = utcnow()
    user.automation_stopped_at = now

    cancelled_batches = 0
    for b in db.scalars(select(Batch).where(Batch.user_id == user.id, Batch.status.in_(["queued", "running", "quota_paused", "paused"]))).all():
        b.status = "cancelled"
        b.pause_reason = reason
        b.finished_at = now
        cancelled_batches += 1

    cancelled_outbox = 0
    for app, cancelled_ids, initial in cancel_pending_for_applications(db, reason, user_id=user.id):
        if initial and app.transport_status in ("draft", "ready", "leased", "queued"):
            app.transport_status = "cancelled"
            app.skip_or_error_reason = reason
            release_reserved_recipients_if_pre_handoff_failed(db, user_id=user.id, application_id=app.id)
        cancelled_outbox += len(cancelled_ids)

    disabled_schedules = 0
    for s in db.scalars(select(Schedule).where(Schedule.user_id == user.id, Schedule.is_active.is_(True))).all():
        s.is_active = False
        s.next_run_at = None
        disabled_schedules += 1

    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="stop_automation_activated",
            target_type="user",
            target_id=user.id,
            details_json={"cancelled_batches": cancelled_batches, "cancelled_outbox": cancelled_outbox},
        )
    )
    db.commit()
    return {
        "cancelled_batches": cancelled_batches,
        "cancelled_outbox": cancelled_outbox,
        "disabled_schedules": disabled_schedules,
    }


def resume_automation(db: Session, *, user: User) -> None:
    user.automation_stopped_at = None
    db.add(AuditEvent(user_id=user.id, actor_user_id=user.id, action="automation_resumed", target_type="user", target_id=user.id))
    db.commit()


def export_user_account_bundle(db: Session, *, user: User) -> dict[str, object]:
    """Export user's own workspace records (excluding encrypted secrets)."""
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    cvs = db.scalars(select(CvDocument).where(CvDocument.user_id == user.id)).all()
    apps = db.scalars(select(Application).where(Application.user_id == user.id)).all()
    history = db.scalars(select(RecipientHistory).where(RecipientHistory.user_id == user.id)).all()
    mailboxes = db.scalars(select(MailboxConnection).where(MailboxConnection.user_id == user.id)).all()

    return {
        "exported_at_utc": utcnow().isoformat(),
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "ui_locale": user.ui_locale,
            "timezone": user.timezone,
            "retention_days": user.retention_days,
        },
        "profile": {
            "full_name": profile.full_name if profile else "",
            "headline": profile.headline if profile else "",
            "residence_country_code": profile.residence_country_code if profile else "",
            "residence_city": profile.residence_city if profile else "",
            "work_authorizations": profile.work_authorizations_json if profile else [],
        },
        "preferences": {
            "target_titles": pref.target_titles_json if pref else [],
            "target_locations": pref.target_locations_json if pref else [],
            "daily_recipient_limit": pref.daily_recipient_limit if pref else 20,
        },
        "cv_documents": [{"id": c.id, "variant_label": c.variant_label, "is_archived": c.is_archived} for c in cvs],
        "mailboxes": [{"id": m.id, "provider": m.provider, "sender_email": m.sender_email, "status": m.status} for m in mailboxes],
        "applications": [
            {
                "id": a.id,
                "company": a.company_name,
                "role": a.role_title,
                "recipient": a.recipient_email,
                "transport_status": a.transport_status,
                "response_status": a.response_status,
                "outcome_status": a.outcome_status,
                "sent_at": a.sent_at.isoformat() if a.sent_at else None,
            }
            for a in apps
        ],
        "recipient_history": [{"email": h.display_email, "source": h.source} for h in history],
    }


def delete_user_account_and_data(db: Session, *, user: User) -> dict[str, object]:
    """
    Revoke mailbox connections, cancel future queued work, delete stored files,
    revoke sessions, and delete the user workspace from the primary database.
    """
    activate_stop_automation(db, user=user, reason="Account deleted")
    storage = get_storage_adapter()

    for conn in db.scalars(select(MailboxConnection).where(MailboxConnection.user_id == user.id)).all():
        if conn.provider == "gmail_oauth":
            revoke_gmail_connection(db, conn)
        else:
            conn.encrypted_credentials = ""
            conn.status = "revoked"

    for cv_ver in db.scalars(select(CvVersion).where(CvVersion.user_id == user.id)).all():
        try:
            storage.delete_bytes(cv_ver.storage_key)
        except Exception:
            pass

    for sf in db.scalars(select(SupportingFile).where(SupportingFile.user_id == user.id)).all():
        try:
            storage.delete_bytes(sf.storage_key)
        except Exception:
            pass

    sheets = db.scalar(select(SheetsConnection).where(SheetsConnection.user_id == user.id))
    if sheets:
        sheets.encrypted_credentials = ""
        sheets.status = "disconnected"

    for sess in db.scalars(select(UserSession).where(UserSession.user_id == user.id)).all():
        sess.revoked_at = utcnow()

    uid = user.id
    db.delete(user)
    db.commit()
    return {
        "deleted_user_id": uid,
        "status": "deleted",
        "note": "Primary database records, sessions, encrypted mailbox credentials, and private stored files were removed. Any message already accepted by an external mail provider cannot be recalled after handoff; encrypted database backups expire per backup retention policy.",
    }
