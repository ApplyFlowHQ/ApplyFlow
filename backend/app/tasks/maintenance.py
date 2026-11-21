"""Periodic Celery Beat tasks for schedules, lease recovery, inbox polling, follow-ups, and retention."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from threading import Event, Thread
from time import monotonic

from celery import shared_task
from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import Batch, MailboxConnection, User
from app.db.session import session_scope
from app.services.followup import evaluate_and_queue_followups_for_user
from app.services.inbox_processor import poll_mailbox_connection
from app.services.mailbox.gmail import GMAIL_READONLY_SCOPE, sync_gmail_sent_history
from app.services.outbox import recover_stale_outbox_leases
from app.services.scheduler import dispatch_due_schedules
from app.tasks.pipeline import free_user_batch_slot, runnable_batch, task_run_batch

logger = logging.getLogger(__name__)


@shared_task
def task_dispatch_due_schedules() -> int:
    with session_scope() as db:
        batches = dispatch_due_schedules(db)
        for batch in batches:
            task_run_batch.delay(batch.id)
        return len(batches)


def _pending_discovery_ids(limit: int) -> list[str]:
    now = utcnow()
    with session_scope() as db:
        return list(db.scalars(select(Batch.id).where(
            runnable_batch(now), free_user_batch_slot(Batch.user_id, Batch.id, now),
            Batch.discovery_stage.is_not(None), Batch.discovery_stage != "deferred", Batch.import_origin == "",
        ).order_by(Batch.updated_at, Batch.id).limit(limit)))


@shared_task
def task_recover_discovery_batches() -> int:
    """Redispatch committed requests missed by the broker and expired worker leases."""
    batch_ids = _pending_discovery_ids(50)
    for batch_id in batch_ids:
        if get_settings().celery_task_always_eager:
            task_run_batch.run(batch_id)
        else:
            task_run_batch.delay(batch_id)
    return len(batch_ids)


@asynccontextmanager
async def local_batch_worker_lifespan(_app):
    """Single-host eager installations have no Beat. Poll the same durable batch queue."""
    settings = get_settings()
    if not settings.celery_task_always_eager or settings.environment == "test":
        yield
        return
    stopped = Event()

    def poll():
        next_schedule_check = 0.0
        while not stopped.is_set():
            try:
                if monotonic() >= next_schedule_check:
                    with session_scope() as db:
                        dispatch_due_schedules(db)
                    next_schedule_check = monotonic() + 60.0
                batch_ids = _pending_discovery_ids(1)
                if batch_ids and not stopped.is_set():
                    task_run_batch.run(batch_ids[0])
            except Exception as exc:
                logger.warning("Discovery queue poll failed (%s)", type(exc).__name__)
            stopped.wait(5)

    worker = Thread(target=poll, daemon=True, name="local-batch-discovery")
    worker.start()
    try:
        yield
    finally:
        stopped.set()


@shared_task
def task_recover_outbox_leases() -> dict[str, int]:
    with session_scope() as db:
        return recover_stale_outbox_leases(db)


@shared_task
def task_poll_all_inboxes() -> int:
    total_events = 0
    with session_scope() as db:
        conns = db.scalars(
            select(MailboxConnection).where(
                MailboxConnection.inbox_poll_enabled.is_(True),
                MailboxConnection.status == "connected",
                MailboxConnection.revoked_at.is_(None),
            )
        ).all()
        for conn in conns:
            user = db.scalar(select(User).where(User.id == conn.user_id))
            if not user or user.deleted_at or user.is_suspended:
                continue
            total_events += poll_mailbox_connection(db, conn)
            if conn.provider == "gmail_oauth" and GMAIL_READONLY_SCOPE in (conn.granted_scopes_json or []):
                try:
                    sync_gmail_sent_history(db, conn=conn, max_pages=3)
                except Exception:
                    pass
    return total_events


@shared_task
def task_evaluate_all_followups() -> int:
    total_queued = 0
    with session_scope() as db:
        users = db.scalars(select(User).where(User.deleted_at.is_(None), User.is_suspended.is_(False))).all()
        for user in users:
            queued, _ = evaluate_and_queue_followups_for_user(db, user_id=user.id)
            total_queued += queued
    return total_queued


@shared_task
def task_enforce_retention() -> int:
    """Delete expired unconsumed AuthTokens and expired sessions past retention."""
    from app.db.models import AuthToken, UserSession

    now = utcnow()
    removed = 0
    with session_scope() as db:
        for tok in db.scalars(select(AuthToken).where(AuthToken.expires_at < now - timedelta(days=7))).all():
            db.delete(tok)
            removed += 1
        for sess in db.scalars(select(UserSession).where(UserSession.expires_at < now - timedelta(days=30))).all():
            db.delete(sess)
            removed += 1
        db.commit()
    return removed
