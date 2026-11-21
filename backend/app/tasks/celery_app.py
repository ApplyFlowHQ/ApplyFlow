"""Celery configuration and Beat periodic schedule definitions."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "applyflow",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.pipeline", "app.tasks.maintenance"],
)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_always_eager=settings.celery_task_always_eager,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=300,
    task_soft_time_limit=240,
    beat_schedule={
        "recover-discovery-batches": {
            "task": "app.tasks.maintenance.task_recover_discovery_batches",
            "schedule": 45.0,
        },
        "dispatch-due-schedules": {
            "task": "app.tasks.maintenance.task_dispatch_due_schedules",
            "schedule": 60.0,
        },
        "process-outbox-queue": {
            "task": "app.tasks.pipeline.task_drain_outbox",
            "schedule": 15.0,
        },
        "recover-stale-outbox-leases": {
            "task": "app.tasks.maintenance.task_recover_outbox_leases",
            "schedule": 120.0,
        },
        "poll-connected-inboxes": {
            "task": "app.tasks.maintenance.task_poll_all_inboxes",
            "schedule": 600.0,
        },
        "evaluate-followups": {
            "task": "app.tasks.maintenance.task_evaluate_all_followups",
            "schedule": 1800.0,
        },
        "cleanup-expired-retention": {
            "task": "app.tasks.maintenance.task_enforce_retention",
            "schedule": crontab(hour=3, minute=15),
        },
    },
)
