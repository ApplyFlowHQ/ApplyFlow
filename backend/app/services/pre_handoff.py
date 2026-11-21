"""Serialize draft mutations and cancellation with the worker's application-first handoff."""

from collections.abc import Iterator

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Application, OutboxEntry

HANDOFF_STATES = ("sending", "send_uncertain", "provider_accepted", "sent")
PROTECTED_OUTCOMES = ("provider_accepted", "ambiguous_handoff")
PENDING = ("queued", "leased")


def _lock_application(db: Session, user_id: str, application_id: str) -> Application | None:
    # SQLite ignores FOR UPDATE. A no-op write also serializes with dispatch there.
    return db.scalar(update(Application).where(
        Application.id == application_id, Application.user_id == user_id,
    ).values(updated_at=Application.updated_at).returning(Application)
        .execution_options(synchronize_session=False, populate_existing=True))


def initial_handoff_started(db: Session, app: Application) -> bool:
    if app.sent_at or app.transport_status in HANDOFF_STATES:
        return True
    return bool(db.scalar(select(OutboxEntry.id).where(
        OutboxEntry.application_id == app.id, OutboxEntry.user_id == app.user_id,
        or_(OutboxEntry.kind == "followup", OutboxEntry.status.in_(HANDOFF_STATES),
            OutboxEntry.handoff_outcome.in_(PROTECTED_OUTCOMES)),
    ).limit(1)))


def lock_mutable_draft(db: Session, user_id: str, application_id: str) -> Application:
    app = _lock_application(db, user_id, application_id)
    if app is None or initial_handoff_started(db, app):
        raise ValueError("Application unavailable for draft changes after handoff")
    return app


def _cancel_pending(db: Session, app: Application, reason: str, *criteria):
    return db.execute(update(OutboxEntry).where(
        OutboxEntry.application_id == app.id, OutboxEntry.user_id == app.user_id,
        OutboxEntry.status.in_(PENDING), OutboxEntry.handoff_outcome.not_in(PROTECTED_OUTCOMES),
        *criteria,
    ).values(status="cancelled", lease_owner=None, lease_expires_at=None, last_error=reason)
        .returning(OutboxEntry.id, OutboxEntry.kind).execution_options(synchronize_session="fetch")).all()


def cancel_initial_for_locked_draft(db: Session, app: Application, reason: str) -> bool:
    return bool(_cancel_pending(db, app, reason, OutboxEntry.kind == "initial"))


def cancel_pending_for_applications(
    db: Session, reason: str, *, user_id: str | None = None, mailbox_id: str | None = None,
) -> Iterator[tuple[Application, list[str], bool]]:
    scope = []
    if user_id is not None:
        scope.append(OutboxEntry.user_id == user_id)
    if mailbox_id is not None:
        scope.append(OutboxEntry.mailbox_connection_id == mailbox_id)
    candidates = db.execute(select(OutboxEntry.user_id, OutboxEntry.application_id)
        .where(OutboxEntry.status.in_(PENDING), *scope).distinct()
        .order_by(OutboxEntry.user_id, OutboxEntry.application_id)).all()
    for owner_id, application_id in candidates:
        app = _lock_application(db, owner_id, application_id)
        if app is None:
            continue
        rows = _cancel_pending(db, app, reason, *scope)
        if rows:
            # A redundant pending initial entry can coexist with a historical receipt.
            # Cancel the pending work while preserving the receipt and its reservation.
            reset_initial = any(row.kind == "initial" for row in rows) and not initial_handoff_started(db, app)
            if reset_initial and mailbox_id is not None:
                remaining = db.scalar(select(OutboxEntry.id).where(
                    OutboxEntry.user_id == owner_id, OutboxEntry.application_id == app.id,
                    OutboxEntry.kind == "initial", OutboxEntry.status.in_(PENDING),
                ).limit(1))
                reset_initial = remaining is None
            yield app, [row.id for row in rows], reset_initial
