"""Recurring schedule management and at-most-once DST-safe occurrence dispatch."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import as_utc, utcnow
from app.db.models import Batch, Schedule, ScheduleOccurrence, SearchPreference, User
from app.services.dates import compute_next_schedule_run, ensure_valid_timezone, resolve_local_schedule_instant


def refresh_schedule_next_run(schedule: Schedule, *, after_utc: datetime | None = None) -> None:
    if not schedule.is_active:
        schedule.next_run_at = None
        return
    ref = after_utc or utcnow()
    next_dt, _ = compute_next_schedule_run(
        days_of_week=schedule.days_of_week_json or [],
        local_time_hhmm=schedule.local_time_hhmm,
        tz_name=schedule.timezone,
        after_utc=ref,
    )
    schedule.next_run_at = next_dt


def dispatch_due_schedules(
    db: Session,
    *,
    now_utc: datetime | None = None,
) -> list[Batch]:
    """
    Evaluate active recurring schedules and create at most one Batch per due occurrence.
    Enforces unique (schedule_id, scheduled_for_utc) and (schedule_id, local_date_str) so
    repeated DST fall-back hours or scheduler restarts/retries never execute twice.
    """
    now = as_utc(now_utc) or utcnow()
    active_schedules = db.scalars(select(Schedule).where(Schedule.is_active.is_(True))).all()
    dispatched_batches: list[Batch] = []

    for sched in active_schedules:
        if sched.mode == "demo_fixture":
            continue
        user = db.scalar(select(User).where(User.id == sched.user_id))
        if not user or user.deleted_at or user.is_suspended or user.automation_stopped_at:
            continue

        tz = ensure_valid_timezone(sched.timezone)
        local_now = now.astimezone(tz)
        local_date = local_now.date()
        if local_date.weekday() not in (sched.days_of_week_json or []):
            if not sched.next_run_at:
                refresh_schedule_next_run(sched, after_utc=now)
                db.commit()
            continue

        scheduled_utc, dst_note = resolve_local_schedule_instant(local_date, sched.local_time_hhmm, sched.timezone)
        if now < scheduled_utc:
            if sched.next_run_at != scheduled_utc:
                sched.next_run_at = scheduled_utc
                db.commit()
            continue

        local_date_str = local_date.isoformat()
        age_minutes = (now - scheduled_utc).total_seconds() / 60.0
        too_old = age_minutes > max(1, sched.catch_up_window_minutes)
        occ_status = "skipped_catchup" if too_old else "dispatched"

        occ = ScheduleOccurrence(
            schedule_id=sched.id,
            user_id=sched.user_id,
            scheduled_for_utc=scheduled_utc,
            local_date_str=local_date_str,
            status=occ_status,
            dst_note=dst_note,
        )
        try:
            with db.begin_nested():
                db.add(occ)
                db.flush()
        except IntegrityError:
            # Occurrence already recorded for this UTC instant or local wall-clock date (DST fall-back guard)
            refresh_schedule_next_run(sched, after_utc=now)
            db.commit()
            continue

        if occ_status == "dispatched":
            pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == sched.user_id))
            prev_count = db.scalar(select(func.count(Batch.id)).where(Batch.user_id == sched.user_id)) or 0
            run_mode = "approval_required" if sched.mode == "live" else sched.mode
            batch = Batch(
                user_id=sched.user_id,
                idempotency_key=f"sched-{sched.id}-{local_date_str}",
                schedule_occurrence_id=occ.id,
                mode=sched.mode,
                run_mode=run_mode,
                pipeline_version="vNext",
                target_goal_count=pref.target_goal_count if pref else 10,
                query_rotation_offset=int(prev_count),
                status="queued",
                discovery_stage="queued",
            )
            db.add(batch)
            db.flush()
            occ.batch_id = batch.id
            sched.last_run_at = now
            dispatched_batches.append(batch)

        refresh_schedule_next_run(sched, after_utc=max(now, scheduled_utc + timedelta(minutes=1)))
        db.commit()

    return dispatched_batches
