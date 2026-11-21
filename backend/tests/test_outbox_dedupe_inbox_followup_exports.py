"""Tests for dry-run vs live isolation, dedupe, atomic budgets, crash recovery, replies/follow-ups, and safe exports."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

from openpyxl import load_workbook

from app.db.base import utcnow
from app.db.models import (
    Application,
    Batch,
    CandidateProfile,
    CvDocument,
    CvExtractedFact,
    CvVersion,
    JobPosting,
    MailboxConnection,
    OutboxEntry,
    RecipientHistory,
    SearchPreference,
    User,
)
from app.services.account_lifecycle import activate_stop_automation
from app.services.approval import application_draft_hash, approve_application_for_send
from app.services.batch_runner import execute_batch_pipeline
from app.services.dedupe import import_historical_recipients
from app.services.discovery.base import DiscoveredCandidate
from app.services.exports import build_tracker_rows, export_applications_csv, export_applications_xlsx
from app.services.followup import evaluate_and_queue_followups_for_user
from app.services.inbox_processor import ingest_inbound_messages
from app.services.mailbox.base import HandoffOutcome, InboundMessageRecord, OutboundEmailMessage, SendResult
from app.services.outbox import (
    claim_next_outbox_entry,
    count_rolling_24h_recipients,
    enqueue_application_for_send,
    process_outbox_entry,
    recover_stale_outbox_leases,
)
from app.services.storage import compute_sha256, get_storage_adapter
from tests.conftest import build_pdf_bytes


class RecordingTransport:
    def __init__(self, outcome: HandoffOutcome = HandoffOutcome.PROVIDER_ACCEPTED, rate_limit: bool = False):
        self.sent: list[OutboundEmailMessage] = []
        self.outcome = outcome
        self.rate_limit = rate_limit
        self.reconcile_result: SendResult | None = None

    def send_message(self, msg: OutboundEmailMessage) -> SendResult:
        self.sent.append(msg)
        if self.rate_limit:
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_REJECTED,
                rfc_message_id=msg.rfc_message_id,
                error_code="RATE_LIMITED",
                error_message="429 Too Many Requests",
                retry_after_seconds=120,
                is_rate_limit=True,
            )
        if self.outcome == HandoffOutcome.PROVIDER_ACCEPTED:
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_ACCEPTED,
                provider_message_id=f"gm-{len(self.sent)}",
                provider_thread_id=f"thr-{len(self.sent)}",
                rfc_message_id=msg.rfc_message_id,
            )
        return SendResult(
            outcome=self.outcome,
            rfc_message_id=msg.rfc_message_id,
            error_message="Simulated failure/ambiguity",
        )

    def reconcile_sent_message(self, rfc_message_id: str, action_id: str) -> SendResult | None:
        return self.reconcile_result

    def poll_inbox(self, cursor: str):
        return [], cursor


def _seed_live_user(db, *, limit: int = 20):
    user = User(email="live@example.org", email_normalized="live@example.org", timezone="UTC", email_verified_at=utcnow())
    db.add(user)
    db.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name="Omar Farouk",
        contact_email="live@example.org",
        residence_country_code="AE",
        residence_city="Dubai",
        work_authorizations_json=["AE", "EG"],
        confirmed_at=utcnow(),
        signature_text="Best regards,\nOmar Farouk",
    )
    pref = SearchPreference(
        user_id=user.id,
        target_titles_json=["Data Analyst"],
        target_locations_json=[{"country_code": "AE", "city": "Dubai", "label": "Dubai, UAE"}],
        min_match_score=40,
        daily_recipient_limit=limit,
        followup_interval_days=7,
        followup_max_count=1,
    )
    conn = MailboxConnection(
        user_id=user.id,
        provider="gmail_oauth",
        label="Gmail",
        sender_email="live@example.org",
        granted_scopes_json=["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"],
        status="connected",
        inbox_poll_enabled=True,
        inbox_last_polled_at=utcnow(),
        inbox_health_status="healthy",
    )
    doc = CvDocument(user_id=user.id, variant_label="Analytics")
    db.add_all([profile, pref, conn, doc])
    db.flush()

    pdf_bytes = build_pdf_bytes(["Omar Farouk", "Data Analyst 2021-01 - 2024-01", "Skills: Python, SQL, Power BI"])
    sha = compute_sha256(pdf_bytes)
    storage_key = f"users/{user.id}/cvs/{doc.id}/v1_omar.pdf"
    get_storage_adapter().put_bytes(storage_key, pdf_bytes, "application/pdf")

    ver = CvVersion(
        cv_document_id=doc.id,
        user_id=user.id,
        version_number=1,
        original_filename="omar_cv.pdf",
        mime_type="application/pdf",
        size_bytes=len(pdf_bytes),
        sha256=sha,
        storage_key=storage_key,
        parse_status="parsed",
        detected_language="en",
        extracted_text="Omar Farouk Data Analyst Python SQL Power BI",
        total_experience_months=36,
    )
    db.add(ver)
    db.flush()
    doc.active_version_id = ver.id
    db.add(
        CvExtractedFact(
            cv_version_id=ver.id,
            user_id=user.id,
            category="tool",
            title="Python",
            value="Python",
            evidence_quote="Skills: Python, SQL, Power BI",
            confidence="evidenced",
        )
    )
    db.commit()
    return user, conn, ver, pdf_bytes, sha


def test_dry_run_vs_live_dedupe_cc_budget_and_frozen_attachment(db_session, monkeypatch):
    user, conn, ver, pdf_bytes, sha = _seed_live_user(db_session, limit=3)
    now = utcnow()
    cand1 = DiscoveredCandidate(
        source_provider="tavily",
        source_url="https://co1.test/jobs/1",
        source_type="job_post",
        title="Data Analyst",
        company_name="CoOne",
        company_domain="co1.test",
        jd_text="Need Python and SQL. Send CV to hr@co1.test and CC mgr@co1.test. Subject: REF-100",
        location_raw="Dubai, UAE",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="hr@co1.test",
        cc_emails=["mgr@co1.test"],
        subject_instruction="REF-100",
        evidence_quote="Send CV to hr@co1.test and CC mgr@co1.test.",
    )
    cand2 = DiscoveredCandidate(
        source_provider="tavily",
        source_url="https://co2.test/jobs/2",
        source_type="job_post",
        title="Data Analyst",
        company_name="CoTwo",
        company_domain="co2.test",
        jd_text="Need Python and SQL. Send CV to jobs@co2.test and CC lead@co2.test.",
        location_raw="Dubai, UAE",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="jobs@co2.test",
        cc_emails=["lead@co2.test"],
        evidence_quote="Send CV to jobs@co2.test and CC lead@co2.test.",
    )

    transport = RecordingTransport()

    # 1. Dry run MUST NOT send or consume RecipientHistory
    dry_batch = Batch(user_id=user.id, mode="dry_run", status="queued")
    db_session.add(dry_batch)
    db_session.commit()
    execute_batch_pipeline(
        db_session,
        batch=dry_batch,
        candidate_overrides=[cand1],
        skip_network_dns=True,
        auto_process_outbox_sync=True,
        transport_override=transport,
    )
    assert dry_batch.drafted_count == 1
    assert len(transport.sent) == 0
    assert db_session.query(RecipientHistory).count() == 0

    # 2. Live run with LIVE_SENDING_ENABLED=false MUST NOT call transport
    live_blocked = Batch(user_id=user.id, mode="live", status="queued")
    db_session.add(live_blocked)
    db_session.commit()
    execute_batch_pipeline(
        db_session,
        batch=live_blocked,
        candidate_overrides=[cand1],
        skip_network_dns=True,
        auto_process_outbox_sync=True,
        transport_override=transport,
    )
    assert len(transport.sent) == 0

    # Release reservation from blocked test so we can test actual live send
    for rh in db_session.query(RecipientHistory).all():
        db_session.delete(rh)
    db_session.commit()

    # 3. Enable live sending toggle and import prior history for co3
    from app.core.config import get_settings

    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    import_historical_recipients(db_session, user_id=user.id, entries=[{"email": "old@co3.test", "company": "CoThree"}])

    cand3_dup = DiscoveredCandidate(
        source_provider="tavily",
        source_url="https://co3.test/jobs/3",
        source_type="job_post",
        title="Data Analyst",
        company_name="CoThree",
        company_domain="co3.test",
        jd_text="Need Python. Send CV to OLD@co3.test.",
        location_raw="Dubai, UAE",
        retrieved_at=now,
        posted_at=now - timedelta(days=1),
        application_email="OLD@co3.test",
        evidence_quote="Send CV to OLD@co3.test.",
    )

    live_batch = Batch(user_id=user.id, mode="live", status="queued")
    db_session.add(live_batch)
    db_session.commit()
    execute_batch_pipeline(
        db_session,
        batch=live_batch,
        candidate_overrides=[cand1, cand2, cand3_dup],
        skip_network_dns=True,
        auto_process_outbox_sync=True,
        transport_override=transport,
    )
    assert transport.sent == []
    assert db_session.query(OutboxEntry).count() == 0
    for recipient in ("hr@co1.test", "jobs@co2.test"):
        draft = db_session.query(Application).filter_by(batch_id=live_batch.id, recipient_email=recipient).one()
        ok, reason = approve_application_for_send(db_session, user=user, app_rec=draft)
        assert ok, reason
        db_session.commit()
        entry, reason = enqueue_application_for_send(db_session, user=user, batch=live_batch, app_record=draft, conn=conn)
        assert entry, reason
        claimed = claim_next_outbox_entry(db_session, worker_id="test", outbox_id=entry.id)
        assert claimed
        process_outbox_entry(db_session, entry=claimed, transport_override=transport, ignore_window_for_test=True)
    # cand1 consumed 2 budget slots (To + CC). cand2 needs 2 slots (exceeds limit=3!) -> quota_paused!
    # cand3_dup skipped by case-insensitive historical import dedupe!
    assert len(transport.sent) == 1
    sent_msg = transport.sent[0]
    assert sent_msg.to_email == "hr@co1.test"
    assert sent_msg.cc_emails == ["mgr@co1.test"]
    assert "REF-100" in sent_msg.subject
    assert compute_sha256(sent_msg.attachments[0].data) == sha
    assert count_rolling_24h_recipients(db_session, user_id=user.id) == 2

    # 4. Repeat batch on cand1 MUST NOT duplicate initial application
    repeat_batch = Batch(user_id=user.id, mode="live", status="queued")
    db_session.add(repeat_batch)
    db_session.commit()
    execute_batch_pipeline(
        db_session,
        batch=repeat_batch,
        candidate_overrides=[cand1],
        skip_network_dns=True,
        auto_process_outbox_sync=True,
        transport_override=transport,
    )
    assert len(transport.sent) == 1
    assert repeat_batch.skipped_count == 1


def test_crash_recovery_uncertain_send_and_cancelled_batch_and_rate_limit(db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    user, conn, ver, _, _ = _seed_live_user(db_session, limit=20)
    now = utcnow()

    batch = Batch(user_id=user.id, mode="live", status="running")
    db_session.add(batch)
    db_session.flush()
    post = JobPosting(
        user_id=user.id,
        batch_id=batch.id,
        canonical_url_hash="u-crash",
        content_hash="c-crash",
        source_provider="tavily",
        source_url="https://crash.test/1",
        title="Data Analyst",
        company_name="CrashCo",
        jd_text="Python",
        location_raw="Dubai, UAE",
        country_code="AE",
        remote_scope="onsite",
        application_email="hr@crash.test",
    )
    db_session.add(post)
    db_session.flush()

    app_rec = Application(
        user_id=user.id,
        batch_id=batch.id,
        job_posting_id=post.id,
        mailbox_connection_id=conn.id,
        selected_cv_version_id=ver.id,
        frozen_cv_sha256=ver.sha256,
        frozen_cv_filename=ver.original_filename,
        company_name="CrashCo",
        role_title="Data Analyst",
        recipient_email="hr@crash.test",
        recipient_normalized="hr@crash.test",
        subject="Application",
        body_text="Hello",
        transport_status="sending",
        rfc_message_id="<msg1@example.org>",
    )
    db_session.add(app_rec)
    db_session.flush()

    # Simulate crashed worker holding expired `sending` lease
    stale_entry = OutboxEntry(
        user_id=user.id,
        application_id=app_rec.id,
        mailbox_connection_id=conn.id,
        kind="initial",
        action_id="act-crash-1",
        rfc_message_id="<msg1@example.org>",
        status="sending",
        lease_owner="dead-worker",
        lease_expires_at=now - timedelta(minutes=5),
    )
    db_session.add(stale_entry)
    db_session.commit()

    transport = RecordingTransport()
    rec_stats = recover_stale_outbox_leases(db_session, transport_factory=lambda _db, _c: transport)
    assert rec_stats["marked_uncertain"] == 1
    db_session.refresh(stale_entry)
    db_session.refresh(app_rec)
    assert stale_entry.status == "send_uncertain"
    assert app_rec.transport_status == "send_uncertain"
    assert len(transport.sent) == 0

    # Cancelled / stop-automation prevents queued unclaimed work from sending
    queued_entry = OutboxEntry(
        user_id=user.id,
        application_id=app_rec.id,
        mailbox_connection_id=conn.id,
        kind="initial",
        action_id="act-cancel-2",
        rfc_message_id="<msg2@example.org>",
        status="queued",
    )
    db_session.add(queued_entry)
    db_session.commit()
    activate_stop_automation(db_session, user=user)
    db_session.refresh(queued_entry)
    assert queued_entry.status == "cancelled"

    # Rate-limited provider response pauses batch cleanly
    user.automation_stopped_at = None
    batch.status = "running"
    app_rec.transport_status = "ready"
    rl_entry = OutboxEntry(
        user_id=user.id,
        application_id=app_rec.id,
        mailbox_connection_id=conn.id,
        kind="initial",
        action_id="act-rl-3",
        rfc_message_id="<msg3@example.org>",
        status="queued",
    )
    app_rec.approval_state = "approved"
    app_rec.approved_content_hash = application_draft_hash(app_rec)
    db_session.add(rl_entry)
    db_session.commit()
    claimed = claim_next_outbox_entry(db_session, worker_id="w1", outbox_id=rl_entry.id)
    assert claimed is not None
    res = process_outbox_entry(db_session, entry=claimed, transport_override=RecordingTransport(rate_limit=True), ignore_window_for_test=True)
    assert res == "rate_limited"
    db_session.refresh(batch)
    assert batch.status == "quota_paused"


def test_inbox_replies_bounces_stale_followups_and_safe_exports(db_session):
    user, conn, ver, _, _ = _seed_live_user(db_session, limit=20)
    now = utcnow()
    batch = Batch(user_id=user.id, mode="live", status="completed")
    db_session.add(batch)
    db_session.flush()
    post = JobPosting(
        user_id=user.id,
        batch_id=batch.id,
        canonical_url_hash="u-inb",
        content_hash="c-inb",
        source_provider="tavily",
        source_url="https://inb.test/1",
        title="Data Analyst",
        company_name="=CMD|'/C calc'!A0",
        jd_text="Python",
        location_raw="Dubai, UAE",
        country_code="AE",
        remote_scope="onsite",
        application_email="hr@inb.test",
    )
    db_session.add(post)
    db_session.flush()

    app1 = Application(
        user_id=user.id,
        batch_id=batch.id,
        job_posting_id=post.id,
        mailbox_connection_id=conn.id,
        selected_cv_version_id=ver.id,
        company_name="=CMD|'/C calc'!A0",
        role_title="Data Analyst",
        recipient_email="hr@inb.test",
        recipient_normalized="hr@inb.test",
        transport_status="sent",
        provider_thread_id="thr-100",
        rfc_message_id="<app1@inb.test>",
        next_follow_up_at=now - timedelta(hours=1),
        sent_at=now - timedelta(days=7),
        notes="+SUM(1,2)",
    )
    app2 = Application(
        user_id=user.id,
        batch_id=batch.id,
        job_posting_id=post.id,
        mailbox_connection_id=conn.id,
        selected_cv_version_id=ver.id,
        company_name="AckCo",
        role_title="Data Analyst",
        recipient_email="jobs@ackco.test",
        recipient_normalized="jobs@ackco.test",
        transport_status="sent",
        provider_thread_id="thr-200",
        rfc_message_id="<app2@ackco.test>",
        next_follow_up_at=now - timedelta(hours=1),
        sent_at=now - timedelta(days=7),
    )
    db_session.add_all([app1, app2])
    db_session.commit()

    # Inbound human interview reply on app1 stops follow-up and infers interview
    # Inbound auto-ack on app2 preserves follow-up
    ingest_inbound_messages(
        db_session,
        conn=conn,
        records=[
            InboundMessageRecord(
                provider_message_id="m-1",
                provider_thread_id="thr-100",
                rfc_message_id="<rep1@inb.test>",
                in_reply_to="<app1@inb.test>",
                references="",
                sender_email="Recruiter <hr@inb.test>",
                recipient_email="live@example.org",
                subject="Re: Application",
                body_snippet="We would like to schedule an interview with you next week.",
                received_at=now,
            ),
            InboundMessageRecord(
                provider_message_id="m-2",
                provider_thread_id="thr-200",
                rfc_message_id="<ack2@ackco.test>",
                in_reply_to="<app2@ackco.test>",
                references="",
                sender_email="jobs@ackco.test",
                recipient_email="live@example.org",
                subject="Application received",
                body_snippet="Thank you for applying. This is an automated confirmation; do not reply.",
                received_at=now,
            ),
        ],
    )
    db_session.refresh(app1)
    db_session.refresh(app2)
    assert app1.response_status == "human_reply"
    assert app1.outcome_status == "interview"
    assert app1.next_follow_up_at is None
    assert app2.response_status == "auto_ack"
    assert app2.next_follow_up_at is not None

    # Stale inbox suppresses unattended follow-up on app2
    conn.inbox_last_polled_at = now - timedelta(hours=10)
    db_session.commit()
    queued_stale, reasons_stale = evaluate_and_queue_followups_for_user(db_session, user_id=user.id)
    assert queued_stale == 0
    assert any("stale" in r for r in reasons_stale)

    # Fresh inbox queues app2 follow-up exactly once
    conn.inbox_last_polled_at = now
    conn.inbox_health_status = "healthy"
    db_session.commit()
    queued_ok, _ = evaluate_and_queue_followups_for_user(db_session, user_id=user.id)
    assert queued_ok == 1

    # Formula-injection protection on CSV & XLSX exports
    rows = build_tracker_rows(
        [app1],
        batches_by_id={batch.id: batch},
        postings_by_id={post.id: post},
        matches_by_id={},
        cv_labels_by_version_id={},
        tz_name="Asia/Dubai",
    )
    csv_bytes = export_applications_csv(rows)
    csv_rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    assert csv_rows[1][6].startswith("'=CMD")
    assert csv_rows[1][-1].startswith("'+SUM")

    xlsx_bytes = export_applications_xlsx(rows, tz_name="Asia/Dubai")
    wb = load_workbook(io.BytesIO(xlsx_bytes))
    assert str(wb["Applications"]["G2"].value).startswith("'=CMD")
