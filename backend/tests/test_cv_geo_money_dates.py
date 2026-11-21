"""Tests for CV provenance/non-overlapping experience, geo disambiguation, money/FX, and DST scheduling."""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.db.models import Schedule, User
from app.services.cv_parser import parse_cv_bytes
from app.services.dates import resolve_local_schedule_instant
from app.services.geo import parse_job_location
from app.services.money import compare_salary_to_minimum
from app.services.scheduler import dispatch_due_schedules
from tests.conftest import build_blank_scanned_pdf_bytes, build_docx_bytes, build_pdf_bytes


def test_cv_parsing_no_double_counting_and_ocr_required():
    # Overlapping roles: 2020-01 to 2023-12 (48 months) + concurrent 2021-06 to 2022-06
    pdf_data = build_pdf_bytes(
        [
            "Tariq Mansour",
            "tariq@example.org",
            "Lead Analytics Engineer 2020-01 - 2023-12",
            "Part-time Data Consultant 2021-06 - 2022-06",
            "Skills: Python, SQL, PostgreSQL, Docker",
            "Project: Automated revenue reporting pipeline",
            "Passport Number: A12345678",
        ]
    )
    res = parse_cv_bytes(pdf_data, "application/pdf")
    assert res.parse_status == "parsed"
    assert res.total_experience_months == 48
    assert any(f.category == "tool" and f.title == "Python" and f.evidence_quote for f in res.facts)
    # Sensitive personal trait must be redacted from extracted facts
    assert not any("A12345678" in f.value for f in res.facts)

    # DOCX parsing check
    docx_data = build_docx_bytes(["Nadia Haddad", "Senior Finance Analyst 2021-01 - 2023-06", "Skills: Excel, IFRS, Budgeting"])
    dres = parse_cv_bytes(docx_data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert dres.parse_status == "parsed"
    assert dres.total_experience_months == 30

    # Blank/scanned PDF must report ocr_required, never valid parsed CV
    scanned = build_blank_scanned_pdf_bytes()
    sres = parse_cv_bytes(scanned, "application/pdf")
    assert sres.parse_status == "ocr_required"


def test_geo_disambiguation_and_remote_classification():
    amb = parse_job_location("Cairo")
    assert amb.is_ambiguous is True
    assert amb.country_code == ""
    assert len(amb.candidates) >= 2

    eg = parse_job_location("Cairo, Egypt")
    assert eg.is_ambiguous is False
    assert eg.country_code == "EG"
    assert eg.city == "Cairo"

    eu = parse_job_location("Berlin, Germany (Remote EU)", "Remote within EU only")
    assert eu.remote_scope == "remote_region"
    assert "EU" in eu.allowed_regions

    ww = parse_job_location("Remote", "This is a worldwide remote role — work from anywhere.")
    assert ww.remote_scope == "remote_worldwide"


def test_money_period_and_fx_without_fabricated_hours():
    # Hourly vs monthly without stated weekly hours MUST return unknown rather than assuming 40h
    unk = compare_salary_to_minimum(
        job_min=35.0,
        job_max=40.0,
        job_currency="USD",
        job_period="hourly",
        job_weekly_hours=None,
        user_min=4000.0,
        user_currency="USD",
        user_period="monthly",
    )
    assert unk.status == "unknown"

    # Cross-currency with explicit dated FX rate
    cmp_fx = compare_salary_to_minimum(
        job_min=5000.0,
        job_max=5000.0,
        job_currency="EUR",
        job_period="monthly",
        job_weekly_hours=40.0,
        user_min=20000.0,
        user_currency="AED",
        user_period="monthly",
        static_fx_rates={"EUR_AED": 4.0, "_date": "2026-11-18"},
    )
    assert cmp_fx.status == "meets_minimum"
    assert cmp_fx.normalized_amount == 20000.0
    assert cmp_fx.fx_rate_date == "2026-11-18"

    # Missing FX rate returns unknown
    no_fx = compare_salary_to_minimum(
        job_min=5000.0,
        job_max=5000.0,
        job_currency="JPY",
        job_period="monthly",
        job_weekly_hours=None,
        user_min=3000.0,
        user_currency="AED",
        user_period="monthly",
    )
    assert no_fx.status == "unknown"


def test_dst_schedule_spring_forward_and_fall_back_once(db_session):
    # Spring-forward in America/New_York: 2026-03-08 02:30 does not exist -> shifts to 03:00 EDT (07:00 UTC)
    spring_utc, spring_note = resolve_local_schedule_instant(date(2026, 3, 8), "02:30", "America/New_York")
    assert spring_utc == datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
    assert "shifted forward" in spring_note

    # Fall-back in America/New_York: 2026-11-01 01:30 occurs twice -> fold=0 (05:30 UTC), second run suppressed
    fall_utc, fall_note = resolve_local_schedule_instant(date(2026, 11, 1), "01:30", "America/New_York")
    assert fall_utc == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    assert "fold=0" in fall_note

    user = User(email="sched@example.org", email_normalized="sched@example.org", timezone="America/New_York")
    db_session.add(user)
    db_session.flush()
    sched = Schedule(
        user_id=user.id,
        name="Morning Run",
        mode="dry_run",
        days_of_week_json=[6],  # Sunday
        local_time_hhmm="01:30",
        timezone="America/New_York",
        catch_up_window_minutes=120,
        is_active=True,
    )
    db_session.add(sched)
    db_session.commit()

    # First occurrence at 01:30 EDT (05:30 UTC)
    b1 = dispatch_due_schedules(db_session, now_utc=datetime(2026, 11, 1, 5, 35, tzinfo=timezone.utc))
    assert len(b1) == 1
    # Repeated 01:30 EST (06:30 UTC) on same local date MUST NOT execute a second batch!
    b2 = dispatch_due_schedules(db_session, now_utc=datetime(2026, 11, 1, 6, 35, tzinfo=timezone.utc))
    assert len(b2) == 0
