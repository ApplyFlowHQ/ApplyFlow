"""Comprehensive regression, replay audit, isolation, and controlled owner-mailbox E2E tests."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import (
    Application,
    Batch,
    CandidateProfile,
    CvDocument,
    CvExtractedFact,
    CvVersion,
    DiscoverySourceSighting,
    JobPosting,
    MailboxConnection,
    RecipientHistory,
    Schedule,
    SearchPreference,
    User,
)
from app.db.session import get_sessionmaker
from app.services.account_lifecycle import activate_stop_automation
from app.services.batch_runner import execute_batch_pipeline
from app.services.dedupe import DuplicateRecipientError, reserve_recipients_atomically
from app.services.discovery.base import DiscoveredCandidate, SearchProviderOutcome
from app.services.discovery.career_page import (
    clean_and_extract_role_and_employer,
    extract_structured_application_evidence,
    split_clear_multi_role_post,
)
from app.services.followup import evaluate_and_queue_followups_for_user
from app.services.geo import infer_location_hint_with_confidence, parse_job_location
from app.services.inbox_processor import ingest_inbound_messages
from app.services.mailbox.base import HandoffOutcome, InboundMessageRecord, OutboundEmailMessage, SendResult
from app.services.matching import evaluate_job_against_cv
from app.services.outbox import enqueue_application_for_send
from app.services.scheduler import dispatch_due_schedules
from app.services.sheets import SheetsConnection, encrypt_json, sync_applications_to_google_sheet
from app.services.storage import compute_sha256, get_storage_adapter
from app.services.verification import verify_discovered_candidate
from tests.conftest import build_pdf_bytes

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "synthetic_replay.json"
REQUIRED_METRICS_KEYS = {
    "search_hits",
    "unique_source_urls",
    "verified_role_email_invitations",
    "verified_general_cv_invitations",
    "eligible_for_draft",
    "eligible_for_live_send",
    "drafted",
    "sent_accepted",
    "failed_or_uncertain",
    "skipped",
    "duplicates",
    "demo_excluded",
}


class _OwnerMailboxTransport:
    def __init__(self, outcome: HandoffOutcome = HandoffOutcome.PROVIDER_ACCEPTED) -> None:
        self.sent: list[OutboundEmailMessage] = []
        self.outcome = outcome

    def send_message(self, msg: OutboundEmailMessage) -> SendResult:
        self.sent.append(msg)
        if self.outcome == HandoffOutcome.AMBIGUOUS_HANDOFF:
            return SendResult(
                outcome=HandoffOutcome.AMBIGUOUS_HANDOFF,
                rfc_message_id=msg.rfc_message_id,
                error_code="SMTP_TIMEOUT",
                error_message="Connection closed after DATA prior to 250 OK",
            )
        return SendResult(
            outcome=HandoffOutcome.PROVIDER_ACCEPTED,
            provider_message_id=f"owner-msg-{len(self.sent)}",
            provider_thread_id=f"owner-thr-{len(self.sent)}",
            rfc_message_id=msg.rfc_message_id,
        )

    def reconcile_sent_message(self, *_args):
        return None

    def poll_inbox(self, cursor: str):
        return [], cursor


def _seed_cafm_user(db, *, email: str = "audit@example.org", with_mailbox: bool = False):
    now = utcnow()
    user = User(
        email=email,
        email_normalized=email.lower(),
        full_name="Fictional Reviewer",
        timezone="Asia/Dubai",
        email_verified_at=now,
    )
    db.add(user)
    db.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name="Fictional Reviewer",
        contact_email=email,
        residence_country_code="AE",
        residence_city="Dubai",
        work_authorizations_json=["AE"],
        nationalities_json=["IN"],
        confirmed_languages_json=[{"language": "en", "level": "fluent"}, {"language": "ar", "level": "intermediate"}],
        willing_to_relocate=False,
        confirmed_at=now,
        signature_text="Best regards,\nFictional Reviewer",
    )
    pref = SearchPreference(
        user_id=user.id,
        target_titles_json=["CAFM Administrator", "CAFM Coordinator", "Facilities Coordinator", "Data Analyst", "Power BI Analyst"],
        role_families_json=["CAFM", "Data Analytics"],
        target_locations_json=[{"country_code": "AE", "city": "Dubai", "label": "Dubai, UAE"}],
        allow_relocation=False,
        allowed_remote_modes_json=["onsite", "hybrid", "remote_country", "remote_worldwide"],
        freshness_days=30,
        unknown_dates_policy="allow_neutral",
        unknown_location_policy="skip",
        unknown_eligibility_policy="skip",
        min_match_score=65,
        max_experience_gap_years=2,
        include_standing_career_invitations=True,
        batch_size=50,
    )
    doc = CvDocument(user_id=user.id, variant_label="CAFM & Data CV")
    db.add_all([profile, pref, doc])
    db.flush()

    pdf_bytes = build_pdf_bytes(
        [
            "Fictional Reviewer",
            "CAFM Administrator and Data Analyst in Dubai UAE",
            "Skills: CAFM, IBM Maximo, SQL, Power BI, Excel, PPM, SLA, KPI",
            "6 years Facilities Management and Data Analytics experience",
        ]
    )
    sha = compute_sha256(pdf_bytes)
    storage_key = f"users/{user.id}/cvs/{doc.id}/v1.pdf"
    get_storage_adapter().put_bytes(storage_key, pdf_bytes, "application/pdf")

    ver = CvVersion(
        cv_document_id=doc.id,
        user_id=user.id,
        version_number=1,
        original_filename="Tariq_CAFM_CV.pdf",
        mime_type="application/pdf",
        size_bytes=len(pdf_bytes),
        sha256=sha,
        storage_key=storage_key,
        parse_status="parsed",
        detected_language="en",
        extracted_text=(
            "Fictional Reviewer CAFM Administrator Data Analyst Dubai UAE "
            "CAFM IBM Maximo SQL Power BI Excel PPM SLA KPI 6 years experience"
        ),
        total_experience_months=72,
    )
    db.add(ver)
    db.flush()
    doc.active_version_id = ver.id

    for cat, title, val in [
        ("role", "CAFM Administrator", "CAFM Administrator"),
        ("skill", "CAFM", "CAFM"),
        ("skill", "IBM Maximo", "IBM Maximo"),
        ("skill", "Power BI", "Power BI"),
        ("skill", "SQL", "SQL"),
        ("project", "CAFM Migration", "Implemented IBM Maximo PPM and SLA reporting"),
        ("certification", "Facilities Management", "ISO 41001"),
    ]:
        db.add(
            CvExtractedFact(
                cv_version_id=ver.id,
                user_id=user.id,
                category=cat,
                title=title,
                value=val,
                evidence_quote=val,
                confidence="evidenced",
            )
        )

    conn = None
    if with_mailbox:
        conn = MailboxConnection(
            user_id=user.id,
            provider="smtp_imap",
            label="Owner Test Mailbox",
            sender_email=email,
            encrypted_credentials=encrypt_json({"username": email, "password": "secret"}),
            smtp_host="mail.acme-eval.test",
            smtp_port=587,
            is_primary=True,
            status="connected",
            inbox_poll_enabled=True,
            inbox_health_status="healthy",
            inbox_last_polled_at=now,
            daily_limit_override=20,
        )
        db.add(conn)

    db.commit()
    return user, profile, pref, ver, pdf_bytes, sha, conn


def test_synthetic_audit_replay(db_session):
    """Exercise demo isolation, eligibility and duplicate detection with fictional postings."""
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert len(data["rows"]) == 6
    rows = data["rows"]

    user, _, _, _, _, _, _ = _seed_cafm_user(db_session)
    batches_seen: list[str] = []
    by_batch: dict[str, list[dict]] = {}
    for r in rows:
        bid = r["batch_id"]
        if bid not in by_batch:
            batches_seen.append(bid)
            by_batch[bid] = []
        by_batch[bid].append(r)

    replayed_batches: list[Batch] = []
    for bid in batches_seen:
        b_rows = by_batch[bid]
        b_mode = b_rows[0]["batch_mode"]
        cands: list[DiscoveredCandidate] = []
        for r in b_rows:
            ev_json = r.get("extraction_evidence_json") or {}
            inacc = str(ev_json.get("page_inaccessible_reason") or "")
            raw_jd = r["jd_text"] or ""
            raw_title = r["title"] or ""
            src_url = r["source_url"] or ""

            if b_mode == "demo_fixture":
                cands.append(
                    DiscoveredCandidate(
                        source_provider=r["source_provider"] or "demo_fixture",
                        source_url=src_url,
                        canonical_url=src_url,
                        source_type=r["source_type"] or "job_post",
                        title=raw_title,
                        company_name=r["company_name"] or "",
                        company_domain=r["company_domain"] or "",
                        jd_text=raw_jd,
                        location_raw=r["location_raw"] or "",
                        retrieved_at=datetime.fromisoformat(r["retrieved_at"]) if r.get("retrieved_at") else utcnow(),
                        posted_at=datetime.fromisoformat(r["posted_at"]) if r.get("posted_at") else None,
                        application_email=r["application_email"] or "",
                        cc_emails=list(r.get("cc_emails_json") or []),
                        subject_instruction=r["subject_instruction"] or "",
                        evidence_quote=r["evidence_quote"] or "",
                        is_standing_invitation=bool(r.get("is_standing_invitation")),
                    )
                )
            else:
                ev = extract_structured_application_evidence(raw_jd, source_url=src_url)
                role_ext, val_emp, purp_emp, emp_conf, publisher = clean_and_extract_role_and_employer(
                    raw_title, raw_jd, src_url
                )
                hint_loc, hint_conf = infer_location_hint_with_confidence(url=src_url, title=raw_title, jd_text=raw_jd)
                inferred_loc = r["location_raw"] or hint_loc
                app_email = ev.primary_email or (r["application_email"] if (r["application_email"] and r["evidence_quote"]) else "")
                ev_quote = ev.evidence_quote or r["evidence_quote"] or ""
                cands.append(
                    DiscoveredCandidate(
                        source_provider=r["source_provider"] or "tavily",
                        source_url=src_url,
                        canonical_url=src_url.split("#", 1)[0],
                        source_type=r["source_type"] or ("standing_invitation" if ev.is_standing_invitation else "job_post"),
                        title=raw_title,
                        company_name=val_emp,
                        company_domain=r["company_domain"] or "",
                        jd_text=raw_jd,
                        location_raw=inferred_loc,
                        location_confidence=hint_conf,
                        retrieved_at=datetime.fromisoformat(r["retrieved_at"]) if r.get("retrieved_at") else utcnow(),
                        posted_at=datetime.fromisoformat(r["posted_at"]) if r.get("posted_at") else None,
                        date_source="provider_metadata" if r.get("posted_at") else "unknown",
                        application_email=app_email,
                        cc_emails=ev.cc_emails or list(r.get("cc_emails_json") or []),
                        subject_instruction=ev.subject_instruction or r["subject_instruction"] or "",
                        evidence_quote=ev_quote,
                        is_standing_invitation=bool(r.get("is_standing_invitation") or ev.is_standing_invitation),
                        qualifications=ev.qualifications,
                        portal_apply_detected=ev.portal_apply_detected or bool(ev_json.get("portal_apply_detected")),
                        rejected_emails=ev.rejected_emails,
                        page_inaccessible_reason=inacc,
                        publisher=publisher,
                        purported_employer=purp_emp,
                        validated_employer=val_emp,
                        employer_confidence=emp_conf,
                        role_extracted=role_ext,
                    )
                )

        batch = Batch(user_id=user.id, mode=b_mode, status="queued")
        db_session.add(batch)
        db_session.commit()
        execute_batch_pipeline(
            db_session,
            batch=batch,
            candidate_overrides=cands,
            skip_network_dns=True,
            dedupe_against_history=(b_mode != "demo_fixture"),
        )
        replayed_batches.append(batch)

    for rb in replayed_batches:
        assert REQUIRED_METRICS_KEYS.issubset(set((rb.metrics_json or {}).keys()))

    real_apps = db_session.query(Application).filter(Application.user_id == user.id, Application.is_demo.is_(False)).all()
    demo_apps = db_session.query(Application).filter(Application.user_id == user.id, Application.is_demo.is_(True)).all()
    real_postings = db_session.query(JobPosting).filter(JobPosting.user_id == user.id, JobPosting.is_demo.is_(False)).all()
    dup_sightings = (
        db_session.query(DiscoverySourceSighting)
        .filter(DiscoverySourceSighting.user_id == user.id, DiscoverySourceSighting.is_duplicate.is_(True))
        .all()
    )

    # 1. Eliminated empty-recipient pollution from real Applications
    assert all(bool(a.recipient_email) for a in real_apps)
    assert len(demo_apps) >= 1, [b.skip_reason_counts_json for b in replayed_batches]
    assert all(not a.recipient_email.endswith(".example") for a in real_apps)

    # 2. Zero aggregator/platform hostnames inside validated_employer or real Application.company_name
    bad_hosts = {"www.linkedin.com", "www.facebook.com", "uk.indeed.com", "www.instagram.com", "jll.wd1.myworkdayjobs.com"}
    for p in real_postings:
        assert p.validated_employer.lower() not in bad_hosts
        assert p.company_name.lower() not in bad_hosts
        assert not p.role_extracted.startswith("#")

    # 3. Fictional citizenship-restricted posting rejected on mandatory nationality restriction and NOT drafted
    agfm_url = "https://paper-lantern.test/jobs/coordinator"
    agfm_posts = [p for p in real_postings if p.source_url == agfm_url]
    assert agfm_posts
    for ap in agfm_posts:
        assert ap.primary_skip_code == "NATIONALITY_RESTRICTION"
    agfm_ids = {p.id for p in agfm_posts}
    agfm_apps = [a for a in real_apps if a.job_posting_id in agfm_ids]
    assert all(a.transport_status == "skipped" and a.selected_cv_version_id is None for a in agfm_apps)

    # 4. Every fixture row is accounted for at its original source within its batch.
    for fixture_id, replay in zip(batches_seen, replayed_batches, strict=True):
        actual = db_session.query(JobPosting.source_url).filter_by(batch_id=replay.id).all()
        assert Counter(url for (url,) in actual) == Counter(r["source_url"] for r in by_batch[fixture_id])
    assert dup_sightings == []

    # The former collision shared parsed employer and title, but no requisition.
    # These are distinct source records; neither supplies a verified vacancy.
    pair_urls = ("https://paper-lantern.test/notice/1", "https://paper-lantern.test/notice/2")
    unknown, dubai = (next(p for p in real_postings if p.source_url == url) for url in pair_urls)
    assert unknown.batch_id == dubai.batch_id
    assert (unknown.validated_employer, unknown.role_extracted) == (dubai.validated_employer, dubai.role_extracted)
    assert unknown.requisition_id == dubai.requisition_id == ""
    assert (unknown.country_code, unknown.city, dubai.country_code, dubai.city) == ("", "", "AE", "Dubai")
    assert unknown.jd_text != dubai.jd_text and unknown.normalized_vacancy_key != dubai.normalized_vacancy_key
    assert unknown.discovery_category == dubai.discovery_category == "incomplete_evidence"


def test_domain_hint_geo_hashtag_role_and_multi_role_splitting(db_session):
    now = utcnow()
    user, profile, pref, ver, _, _, _ = _seed_cafm_user(db_session)

    # Domain suffix hint (.ae / .uk) with NO confirmed text country must NOT pass strict AE filter!
    hint_loc = parse_job_location("", "Looking for experienced Operations Coordinator to manage daily maintenance tasks.", title="Operations Coordinator", source_url="https://acme-jobs.ae/op1")
    assert hint_loc.location_confidence == "domain_hint"
    verif_hint = verify_discovered_candidate(
        DiscoveredCandidate(
            source_provider="tavily",
            source_url="https://acme-jobs.ae/op1",
            source_type="job_post",
            title="Operations Coordinator",
            company_name="Acme AE",
            company_domain="acme-jobs.ae",
            jd_text="Send CV to jobs@acme-jobs.test",
            location_raw="AE",
            location_confidence="domain_hint",
            retrieved_at=now,
            application_email="jobs@acme-jobs.test",
            evidence_quote="Send CV to jobs@acme-jobs.test",
        ),
        include_standing_invitations=True,
        now_utc=now,
        skip_network_dns=True,
        target_country_codes={"AE"},
        parsed_country_code="AE",
        parsed_remote_scope="onsite",
        location_confidence="domain_hint",
    )
    assert "JOB_LOCATION_UNKNOWN" in verif_hint.secondary_skip_codes

    hint_posting = JobPosting(
        user_id=user.id,
        batch_id="b-hint",
        canonical_url_hash="h-hint",
        content_hash="c-hint",
        source_provider="tavily",
        source_url="https://acme-jobs.ae/op1",
        title="CAFM Administrator",
        role_extracted="CAFM Administrator",
        company_name="Acme FM",
        jd_text="CAFM Administrator using IBM Maximo and SQL. Send CV to jobs@acme-jobs.test",
        location_raw="",
        country_code="AE",
        location_confidence="domain_hint",
        remote_scope="onsite",
        application_email="jobs@acme-jobs.test",
        parsing_version="v3.0",
    )
    ev_hint = evaluate_job_against_cv(posting=hint_posting, cv_version=ver, facts=[], pref=pref, profile=profile, now_utc=now)
    assert ev_hint.hard_filter_passed is False
    assert "JOB_LOCATION_UNKNOWN" in ev_hint.failure_codes

    # Zero-skill Cairo/Ops posting MUST NOT receive 18.0 free phantom tool score
    cairo_posting = JobPosting(
        user_id=user.id,
        batch_id="b-cairo",
        canonical_url_hash="h-cairo",
        content_hash="c-cairo",
        source_provider="tavily",
        source_url="https://cairo-ops.test/1",
        title="Fleet Sales Coordinator",
        role_extracted="Fleet Sales Coordinator",
        company_name="Delta Fleet",
        jd_text="Manage automotive dealership sales targets and showroom floor.",
        location_raw="Cairo, Egypt",
        country_code="EG",
        city="Cairo",
        location_confidence="inferred_text",
        remote_scope="onsite",
        application_email="hr@cairo-ops.test",
        parsing_version="v3.0",
    )
    ev_cairo = evaluate_job_against_cv(posting=cairo_posting, cv_version=ver, facts=[], pref=pref, profile=profile, now_utc=now)
    assert ev_cairo.score_components["tools_and_skills"] == 0.0
    assert ev_cairo.fit_score < 65.0

    # Robots / 403 / Informational classification
    v_robots = verify_discovered_candidate(
        DiscoveredCandidate(
            source_provider="tavily",
            source_url="https://blocked.test/post/1",
            source_type="job_post",
            title="Walk-in Fair",
            company_name="",
            company_domain="blocked.test",
            jd_text="Apply at office",
            location_raw="",
            retrieved_at=now,
            page_inaccessible_reason="Source access restricted (robots_disallowed)",
        ),
        include_standing_invitations=False,
        skip_network_dns=True,
    )
    assert v_robots.primary_skip_code == "SOURCE_ROBOTS_BLOCKED"

    v_info = verify_discovered_candidate(
        DiscoveredCandidate(
            source_provider="tavily",
            source_url="https://www.linkedin.com/posts/corp_123",
            source_type="job_post",
            title="Walk-in Interview Notice",
            company_name="",
            company_domain="www.linkedin.com",
            jd_text="Walk-in interview Saturday for technicians.",
            location_raw="",
            retrieved_at=now,
            is_informational_post=True,
        ),
        include_standing_invitations=False,
        skip_network_dns=True,
    )
    assert v_info.discovery_category == "informational_post"

    # Unambiguous multi-role split vs noisy concatenated digest unsplit
    clear_multi = DiscoveredCandidate(
        source_provider="tavily",
        source_url="https://gulf-fm.test/openings",
        source_type="job_post",
        title="Multiple Openings",
        company_name="Gulf FM",
        company_domain="gulf-fm.test",
        jd_text=(
            "1. CAFM Administrator\nDubai UAE IBM Maximo.\n\n"
            "2. Data Analyst\nDubai UAE Power BI & SQL.\n\n"
            "Send your CV to talent@gulf-fm.test"
        ),
        location_raw="Dubai, UAE",
        retrieved_at=now,
        application_email="talent@gulf-fm.test",
        evidence_quote="Send your CV to talent@gulf-fm.test",
        validated_employer="Gulf FM",
    )
    splits = split_clear_multi_role_post(clear_multi)
    assert len(splits) == 2
    assert {s.role_extracted for s in splits} == {"Cafm Administrator", "Data Analyst"}

    noisy_digest = DiscoveredCandidate(
        source_provider="tavily",
        source_url="https://www.linkedin.com/posts/noisy_1",
        source_type="job_post",
        title="Feed",
        company_name="",
        company_domain="www.linkedin.com",
        jd_text=(
            "1. Receptionist\nTo view or add a comment, sign in\n"
            "2. Driver\nTo view or add a comment, sign in\nSend CV to hr@noisy.test"
        ),
        location_raw="",
        retrieved_at=now,
        application_email="hr@noisy.test",
        evidence_quote="Send CV to hr@noisy.test",
    )
    assert len(split_clear_multi_role_post(noisy_digest)) == 1


def test_demo_fixture_and_example_domain_total_isolation(client: TestClient, db_session, monkeypatch):
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()

    user, _, _, ver, _, _, conn = _seed_cafm_user(db_session, email="iso-owner@example.org", with_mailbox=True)
    db_session.commit()

    # Create explicit demo batch + real application pointing illegally to .example
    b_demo = Batch(user_id=user.id, mode="demo_fixture", status="completed")
    db_session.add(b_demo)
    db_session.flush()
    p_demo = JobPosting(
        user_id=user.id,
        batch_id=b_demo.id,
        is_demo=True,
        canonical_url_hash="u-demo-1",
        content_hash="c-demo-1",
        source_provider="demo_fixture",
        source_url="https://crescentlogistics.example/job",
        title="Data Analyst",
        company_name="Crescent Logistics Group",
        jd_text="Send CV to hr@crescentlogistics.example",
        country_code="AE",
        city="Dubai",
        application_email="hr@crescentlogistics.example",
        discovery_category="demo_fixture",
        verification_status="verified",
    )
    db_session.add(p_demo)
    db_session.flush()
    a_demo = Application(
        user_id=user.id,
        batch_id=b_demo.id,
        job_posting_id=p_demo.id,
        mailbox_connection_id=conn.id,
        selected_cv_version_id=ver.id,
        is_demo=True,
        company_name="Crescent Logistics Group",
        role_title="Data Analyst",
        recipient_email="hr@crescentlogistics.example",
        recipient_normalized="hr@crescentlogistics.example",
        transport_status="sent",
        next_follow_up_at=utcnow() - timedelta(hours=1),
        sent_at=utcnow() - timedelta(days=5),
    )
    db_session.add(a_demo)
    db_session.commit()

    # 1. Enqueue & follow-up & scheduler & Google Sheets MUST all block .example / is_demo!
    live_batch = Batch(user_id=user.id, mode="live", status="running")
    db_session.add(live_batch)
    db_session.flush()
    entry, enq_err = enqueue_application_for_send(db_session, user=user, batch=live_batch, app_record=a_demo, conn=conn)
    assert entry is None and "handoff" in enq_err.lower()
    assert a_demo.transport_status == "sent"

    q_cnt, _ = evaluate_and_queue_followups_for_user(db_session, user_id=user.id)
    assert q_cnt == 0
    # Even if an .example row lacked is_demo=True, runtime domain guard blocks follow-up
    a_demo.is_demo = False
    a_demo.transport_status = "sent"
    a_demo.next_follow_up_at = utcnow() - timedelta(hours=1)
    db_session.commit()
    q_cnt2, f_reasons = evaluate_and_queue_followups_for_user(db_session, user_id=user.id)
    assert q_cnt2 == 0 and any("demo" in r.lower() for r in f_reasons)
    a_demo.is_demo = True
    db_session.commit()

    bad_sched = Schedule(user_id=user.id, name="Demo Sched", mode="demo_fixture", days_of_week_json=[0, 1, 2, 3, 4, 5, 6], local_time_hhmm="09:00", timezone="Asia/Dubai", is_active=True)
    db_session.add(bad_sched)
    db_session.commit()
    assert dispatch_due_schedules(db_session) == []

    # 2. Google Sheets sync skips is_demo rows
    db_session.add(
        SheetsConnection(
            user_id=user.id,
            status="connected",
            sheet_name="Applications",
            spreadsheet_id="ss-demo-iso",
            encrypted_credentials=encrypt_json({"access_token": "tok", "refresh_token": "ref", "expires_at": (utcnow() + timedelta(hours=1)).isoformat()}),
        )
    )
    db_session.commit()
    sync_res = sync_applications_to_google_sheet(db_session, user_id=user.id)
    assert sync_res["rows_synced"] == 0 and sync_res["total_applications"] == 0

    # 3. API rejects scheduling demo_fixture via HTTP
    # Authenticate via test helper
    from tests.test_security_and_isolation import _register_and_auth

    _, headers = _register_and_auth(client, "sched-demo-guard@example.org")
    r_sched = client.post(
        "/api/v1/schedules",
        json={"name": "Bad Demo", "mode": "demo_fixture", "days_of_week": [1], "local_time_hhmm": "09:00", "timezone": "UTC"},
        headers=headers,
    )
    assert r_sched.status_code in (400, 422)


def test_controlled_owner_mailbox_send_stop_concurrency_ambiguity_and_bounce(db_session, monkeypatch):
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()

    user, _, _, ver, _, sha, conn = _seed_cafm_user(db_session, email="owner-mailbox@acme-eval.test", with_mailbox=True)
    now = utcnow()
    cand = DiscoveredCandidate(
        source_provider="career_page",
        source_url="https://acme-eval.test/jobs/cafm-1",
        source_type="job_post",
        title="CAFM Administrator",
        company_name="Acme Eval Facilities",
        company_domain="acme-eval.test",
        jd_text="CAFM Administrator in Dubai UAE with IBM Maximo, SQL, and Power BI. Send CV to owner-test-rx@acme-eval.test Subject: REF-OWNER-E2E",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="owner-test-rx@acme-eval.test",
        subject_instruction="REF-OWNER-E2E",
        evidence_quote="Send CV to owner-test-rx@acme-eval.test Subject: REF-OWNER-E2E",
        validated_employer="Acme Eval Facilities",
        role_extracted="CAFM Administrator",
    )

    transport = _OwnerMailboxTransport()
    live_batch = Batch(user_id=user.id, mode="live", status="queued")
    db_session.add(live_batch)
    db_session.commit()

    execute_batch_pipeline(
        db_session,
        batch=live_batch,
        candidate_overrides=[cand],
        skip_network_dns=True,
        auto_process_outbox_sync=True,
        transport_override=transport,
    )

    from tests.test_batch_pipeline import approve_and_dispatch

    assert not transport.sent
    approve_and_dispatch(db_session, user, live_batch, conn, transport)

    # Verify exact CV SHA-256 checksum, subject, sender identity, and delivery status without fake read claim
    assert len(transport.sent) == 1
    msg = transport.sent[0]
    assert msg.to_email == "owner-test-rx@acme-eval.test"
    assert "REF-OWNER-E2E" in msg.subject
    assert compute_sha256(msg.attachments[0].data) == sha
    app_sent = db_session.query(Application).filter(Application.batch_id == live_batch.id).one()
    assert app_sent.transport_status == "sent"
    assert app_sent.response_status == "none"

    # Ambiguous SMTP timeout marks send_uncertain without blind duplicate retry
    cand_ambig = DiscoveredCandidate(
        source_provider="career_page",
        source_url="https://acme-ambig.test/jobs/1",
        source_type="job_post",
        title="Data Analyst",
        company_name="Ambig FM",
        company_domain="acme-ambig.test",
        jd_text="Data Analyst in Dubai UAE with SQL and Power BI. Send CV to ambig@acme-ambig.test",
        location_raw="Dubai, UAE",
        location_confidence="explicit",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="ambig@acme-ambig.test",
        evidence_quote="Send CV to ambig@acme-ambig.test",
        validated_employer="Ambig FM",
        role_extracted="Data Analyst",
    )
    ambig_transport = _OwnerMailboxTransport(outcome=HandoffOutcome.AMBIGUOUS_HANDOFF)
    b_ambig = Batch(user_id=user.id, mode="live", status="queued")
    db_session.add(b_ambig)
    db_session.commit()
    execute_batch_pipeline(
        db_session,
        batch=b_ambig,
        candidate_overrides=[cand_ambig],
        skip_network_dns=True,
        auto_process_outbox_sync=True,
        transport_override=ambig_transport,
    )
    approve_and_dispatch(db_session, user, b_ambig, conn, ambig_transport)
    app_ambig = db_session.query(Application).filter(Application.batch_id == b_ambig.id).one()
    assert app_ambig.transport_status == "send_uncertain"

    # Bounce ingestion suppresses future sends to bounced address
    ingest_inbound_messages(
        db_session,
        conn=conn,
        records=[
            InboundMessageRecord(
                provider_message_id="dsn-1",
                provider_thread_id="owner-thr-1",
                rfc_message_id="<dsn@acme-eval.test>",
                in_reply_to=app_sent.rfc_message_id,
                references="",
                sender_email="mailer-daemon@acme-eval.test",
                recipient_email="owner-mailbox@acme-eval.test",
                subject="Delivery Status Notification (Failure)",
                body_snippet="550 5.1.1 User unknown: owner-test-rx@acme-eval.test permanent failure",
                received_at=utcnow(),
            )
        ],
    )
    db_session.refresh(app_sent)
    assert app_sent.response_status == "permanent_bounce"
    supp = db_session.query(RecipientHistory).filter(
        RecipientHistory.user_id == user.id,
        RecipientHistory.normalized_email == "owner-test-rx@acme-eval.test",
        RecipientHistory.source == "suppressed",
    ).first()
    assert supp is not None

    # Global Stop halts queued sends before transport
    activate_stop_automation(db_session, user=user)
    user.automation_stopped_at = utcnow()
    db_session.commit()

    # Concurrent worker recipient reservation yields strictly 1 winner
    def _conc_worker(_idx: int) -> str:
        sess = get_sessionmaker()()
        try:
            reserve_recipients_atomically(
                sess,
                user_id=user.id,
                application_id=None,
                to_email="conc-owner@acme-eval.test",
                cc_emails=[],
                reject_demo=True,
            )
            sess.commit()
            return "won"
        except DuplicateRecipientError:
            sess.rollback()
            return "duplicate"
        except Exception:
            sess.rollback()
            return "locked"
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(_conc_worker, range(4)))
    assert outcomes.count("won") == 1


def test_recent_incomplete_evidence_revisited_and_listed_in_discovery_api(client: TestClient, db_session, monkeypatch):
    from tests.test_security_and_isolation import _register_and_auth

    _, headers = _register_and_auth(client, "disc-api@example.org")
    me = client.get("/api/v1/auth/me").json()["user"]
    user_id = me["id"]
    now = utcnow()

    client.put(
        "/api/v1/profile",
        json={
            "full_name": "Nadia Ali",
            "contact_email": "disc-api@example.org",
            "residence_country_code": "AE",
            "residence_city": "Dubai",
            "work_authorizations": ["AE"],
            "confirm_profile": True,
        },
        headers=headers,
    )
    doc = CvDocument(user_id=user_id, variant_label="Nadia CV")
    db_session.add(doc)
    db_session.flush()
    cv = CvVersion(
        cv_document_id=doc.id,
        user_id=user_id,
        version_number=1,
        original_filename="nadia.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="b" * 64,
        storage_key="k",
        parse_status="parsed",
        detected_language="en",
        extracted_text="CAFM Administrator Dubai UAE IBM Maximo",
        total_experience_months=48,
    )
    pref = db_session.query(SearchPreference).filter(SearchPreference.user_id == user_id).one()
    pref.target_titles_json = ["CAFM Administrator"]
    db_session.add(cv)
    db_session.flush()
    doc.active_version_id = cv.id

    from app.services.batch_runner import _canonicalize_url_for_hash, _hash_str

    canon = _canonicalize_url_for_hash("https://nodata.test/post/1?utm_source=linkedin")
    b_seed = Batch(id="b-seed", user_id=user_id, mode="dry_run", status="completed")
    p_inc = JobPosting(
        user_id=user_id,
        batch_id="b-seed",
        is_demo=False,
        canonical_url_hash=_hash_str(canon),
        content_hash="c-seed",
        source_provider="tavily",
        source_url="https://nodata.test/post/1?utm_source=linkedin",
        canonical_url=canon,
        title="Hiring Engineer",
        company_domain="nodata.test",
        publisher="nodata.test",
        jd_text="Exciting role in Dubai UAE, click to view.",
        discovery_category="incomplete_evidence",
        primary_skip_code="NO_EMAIL_INVITATION",
        first_seen_at=now - timedelta(days=3),
    )
    db_session.add_all([b_seed, p_inc])
    db_session.commit()

    # A recent sighting and a snippet without an address still warrant a fresh page check.
    class _IncompleteProv:
        name = "tavily"

        async def search(self, query: str, **_kw):
            return SearchProviderOutcome(
                provider_name="tavily",
                query=query,
                candidates=[
                    DiscoveredCandidate(
                        source_provider="tavily",
                        source_url="https://nodata.test/post/1?fbclid=xyz999",
                        source_type="job_post",
                        title="Hiring Engineer",
                        company_name="",
                        company_domain="nodata.test",
                        jd_text="Exciting role in Dubai UAE — check website now!",
                        location_raw="",
                        retrieved_at=now,
                    )
                ],
            )

    fetched = []

    async def fetch(url, **kwargs):
        fetched.append(url)
        return None, "HTTP 403"

    monkeypatch.setattr("app.services.batch_runner.fetch_and_parse_career_page", fetch)
    b_next = Batch(user_id=user_id, mode="dry_run", status="queued",
                   budget_config_json={"max_employer_followup_queries": 0, "max_email_corroboration_queries": 0})
    db_session.add(b_next)
    db_session.commit()
    execute_batch_pipeline(
        db_session,
        batch=b_next,
        search_providers_override=[_IncompleteProv()],
        skip_network_dns=True,
        max_search_queries_override=1,
    )
    assert fetched == ["https://nodata.test/post/1?fbclid=xyz999"]
    assert b_next.duplicates_removed_count == 0

    # Discovery API lists the raw incomplete record WITHOUT putting it in /applications
    assert client.get("/api/v1/applications").json()["total"] == 0
    disc_res = client.get("/api/v1/discovery-sources?scope=real")
    assert disc_res.status_code == 200
    assert disc_res.json()["total"] == 2
    assert {item["primary_skip_code"] for item in disc_res.json()["items"]} == {"NO_EMAIL_INVITATION", "SOURCE_HTTP_403"}
    dups_res = client.get("/api/v1/discovery-sources?scope=real&duplicates_only=true")
    assert dups_res.status_code == 200
    assert dups_res.json()["total"] == 0

    csv_res = client.get("/api/v1/exports/discovery.csv?scope=real")
    assert csv_res.status_code == 200
    csv_rows = list(csv.reader(io.StringIO(csv_res.text.lstrip("\ufeff"))))
    assert "Actual Employer" in csv_rows[0]
    assert len(csv_rows) >= 2

