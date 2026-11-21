"""UTC storage, IANA timezone conversions, and DST-safe schedule occurrence resolution."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def ensure_valid_timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {tz_name}") from exc


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_local_schedule_instant(
    target_date: date,
    local_time_hhmm: str,
    tz_name: str,
) -> tuple[datetime, str]:
    """
    Resolve a scheduled local wall-clock time on `target_date` in `tz_name` to a UTC instant.

    DST Policy (documented):
    - Nonexistent local time (spring-forward gap): advances forward by 1 minute steps until
      the local wall time exists in the zone, returning the first valid instant after the gap
      and recording a `dst_note`.
    - Repeated local time (fall-back fold): always selects `fold=0` (the first occurrence),
      and callers persist `local_date_str` uniquely so the schedule runs only once per day.
    """
    tz = ensure_valid_timezone(tz_name)
    hour_str, minute_str = local_time_hhmm.split(":", 1)
    naive = datetime.combine(target_date, time(hour=int(hour_str), minute=int(minute_str)))

    # Detect nonexistent local time by round-tripping through UTC
    candidate = naive.replace(tzinfo=tz, fold=0)
    roundtrip = candidate.astimezone(timezone.utc).astimezone(tz)
    if roundtrip.replace(tzinfo=None) != naive:
        # Spring-forward gap: transition boundary lies between fold=1 UTC and fold=0 UTC.
        fold1_utc = naive.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
        for step in range(0, 240):
            probe_utc = fold1_utc + timedelta(minutes=step)
            rt = probe_utc.astimezone(tz)
            if rt.date() == target_date and rt.replace(tzinfo=None) >= naive:
                return probe_utc, (
                    f"DST nonexistent local time {local_time_hhmm} shifted forward to "
                    f"{rt.strftime('%H:%M')} {tz_name}"
                )
        probe_utc = candidate.astimezone(timezone.utc)
        return probe_utc, f"DST gap fallback applied for {local_time_hhmm} {tz_name}"

    # Detect repeated local time (fall-back fold)
    fold0_utc = naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    fold1_utc = naive.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    note = ""
    if fold0_utc != fold1_utc:
        note = f"DST repeated local time {local_time_hhmm} in {tz_name}; scheduled once on first occurrence (fold=0)"

    return fold0_utc, note


def compute_next_schedule_run(
    *,
    days_of_week: list[int],
    local_time_hhmm: str,
    tz_name: str,
    after_utc: datetime,
) -> tuple[datetime | None, str]:
    """Find next scheduled UTC instant strictly after `after_utc`."""
    if not days_of_week:
        return None, ""
    tz = ensure_valid_timezone(tz_name)
    ref_utc = to_utc(after_utc)
    local_ref = ref_utc.astimezone(tz)
    start_day = local_ref.date()

    for offset in range(0, 15):
        candidate_date = start_day + timedelta(days=offset)
        if candidate_date.weekday() not in days_of_week:
            continue
        utc_dt, note = resolve_local_schedule_instant(candidate_date, local_time_hhmm, tz_name)
        if utc_dt > ref_utc:
            return utc_dt, note
    return None, ""


def is_in_quiet_hours_or_outside_window(
    *,
    now_utc: datetime,
    tz_name: str,
    window_start_hhmm: str,
    window_end_hhmm: str,
    quiet_start_hhmm: str,
    quiet_end_hhmm: str,
) -> tuple[bool, str]:
    """Return (blocked, reason) according to user's local sending window and quiet hours."""
    tz = ensure_valid_timezone(tz_name)
    local_dt = to_utc(now_utc).astimezone(tz)
    cur = local_dt.time().replace(second=0, microsecond=0)

    def _parse(hhmm: str) -> time:
        h, m = hhmm.split(":", 1)
        return time(int(h), int(m))

    def _in_range(t: time, start: time, end: time) -> bool:
        if start <= end:
            return start <= t <= end
        return t >= start or t <= end

    qs, qe = _parse(quiet_start_hhmm), _parse(quiet_end_hhmm)
    if qs != qe and _in_range(cur, qs, qe):
        return True, f"Paused for quiet hours ({quiet_start_hhmm}-{quiet_end_hhmm} {tz_name})"

    ws, we = _parse(window_start_hhmm), _parse(window_end_hhmm)
    if ws != we and not _in_range(cur, ws, we):
        return True, f"Outside configured sending window ({window_start_hhmm}-{window_end_hhmm} {tz_name})"

    return False, ""
