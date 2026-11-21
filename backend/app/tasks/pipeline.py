"""Bounded background tasks for CV parsing, batch execution, outbox sending, and Google Sheets sync."""

from __future__ import annotations

import logging
import socket
from uuid import uuid4

from celery import shared_task
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import Batch, CvVersion, User
from app.db.session import session_scope
from app.services.batch_runner import BATCH_LEASE, BatchClaimLost, execute_batch_pipeline
from app.services.cv_parser import parse_cv_bytes
from app.services.outbox import claim_next_outbox_entry, process_outbox_entry
from app.services.sheets import sync_applications_to_google_sheet
from app.services.storage import get_storage_adapter

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def task_parse_cv_version(self, cv_version_id: str) -> str:
    from app.services.cv_parser import apply_parsed_cv
    from app.services.pdf_renderer import ensure_cv_version_pdf_ready
    from app.services.storage import compute_sha256

    with session_scope() as db:
        cv_ver = db.scalar(select(CvVersion).where(CvVersion.id == cv_version_id).with_for_update())
        if not cv_ver:
            return "missing"
        storage = get_storage_adapter()
        data = storage.get_bytes(cv_ver.storage_key)
        if compute_sha256(data) != cv_ver.sha256:
            cv_ver.parse_status = "failed"
            cv_ver.parse_error = "Original CV checksum mismatch"
        else:
            apply_parsed_cv(db, cv_ver, parse_cv_bytes(data, cv_ver.mime_type))
        ensure_cv_version_pdf_ready(cv_ver, storage=storage, source_bytes=data)
        db.commit()
        return cv_ver.parse_status


def runnable_batch(now):
    return or_(
        Batch.status == "queued",
        (Batch.status == "running") & (Batch.lease_expires_at <= now),
    )


def free_user_batch_slot(user_id, batch_id, now):
    other = aliased(Batch)
    active = select(func.count(other.id)).where(
        other.user_id == user_id, other.id != batch_id,
        other.status == "running", other.lease_expires_at > now,
    ).correlate_except(other).scalar_subquery()
    return active < get_settings().max_user_concurrent_batches


@shared_task
def task_run_batch(batch_id: str, max_search_queries: int | None = None) -> str:
    """Acquire a renewable DB lease. A broker redelivery is only a wakeup."""
    settings = get_settings()
    with session_scope() as db:
        batch = db.get(Batch, batch_id)
        if batch is None:
            return "missing"
        if batch.discovery_stage == "deferred" or batch.import_origin:
            return batch.status
        token = str(uuid4())
        now = utcnow()
        # Serialize competing claims for a user on PostgreSQL. SQLite serializes the UPDATE.
        db.scalar(select(User.id).where(User.id == batch.user_id).with_for_update())
        claimed = db.execute(update(Batch).where(
            Batch.id == batch.id, runnable_batch(now), free_user_batch_slot(batch.user_id, batch.id, now),
        ).values(status="running", lease_token=token, lease_expires_at=now + BATCH_LEASE,
                 started_at=batch.started_at or now, consecutive_failures=Batch.consecutive_failures
                 + case((Batch.status == "running", 1), else_=0)).execution_options(synchronize_session=False))
        db.commit()
        db.refresh(batch)
        if not claimed.rowcount:
            return batch.status
        if batch.consecutive_failures >= 3:
            db.execute(update(Batch).where(Batch.id == batch_id, Batch.lease_token == token, Batch.status == "running")
                       .values(status="failed", discovery_stage="failed", lease_token=None, lease_expires_at=None,
                               finished_at=now, error_message="Repeated worker loss before the next discovery checkpoint"))
            db.commit()
            db.refresh(batch)
            return batch.status
        try:
            execute_batch_pipeline(db, batch=batch, lease_token=token,
                                   skip_network_dns=settings.environment == "test",
                                   max_search_queries_override=max_search_queries)
        except BatchClaimLost:
            db.rollback()
            # A global stop can invalidate this fence before the stop handler updates the batch.
            db.execute(update(Batch).where(Batch.id == batch_id, Batch.status == "running", Batch.lease_token == token)
                       .values(status="cancelled", discovery_stage="cancelled", finished_at=utcnow(),
                               lease_token=None, lease_expires_at=None))
        except Exception as exc:
            db.rollback()
            db.refresh(batch)
            attempts = batch.consecutive_failures + 1
            failed = attempts >= 3
            db.execute(update(Batch).where(Batch.id == batch_id, Batch.lease_token == token,
                                           Batch.status == "running")
                       .values(status="failed" if failed else "queued", consecutive_failures=attempts,
                               discovery_stage="failed" if failed else batch.discovery_stage or "queued",
                               finished_at=utcnow() if failed else None, lease_token=None, lease_expires_at=None,
                               error_message=f"Discovery interrupted ({type(exc).__name__})"))
            logger.warning("Batch %s interrupted (%s), attempt %d", batch_id, type(exc).__name__, attempts)
        db.commit()
        db.refresh(batch)
        return batch.status


@shared_task(bind=True, max_retries=1)
def task_drain_outbox(self, max_items: int = 10) -> int:
    """Fairly process queued outbox entries up to `max_items` per worker tick."""
    from app.services.effective_config import resolve_effective_live_sending

    worker_id = f"celery-{socket.gethostname()}"
    processed = 0
    with session_scope() as db:
        live_enabled, _ = resolve_effective_live_sending(db)
        if not live_enabled:
            return 0
        for _ in range(max_items):
            entry = claim_next_outbox_entry(db, worker_id=worker_id)
            if not entry:
                break
            process_outbox_entry(db, entry=entry)
            processed += 1
    return processed


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def task_sync_sheets_for_user(self, user_id: str) -> dict:
    with session_scope() as db:
        return sync_applications_to_google_sheet(db, user_id=user_id)
