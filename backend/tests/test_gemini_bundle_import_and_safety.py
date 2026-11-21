"""Comprehensive Gate A & Gate B regression tests for Gemini ZIP import, approval invalidation,
outbox/transport reliability, owner self-test lock, and live_sending_enabled safety invariants.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import (
    Application,
    AuditEvent,
    Batch,
    CandidateProfile,
    CvVersion,
    GmailSentRecord,
    InstallationConfig,
    MailboxConnection,
    OutboxEntry,
    SearchPreference,
    User,
)
from app.services.approval import application_draft_hash, approve_application_for_send
from app.services.bundle_import import BundleImportError, import_gemini_bundle_zip, parse_untrusted_gemini_zip
from app.services.effective_config import resolve_effective_live_sending
from app.services.mailbox.base import HandoffOutcome, OutboundEmailMessage, SendResult
from app.services.outbox import (
    claim_next_outbox_entry,
    enqueue_application_for_send,
    process_outbox_entry,
    recover_stale_outbox_leases,
    send_owner_self_test_email,
)
from app.services.setup_service import complete_installation_setup
from app.services.storage import compute_sha256
from tests.conftest import build_pdf_bytes


class FakeTransport:
    def __init__(self, outcome: HandoffOutcome = HandoffOutcome.PROVIDER_ACCEPTED, reconcile_hit: bool = True) -> None:
        self.outcome = outcome
        self.reconcile_hit = reconcile_hit
        self.sent_messages: list[OutboundEmailMessage] = []

    def send_message(self, msg: OutboundEmailMessage) -> SendResult:
        self.sent_messages.append(msg)
        if self.outcome == HandoffOutcome.PROVIDER_ACCEPTED:
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_ACCEPTED,
                provider_message_id=f"fake-msg-{len(self.sent_messages)}",
                provider_thread_id=f"fake-th-{len(self.sent_messages)}",
                rfc_message_id=msg.rfc_message_id,
            )
        if self.outcome == HandoffOutcome.AMBIGUOUS_HANDOFF:
            return SendResult(
                outcome=HandoffOutcome.AMBIGUOUS_HANDOFF,
                error_code="PROVIDER_TIMEOUT",
                error_message="Simulated network timeout after SMTP DATA handoff",
                rfc_message_id=msg.rfc_message_id,
            )
        return SendResult(
            outcome=self.outcome,
            error_code="SMTP_TEMP_FAIL",
            error_message="Simulated transient SMTP error",
            rfc_message_id=msg.rfc_message_id,
        )

    def reconcile_sent_message(self, rfc_message_id: str, action_id: str) -> SendResult | None:
        if self.reconcile_hit:
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_ACCEPTED,
                provider_message_id="reconciled-sent-1",
                provider_thread_id="reconciled-th-1",
                rfc_message_id=rfc_message_id,
            )
        return None


def _build_zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _seed_owner_user(client: TestClient, db_session, *, email: str = "owner@sample-aae466.example.org") -> tuple[User, str, MailboxConnection]:
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "OwnerPassword123!", "full_name": "Test Owner"},
    )
    assert reg.status_code == 201, reg.text
    csrf = reg.json()["user"]["csrf_token"]
    user = db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    profile = db_session.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    profile.residence_country_code = "AE"
    profile.residence_city = "Dubai"
    profile.work_authorizations_json = ["AE"]
    profile.willing_to_relocate = True
    profile.relocation_targets_json = []
    profile.notice_period_days = 30
    pref = db_session.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    pref.target_locations_json = []
    pref.allow_relocation = False
    pref.min_match_score = 65
    pref.require_official_or_authorized_send = True

    conn = MailboxConnection(
        user_id=user.id,
        provider="gmail_oauth",
        label="Owner Gmail",
        sender_email=email,
        status="connected",
        is_primary=True,
        sent_sync_status="ok",
        sent_last_synced_at=utcnow(),
        sent_earliest_covered_at=utcnow() - timedelta(days=30),
        sent_records_count=5,
        sent_sync_cursor="123",
        sent_sync_checkpoint_json={"mode": "incremental", "anchor": "123", "page": None},
        granted_scopes_json=["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"],
    )
    db_session.add(conn)
    db_session.commit()
    return user, csrf, conn


def test_live_sending_cannot_implicitly_enable_across_lifecycle(client: TestClient, db_session, capsys) -> None:
    """Startup, claim token generation, setup completion, ZIP import, and restarts must keep live_sending_enabled False."""
    from app.cli_bootstrap import ensure_setup_claim_token
    from app.main import create_app

    # 1. Default fresh state
    enabled, reason = resolve_effective_live_sending(db_session)
    assert enabled is False
    assert get_settings().live_sending_enabled is False

    # 2. Unclaimed installation DB row with stray True flag must NOT implicitly enable live sending
    inst = InstallationConfig(id="default", setup_state="unclaimed", live_sending_enabled=True, owner_user_id=None)
    db_session.add(inst)
    db_session.commit()
    enabled_unclaimed, _ = resolve_effective_live_sending(db_session)
    assert enabled_unclaimed is False
    inst.live_sending_enabled = False
    db_session.commit()

    # 3. Setup claim token bootstrap + owner registration + setup completion
    rc = ensure_setup_claim_token()
    assert rc == 0
    out_lines = [ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    code_info = json.loads(out_lines[-1])
    claim = client.post(
        "/api/v1/setup/claim",
        json={"setup_code": code_info["setup_code"], "email": "safety-owner@sample-aae466.example.org", "password": "SafePassword123!"},
    )
    assert claim.status_code == 200
    csrf = claim.json()["user"]["csrf_token"]
    owner = db_session.scalar(select(User).where(User.email == "safety-owner@sample-aae466.example.org"))
    assert owner is not None
    complete_installation_setup(db_session, user=owner)
    assert resolve_effective_live_sending(db_session)[0] is False

    # 4. Importing a Gemini ZIP must never flip live sending on
    pdf_bytes = build_pdf_bytes(["Owner Name", "CAFM Coordinator Dubai UAE", "Skills: Maximo, SQL, Power BI"])
    mini_zip = _build_zip_bytes(
        {
            "email_queue_fresh_10.json": json.dumps(
                [
                    {
                        "company": "Studio Zero",
                        "role": "CAFM Coordinator",
                        "location": "Dubai, UAE",
                        "to": "jobs@studio-0.test",
                        "subject": "CAFM Coordinator",
                        "source_url": "https://studio-0.test/careers",
                        "attachments": ["CV.pdf"],
                        "body": "Dear Hiring Team, please find attached my CV for the CAFM Coordinator opening.",
                    }
                ]
            ).encode("utf-8"),
            "CV.pdf": pdf_bytes,
            "send_fresh_batch.py": b"raise RuntimeError('MUST NEVER EXECUTE')",
        }
    )
    r_imp = client.post(
        "/api/v1/batches/import-bundle",
        files={"file": ("bundle.zip", mini_zip, "application/zip")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_imp.status_code == 200, r_imp.text
    assert resolve_effective_live_sending(db_session)[0] is False

    # 5. Simulated app restart (new create_app instance) + health endpoint
    restarted_app = create_app()
    restarted_client = TestClient(restarted_app)
    health = restarted_client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["live_sending_enabled"] is False


def test_synthetic_bundle_idempotency_and_invalidation(client: TestClient, db_session) -> None:
    """Replay ten fictional applications against four generated CVs and a mocked mailbox."""
    user, csrf, conn = _seed_owner_user(client, db_session, email="gate-a-owner@sample-aae466.example.org")

    # Pre-seed a previously sent recipient in GmailSentRecord to verify Gmail Sent dedup on import
    db_session.add(
        GmailSentRecord(
            user_id=user.id,
            mailbox_connection_id=conn.id,
            provider_message_id="gmail-sent-fixture-1",
            thread_id="th-fixture-1",
            recipient_email="jobs@studio-7.test",
            recipient_normalized="jobs@studio-7.test",
            recipient_domain="studio-7.test",
            subject="Document Controller",
            sent_at=utcnow() - timedelta(days=2),
            classification="job_application",
            is_suppressed_recipient=True,
        )
    )
    db_session.commit()

    records = []
    files = {"ignored_example.py": b"# An inert archive member"}
    for index in range(10):
        host = f"studio-{index}.test"
        pdf = f"sample-{index % 4}.pdf"
        files[pdf] = build_pdf_bytes(["Fictional Applicant", pdf, "Facilities Coordinator", "SQL and Excel"])
        records.append({
            "company": f"Studio {index}",
            "role": "Facilities Coordinator",
            "location": "Doha, Qatar" if index == 6 else "Dubai, UAE",
            "to": f"jobs@{host}",
            "subject": "Facilities Coordinator",
            "source_url": f"https://{host}/jobs/coordinator",
            "attachments": [pdf],
            "body": "Please consider my application for the advertised position.",
        })
    files["queue.json"] = json.dumps(records).encode()
    zip_bytes = _build_zip_bytes(files)

    def _mock_source(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host in {"studio-0.test", "studio-1.test"}:
            body = f"<h1>Facilities Coordinator</h1><p>Dubai UAE. Send your CV to jobs@{host}.</p>"
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404)

    mock_tx = httpx.MockTransport(_mock_source)

    # Mock SSRF DNS resolution for deterministic unit test
    import app.core.ssrf as ssrf_mod

    orig_getaddrinfo = ssrf_mod.socket.getaddrinfo
    ssrf_mod.socket.getaddrinfo = lambda host, port, *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 0))]
    try:
        res = import_gemini_bundle_zip(
            db_session,
            user=user,
            zip_bytes=zip_bytes,
            original_filename="sample-applications.zip",
            skip_network=True,
            transport_override=mock_tx,
        )
    finally:
        ssrf_mod.socket.getaddrinfo = orig_getaddrinfo

    assert res["duplicate_import"] is False
    assert res["created_applications"] == 10
    batch: Batch = res["batch"]
    assert batch.import_origin == "gemini_zip"
    assert batch.strategy_used == "gemini_zip_import"
    # Executable send_fresh_batch.py must be recorded as ignored and never executed
    ignored_names = {f["filename"] for f in res["ignored_files"]}
    assert "ignored_example.py" in ignored_names

    apps = db_session.scalars(
        select(Application).where(Application.batch_id == batch.id).order_by(Application.created_at.asc())
    ).all()
    assert len(apps) == 10
    by_to = {a.recipient_normalized: a for a in apps}

    # Exact 4 distinct PDF CV hashes ingested/linked
    distinct_pdf_hashes = {a.attachment_pdf_sha256 for a in apps}
    assert len(distinct_pdf_hashes) == 4
    assert all(a.selected_cv_version_id for a in apps)
    assert all(a.import_origin == "gemini_zip" for a in apps)

    for index in (0, 1):
        item = by_to[f"jobs@studio-{index}.test"]
        assert item.approval_state == "ready_for_approval", item.review_reasons_json
        assert item.source_verification_tier == "tier_1_official"
    for index in (2, 3, 4, 5):
        assert by_to[f"jobs@studio-{index}.test"].approval_state == "needs_human_review"
    for index, code in ((6, "OUTSIDE_TARGET_COUNTRY"), (7, "PREVIOUSLY_CONTACTED")):
        item = by_to[f"jobs@studio-{index}.test"]
        assert item.approval_state == "rejected"
        assert item.transport_status == "skipped"
        assert item.primary_skip_code == code

    # Re-importing the identical ZIP must NOT create duplicate sendable entities
    res2 = import_gemini_bundle_zip(
        db_session,
        user=user,
        zip_bytes=zip_bytes,
        original_filename="sample-applications.zip",
        skip_network=True,
    )
    assert res2["duplicate_import"] is True
    assert res2["created_applications"] == 0
    assert res2["batch"].id == batch.id
    assert len(db_session.scalars(select(Application).where(Application.user_id == user.id)).all()) == 10

    # Blocked item (Doha Qatar mandatory country mismatch) CANNOT be approved even with override
    ok_blk, err_blk = approve_application_for_send(
        db_session,
        user=user,
        app_rec=by_to["jobs@studio-6.test"],
        review_notes="Trying to override blocked country",
        allow_third_party_override=True,
    )
    assert ok_blk is False
    assert "cannot be approved" in err_blk.lower() or "blocked" in err_blk.lower()

    # Approve ready item (Studio Zero), then verify changing CV, body, or recipient invalidates approval
    review_item = by_to["jobs@studio-0.test"]
    ok_app, _ = approve_application_for_send(db_session, user=user, app_rec=review_item, review_notes="Verified official")
    assert ok_app is True
    db_session.commit()
    assert review_item.approval_state == "approved"
    old_hash = review_item.approved_content_hash
    assert old_hash == application_draft_hash(review_item)

    # Change CV version -> MUST invalidate approval
    other_cv_ver = db_session.scalar(
        select(CvVersion).where(CvVersion.user_id == user.id, CvVersion.id != review_item.selected_cv_version_id)
    )
    assert other_cv_ver is not None
    r_rev_cv = client.post(
        f"/api/v1/drafts/{review_item.id}/revise",
        json={"selected_cv_version_id": other_cv_ver.id},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_rev_cv.status_code == 200, r_rev_cv.text
    assert r_rev_cv.json()["approval_invalidated"] is True
    assert r_rev_cv.json()["draft"]["approval_state"] != "approved"
    assert r_rev_cv.json()["draft"]["selected_cv_version_id"] == other_cv_ver.id

    stale = client.post("/api/v1/drafts/approve", json={
        "application_ids": [review_item.id], "expected_hashes": {review_item.id: old_hash},
    }, headers={"X-CSRF-Token": csrf})
    assert not stale.json()["approved_ids"] and stale.json()["failed"]

    # Re-approve, then edit body -> MUST invalidate approval
    r_reapp = client.post(
        "/api/v1/drafts/approve",
        json={"application_ids": [review_item.id], "expected_hashes": {review_item.id: r_rev_cv.json()["draft"]["draft_content_hash"]}},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_reapp.status_code == 200 and review_item.id in r_reapp.json()["approved_ids"]
    r_rev_body = client.post(
        f"/api/v1/drafts/{review_item.id}/revise",
        json={"body_text": review_item.body_text + "\n\nUpdated availability details for Dubai interview."},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_rev_body.status_code == 200 and r_rev_body.json()["approval_invalidated"] is True

    # Re-approve, then edit recipient -> MUST invalidate approval
    r_reapp2 = client.post(
        "/api/v1/drafts/approve",
        json={"application_ids": [review_item.id], "expected_hashes": {review_item.id: r_rev_body.json()["draft"]["draft_content_hash"]}},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_reapp2.status_code == 200 and review_item.id in r_reapp2.json()["approved_ids"]
    r_rev_to = client.post(
        f"/api/v1/drafts/{review_item.id}/revise",
        json={"recipient_email": "review@studio-0.test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_rev_to.status_code == 200 and r_rev_to.json()["approval_invalidated"] is True

    # GET /batches/{batch_id} returns all 10 items + workflow_counts
    b_detail = client.get(f"/api/v1/batches/{batch.id}")
    assert b_detail.status_code == 200
    assert len(b_detail.json()["all_applications"]) == 10
    assert b_detail.json()["workflow_counts"]["imported"] == 10
    assert b_detail.json()["workflow_counts"]["duplicate"] == 1
    assert b_detail.json()["workflow_counts"]["blocked"] == 1


def test_gate_a_malicious_and_malformed_zip_rejections() -> None:
    """Reject malformed JSON, missing PDF, path traversal, ZIP bomb, unsupported attachment, and SHA-256 mismatch."""
    valid_pdf = build_pdf_bytes(["Candidate CV", "Data Analyst SQL Python"])
    valid_sha = compute_sha256(valid_pdf)
    base_rec = {
        "company": "Acme Gulf",
        "role": "Data Analyst",
        "location": "Dubai, UAE",
        "to": "careers@sample-ec069c.test",
        "subject": "Application for Data Analyst",
        "source_url": "https://sample-ec069c.test/careers",
        "attachments": ["cv.pdf"],
        "body": "Dear Hiring Team, please find my CV attached for the Data Analyst position.",
    }

    # 1. Malformed JSON
    bad_json_zip = _build_zip_bytes({"email_queue_fresh_10.json": b"{bad json", "cv.pdf": valid_pdf})
    try:
        parse_untrusted_gemini_zip(bad_json_zip)
        raise AssertionError("Expected MALFORMED_MANIFEST")
    except BundleImportError as exc:
        assert exc.code == "MALFORMED_MANIFEST"

    # 2. Missing PDF referenced by manifest
    missing_pdf_zip = _build_zip_bytes({"email_queue_fresh_10.json": json.dumps([base_rec]).encode("utf-8")})
    try:
        parse_untrusted_gemini_zip(missing_pdf_zip)
        raise AssertionError("Expected MISSING_ATTACHMENT")
    except BundleImportError as exc:
        assert exc.code == "MISSING_ATTACHMENT"

    # 3. Path traversal entry in ZIP
    traversal_zip = _build_zip_bytes(
        {
            "../evil.txt": b"pwn",
            "email_queue_fresh_10.json": json.dumps([base_rec]).encode("utf-8"),
            "cv.pdf": valid_pdf,
        }
    )
    try:
        parse_untrusted_gemini_zip(traversal_zip)
        raise AssertionError("Expected UNSAFE_ARCHIVE_PATH")
    except BundleImportError as exc:
        assert exc.code == "UNSAFE_ARCHIVE_PATH"

    # 4. Unsupported attachment type (.py / .docx referenced in attachments)
    bad_att_rec = {**base_rec, "attachments": ["send_fresh_batch.py"]}
    bad_att_zip = _build_zip_bytes(
        {"email_queue_fresh_10.json": json.dumps([bad_att_rec]).encode("utf-8"), "send_fresh_batch.py": b"print(1)"}
    )
    try:
        parse_untrusted_gemini_zip(bad_att_zip)
        raise AssertionError("Expected UNSUPPORTED_ATTACHMENT")
    except BundleImportError as exc:
        assert exc.code == "UNSUPPORTED_ATTACHMENT"

    # 5. Fake PDF (non-%PDF- bytes disguised as .pdf)
    fake_pdf_zip = _build_zip_bytes(
        {"email_queue_fresh_10.json": json.dumps([base_rec]).encode("utf-8"), "cv.pdf": b"NOT_A_PDF_SCRIPT"}
    )
    try:
        parse_untrusted_gemini_zip(fake_pdf_zip)
        raise AssertionError("Expected INVALID_PDF_ATTACHMENT")
    except BundleImportError as exc:
        assert exc.code == "INVALID_PDF_ATTACHMENT"

    # 6. Mismatched expected SHA-256 in versioned manifest
    hash_mismatch_rec = {**base_rec, "attachment_sha256": {"cv.pdf": "0" * 64}}
    mismatch_zip = _build_zip_bytes(
        {"email_queue_fresh_10.json": json.dumps([hash_mismatch_rec]).encode("utf-8"), "cv.pdf": valid_pdf}
    )
    try:
        parse_untrusted_gemini_zip(mismatch_zip)
        raise AssertionError("Expected ATTACHMENT_HASH_MISMATCH")
    except BundleImportError as exc:
        assert exc.code == "ATTACHMENT_HASH_MISMATCH"
    assert valid_sha != "0" * 64

    # 7. ZIP bomb compression ratio rejection
    bomb_data = b"A" * (5 * 1024 * 1024)
    bomb_zip = _build_zip_bytes(
        {"email_queue_fresh_10.json": json.dumps([base_rec]).encode("utf-8"), "cv.pdf": valid_pdf, "bomb.txt": bomb_data}
    )
    try:
        parse_untrusted_gemini_zip(bomb_zip)
        raise AssertionError("Expected ZIP_BOMB_DETECTED")
    except BundleImportError as exc:
        assert exc.code == "ZIP_BOMB_DETECTED"


def test_gate_b_fake_transport_outbox_crash_recovery_retry_tenant_and_self_test(client: TestClient, db_session, monkeypatch) -> None:
    """Gate B: Verify fake-transport MIME + attachment checksum, idempotency, uncertain crash recovery,
    safe retry-send, strict per-user isolation, and owner-controlled self-test email lock.
    """
    user, csrf, conn = _seed_owner_user(client, db_session, email="gate-b-owner@sample-aae466.example.org")

    # Seed second tenant to verify strict per-user isolation
    other_client = TestClient(client.app)
    r_other = other_client.post(
        "/api/v1/auth/register",
        json={"email": "other-tenant@sample-aae466.example.org", "password": "OtherPassword123!", "full_name": "Other User"},
    )
    assert r_other.status_code == 201
    other_csrf = r_other.json()["user"]["csrf_token"]

    pdf_bytes = build_pdf_bytes(["Test Owner", "CAFM Specialist Dubai UAE", "Skills: Maximo, Power BI, SQL"])
    pdf_sha = compute_sha256(pdf_bytes)
    mini_zip = _build_zip_bytes(
        {
            "email_queue_fresh_10.json": json.dumps(
                [
                    {
                        "company": "Cloudlink Dubai",
                        "role": "Data & Reporting Specialist",
                        "location": "Dubai, UAE",
                        "to": "jobs@studio-1.test",
                        "subject": "Application - Data & Reporting Specialist",
                        "source_url": "https://fictional-studio.test/jobs/sample-88776d50",
                        "attachments": ["Owner_CV.pdf"],
                        "body": "Dear Cloudlink Team, please find my attached PDF CV for your consideration in Dubai.",
                    }
                ]
            ).encode("utf-8"),
            "Owner_CV.pdf": pdf_bytes,
        }
    )

    res = import_gemini_bundle_zip(db_session, user=user, zip_bytes=mini_zip, skip_network=True)
    batch = res["batch"]
    app_rec = db_session.scalar(select(Application).where(Application.batch_id == batch.id))
    assert app_rec is not None

    # Tenant isolation: other user cannot inspect, revise, approve, or send user's application
    assert other_client.get(f"/api/v1/batches/{batch.id}").status_code == 404
    assert other_client.post(
        f"/api/v1/drafts/{app_rec.id}/revise",
        json={"subject": "Hijack"},
        headers={"X-CSRF-Token": other_csrf},
    ).status_code == 404
    assert other_client.post(
        f"/api/v1/mailboxes/{conn.id}/send-test",
        json={"explicit_confirm": True},
        headers={"X-CSRF-Token": other_csrf},
    ).status_code == 404

    # Approve application with explicit review note (tier_3 since skip_network=True)
    ok_app, msg_app = approve_application_for_send(
        db_session,
        user=user,
        app_rec=app_rec,
        review_notes="Verified Cloudlink corporate careers page",
        allow_third_party_override=True,
    )
    assert ok_app is True, msg_app
    db_session.commit()

    # Live sending OFF -> enqueue MUST fail closed at backend boundary
    batch.mode = "live"
    db_session.commit()
    ob_blocked, err_blocked = enqueue_application_for_send(db_session, user=user, batch=batch, app_record=app_rec, conn=conn)
    assert ob_blocked is None
    assert "LIVE_SENDING_DISABLED" in err_blocked

    # Owner self-test email works with LIVE_SENDING_ENABLED=false, rejects non-owner recipients, requires explicit_confirm
    r_no_confirm = client.post(
        f"/api/v1/mailboxes/{conn.id}/send-test",
        json={"explicit_confirm": False, "cv_version_id": app_rec.selected_cv_version_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_no_confirm.status_code == 400 and r_no_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    r_third_party = client.post(
        f"/api/v1/mailboxes/{conn.id}/send-test",
        json={"explicit_confirm": True, "recipient_email": "jobs@studio-1.test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_third_party.status_code == 400 and "SELF_TEST_ONLY" in r_third_party.json()["detail"]["message"]

    fake_self_tx = FakeTransport(outcome=HandoffOutcome.PROVIDER_ACCEPTED, reconcile_hit=True)
    self_res = send_owner_self_test_email(
        db_session,
        user=user,
        conn=conn,
        recipient_email=user.email,
        cv_version_id=app_rec.selected_cv_version_id,
        transport_override=fake_self_tx,
    )
    assert self_res["ok"] is True
    assert self_res["recipient_email"] == user.email
    assert self_res["attachment_sha256"] == pdf_sha
    assert self_res["reconciled_in_sent"] is True
    assert len(fake_self_tx.sent_messages) == 1
    assert fake_self_tx.sent_messages[0].attachments[0].expected_sha256 == pdf_sha

    # Now simulate explicit test-only activation of live sending for fake-transport outbox & crash recovery tests
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    try:
        # 1. Enqueue + verify atomic idempotency (second enqueue blocked by reserved recipient)
        entry, enq_err = enqueue_application_for_send(db_session, user=user, batch=batch, app_record=app_rec, conn=conn)
        assert entry is not None, enq_err
        dup_entry, dup_err = enqueue_application_for_send(db_session, user=user, batch=batch, app_record=app_rec, conn=conn)
        assert dup_entry is None and "already" in dup_err.lower()

        # 1b. Simulate pre-acceptance provider rejection (HandoffOutcome.PROVIDER_REJECTED)
        # -> marks failed, releases reservation, and allows POST /applications/{id}/retry-send to succeed
        rejected_tx = FakeTransport(outcome=HandoffOutcome.PROVIDER_REJECTED, reconcile_hit=False)
        claimed_rej = claim_next_outbox_entry(db_session, worker_id="w-0", outbox_id=entry.id)
        assert claimed_rej is not None
        out_rej = process_outbox_entry(db_session, entry=claimed_rej, transport_override=rejected_tx, ignore_window_for_test=True)
        assert out_rej == "failed"
        db_session.refresh(app_rec)
        assert app_rec.transport_status == "failed"

        # 2. Retry via POST /applications/{id}/retry-send with FakeTransport(AMBIGUOUS_HANDOFF)
        # -> succeeds in re-enqueuing and marks send_uncertain on post-handoff timeout
        uncertain_tx = FakeTransport(outcome=HandoffOutcome.AMBIGUOUS_HANDOFF, reconcile_hit=False)
        monkeypatch.setattr("app.services.outbox.get_mailbox_transport", lambda _db, _conn: uncertain_tx)
        r_retry_ok = client.post(f"/api/v1/applications/{app_rec.id}/retry-send", headers={"X-CSRF-Token": csrf})
        assert r_retry_ok.status_code == 200, r_retry_ok.text
        retry_payload = r_retry_ok.json()
        retry_outbox_id = retry_payload["outbox_entry_id"]
        assert retry_outbox_id != entry.id
        assert retry_payload["outcome"] == "queued" and not uncertain_tx.sent_messages
        claimed = claim_next_outbox_entry(db_session, worker_id="retry-worker", outbox_id=retry_outbox_id)
        assert process_outbox_entry(db_session, entry=claimed, transport_override=uncertain_tx, ignore_window_for_test=True) == "send_uncertain"
        assert claimed is not None
        db_session.refresh(app_rec)
        assert app_rec.transport_status == "send_uncertain"
        assert len(uncertain_tx.sent_messages) == 1
        assert uncertain_tx.sent_messages[0].attachments[0].filename == "Owner_CV.pdf"
        assert uncertain_tx.sent_messages[0].attachments[0].expected_sha256 == pdf_sha

        # A missing Sent-folder result cannot unlock a second attempt, including legacy clients.
        lookup = client.post(f"/api/v1/applications/{app_rec.id}/reconcile-outbox",
                             json={"mark_failed_if_not_found": True}, headers={"X-CSRF-Token": csrf})
        assert lookup.status_code == 200
        assert lookup.json()["reconciled_outcome"] == "still_uncertain"
        assert len(uncertain_tx.sent_messages) == 1

        # Retry endpoint MUST refuse send_uncertain (never blindly replay!)
        r_retry_unc = client.post(f"/api/v1/applications/{app_rec.id}/retry-send", headers={"X-CSRF-Token": csrf})
        assert r_retry_unc.status_code == 409 and r_retry_unc.json()["detail"]["code"] == "RETRY_NOT_ALLOWED"

        # 3. Crash recovery reconciliation of a stale sending entry
        claimed.status = "sending"
        claimed.lease_expires_at = utcnow() - timedelta(minutes=10)
        db_session.commit()
        reconcile_tx = FakeTransport(outcome=HandoffOutcome.PROVIDER_ACCEPTED, reconcile_hit=True)
        rec_stats = recover_stale_outbox_leases(db_session, transport_factory=lambda _db, _conn: reconcile_tx)
        assert rec_stats["reconciled_sent"] == 1
        db_session.refresh(app_rec)
        assert app_rec.transport_status == "sent"
        assert app_rec.provider_message_id == "reconciled-sent-1"

        # Retry endpoint MUST refuse already accepted/sent applications
        r_retry_sent = client.post(f"/api/v1/applications/{app_rec.id}/retry-send", headers={"X-CSRF-Token": csrf})
        assert r_retry_sent.status_code == 409
    finally:
        monkeypatch.delenv("LIVE_SENDING_ENABLED", raising=False)
        get_settings.cache_clear()

    # Confirm default remains False again
    assert resolve_effective_live_sending(db_session)[0] is False
    # Confirm self-test audit event was logged
    audit_actions = set(db_session.scalars(select(AuditEvent.action).where(AuditEvent.user_id == user.id)).all())
    assert "mailbox_self_test_sent" in audit_actions
    assert "gemini_bundle_imported" in audit_actions


def test_gmail_oauth_redirect_uri_and_callback_flow(client: TestClient, db_session, monkeypatch) -> None:
    """Verify externally reachable Docker callback URL (http://localhost/api/v1/mailboxes/gmail/callback),
    PKCE state verification, token exchange, single-use state consumption, and callback session binding.
    """
    from urllib.parse import parse_qs, urlparse

    from app.services.effective_config import (
        get_or_create_installation_config,
        resolve_effective_google_oauth,
        update_encrypted_secrets,
    )

    user, _csrf, _conn = _seed_owner_user(client, db_session, email="oauth-owner@sample-aae466.example.org")
    inst = get_or_create_installation_config(db_session)
    inst.owner_user_id = user.id
    inst.google_oauth_client_id = "968664633649-testclient.apps.googleusercontent.com"
    inst.encrypted_secrets_json = update_encrypted_secrets(
        inst.encrypted_secrets_json,
        {"google_oauth_client_secret": "synthetic-credential-cdfb361a"},
    )
    db_session.commit()

    monkeypatch.setenv("APP_URL", "http://localhost")
    monkeypatch.setenv("API_PUBLIC_URL", "http://localhost")
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI_GMAIL", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI_AUTH", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI_SHEETS", raising=False)
    get_settings.cache_clear()
    try:
        eff_g = resolve_effective_google_oauth(db_session)
        assert eff_g.redirect_uri_gmail == "http://localhost/api/v1/mailboxes/gmail/callback"
        assert eff_g.redirect_uri_auth == "http://localhost/api/v1/auth/google/callback"
        assert eff_g.redirect_uri_sheets == "http://localhost/api/v1/sheets/callback"

        # Even if a legacy :8000 env var were present while API_PUBLIC_URL=http://localhost, it normalizes to :80
        monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI_GMAIL", "http://localhost:8000/api/v1/mailboxes/gmail/callback")
        get_settings.cache_clear()
        eff_g_legacy = resolve_effective_google_oauth(db_session)
        assert eff_g_legacy.redirect_uri_gmail == "http://localhost/api/v1/mailboxes/gmail/callback"

        # 1. Start Gmail OAuth consent flow
        r_start = client.get("/api/v1/mailboxes/gmail/start?include_inbox_readonly=true&include_compose_drafts=true")
        assert r_start.status_code == 200, r_start.text
        auth_url = r_start.json()["authorization_url"]
        parsed = urlparse(auth_url)
        qs = parse_qs(parsed.query)
        assert qs["redirect_uri"] == ["http://localhost/api/v1/mailboxes/gmail/callback"]
        assert qs["code_challenge_method"] == ["S256"]
        state = qs["state"][0]

        # 2. Mock Google OAuth token & userinfo endpoints
        exchanged_payloads: list[dict[str, str]] = []

        def _mock_google_oauth_handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://oauth2.googleapis.com/token":
                form = parse_qs(request.content.decode("utf-8"))
                exchanged_payloads.append({k: v[0] for k, v in form.items()})
                assert form["redirect_uri"] == ["http://localhost/api/v1/mailboxes/gmail/callback"]
                assert form["grant_type"] == ["authorization_code"]
                assert len(form.get("code_verifier", [""])[0]) >= 32
                return httpx.Response(
                    200,
                    json={
                        "access_token": "synthetic-credential-3990ba88",
                        "refresh_token": "1//mock-refresh-token",
                        "expires_in": 3600,
                        "scope": (
                            "https://www.googleapis.com/auth/userinfo.email "
                            "https://www.googleapis.com/auth/gmail.send "
                            "https://www.googleapis.com/auth/gmail.readonly "
                            "https://www.googleapis.com/auth/gmail.compose"
                        ),
                        "token_type": "Bearer",
                    },
                )
            if str(request.url) == "https://openidconnect.googleapis.com/v1/userinfo":
                return httpx.Response(200, json={"sub": "fixture-sub", "email": user.email, "email_verified": True})
            return httpx.Response(404)

        mock_tx = httpx.MockTransport(_mock_google_oauth_handler)
        orig_complete = __import__("app.services.mailbox.gmail", fromlist=["complete_gmail_oauth_callback"]).complete_gmail_oauth_callback
        monkeypatch.setattr(
            "app.api.routes_automation.complete_gmail_oauth_callback",
            lambda db, *, user_id=None, code, state: orig_complete(
                db, user_id=user_id, code=code, state=state, http_transport=mock_tx
            ),
        )

        # 3. A missing session cannot claim the authorization. The initiating user can still finish.
        anon_client = TestClient(client.app)
        anonymous = anon_client.get(
            f"/api/v1/mailboxes/gmail/callback?code=unused&state={state}", follow_redirects=False,
        )
        assert anonymous.status_code == 401
        assert not exchanged_payloads
        r_cb = client.get(
            f"/api/v1/mailboxes/gmail/callback?code=4/0AeanS0MockOneTimeCode&state={state}",
            follow_redirects=False,
        )
        assert r_cb.status_code == 302, r_cb.text
        assert r_cb.headers["location"] == "http://localhost/settings?mailbox=gmail_connected"
        assert len(exchanged_payloads) == 1

        # 4. Reusing the same authorization code / state MUST fail closed (single-use state token)
        r_replay = client.get(
            f"/api/v1/mailboxes/gmail/callback?code=4/0AeanS0MockOneTimeCode&state={state}",
            follow_redirects=False,
        )
        assert r_replay.status_code == 400
        assert r_replay.json()["detail"]["code"] == "INVALID_OAUTH_STATE"

        # 5. Verify GET /api/v1/mailboxes reports the connected gmail_oauth primary mailbox
        r_mb = client.get("/api/v1/mailboxes")
        assert r_mb.status_code == 200
        gmail_mbs = [m for m in r_mb.json()["mailboxes"] if m["provider"] == "gmail_oauth"]
        assert len(gmail_mbs) == 1
        assert gmail_mbs[0]["status"] == "connected"
        assert gmail_mbs[0]["is_primary"] is True
        assert gmail_mbs[0]["save_drafts_in_gmail"] is True
    finally:
        monkeypatch.delenv("APP_URL", raising=False)
        monkeypatch.delenv("API_PUBLIC_URL", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI_GMAIL", raising=False)
        get_settings.cache_clear()


def test_outbox_queue_sync_cancel_reconcile_and_quiet_hours_lifecycle(client: TestClient, db_session, monkeypatch) -> None:
    """Verify full outbox lifecycle:
    Approved Application -> Outbox Entry (transport_status='queued', queue_state='queued') ->
    Quiet-hours deferral synchronization -> Safe cancellation (cancel-outbox) -> Re-enqueue ->
    Worker Processing -> Mocked Gmail Provider Response -> Application Tracker Update,
    plus uncertain send reconciliation via /reconcile-outbox.
    """
    user, csrf, conn = _seed_owner_user(client, db_session, email="outbox-flow-owner@sample-aae466.example.org")
    pdf_bytes = build_pdf_bytes(["Test Owner", "CAFM Specialist Dubai UAE", "Skills: Maximo, Power BI, SQL"])
    mini_zip = _build_zip_bytes(
        {
            "email_queue_fresh_10.json": json.dumps(
                [
                    {
                        "company": "Studio Zero",
                        "role": "Property Operations & CAFM",
                        "location": "Sharjah, UAE",
                        "to": "jobs@studio-0.test",
                        "subject": "Application - Property Operations & CAFM",
                        "source_url": "https://www.review_item.ae/careers",
                        "attachments": ["Owner_CV.pdf"],
                        "body": "Dear Studio Zero Team, please find my attached PDF CV for your consideration.",
                    }
                ]
            ).encode("utf-8"),
            "Owner_CV.pdf": pdf_bytes,
        }
    )
    res = import_gemini_bundle_zip(db_session, user=user, zip_bytes=mini_zip, skip_network=True)
    batch = res["batch"]
    app_rec = db_session.scalar(select(Application).where(Application.batch_id == batch.id))
    assert app_rec is not None

    ok_app, msg_app = approve_application_for_send(
        db_session,
        user=user,
        app_rec=app_rec,
        review_notes="Approved for outbox lifecycle test",
        allow_third_party_override=True,
    )
    assert ok_app is True, msg_app
    batch.mode = "live"
    db_session.commit()

    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    try:
        # 1. Enqueue sets application transport_status='queued' immediately (never leaves stale 'ready')
        entry, enq_err = enqueue_application_for_send(db_session, user=user, batch=batch, app_record=app_rec, conn=conn)
        assert entry is not None, enq_err
        db_session.refresh(app_rec)
        assert app_rec.transport_status == "queued"

        # 2. Simulate quiet-hours deferral during worker processing (ignore_window_for_test=False)
        monkeypatch.setattr(
            "app.services.outbox.is_in_quiet_hours_or_outside_window",
            lambda **_kw: (True, "Paused for quiet hours (22:00-07:00 Asia/Dubai)"),
        )
        claimed_q = claim_next_outbox_entry(db_session, worker_id="w-quiet", outbox_id=entry.id)
        assert claimed_q is not None
        out_q = process_outbox_entry(db_session, entry=claimed_q, ignore_window_for_test=False)
        assert out_q == "deferred_window"
        db_session.refresh(app_rec)
        db_session.refresh(entry)
        assert entry.status == "queued"
        assert app_rec.transport_status == "queued"
        assert app_rec.skip_or_error_reason == "Paused for quiet hours (22:00-07:00 Asia/Dubai)"

        # Verify Review Queue & Tracker endpoints expose synchronized queue_state='queued' and outbox metadata
        rq = client.get(f"/api/v1/review-queue?batch_id={batch.id}&include_all=true").json()
        assert rq["workflow_counts"]["queued"] == 1
        assert rq["items"][0]["transport_status"] == "queued"
        assert rq["items"][0]["queue_state"] == "queued"
        assert rq["items"][0]["outbox_status"] == "queued"
        assert "Paused for quiet hours" in rq["items"][0]["outbox_last_error"]

        tr = client.get("/api/v1/applications?transport_status=queued").json()
        assert tr["total"] == 1
        assert tr["applications"][0]["queue_state"] == "queued"

        # 3. Safe cancellation of genuinely unsent queued entry releases reservation and returns to 'approved'/'draft'
        r_cancel = client.post(
            f"/api/v1/applications/{app_rec.id}/cancel-outbox",
            json={"reason": "Cancel quiet-hours entry for test"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r_cancel.status_code == 200, r_cancel.text
        assert r_cancel.json()["draft"]["transport_status"] == "draft"
        assert r_cancel.json()["draft"]["queue_state"] == "approved"

        # Calling cancel-outbox a second time is idempotent
        r_cancel_2 = client.post(
            f"/api/v1/applications/{app_rec.id}/cancel-outbox",
            json={"reason": "Idempotent second click"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r_cancel_2.status_code == 200 and r_cancel_2.json()["already_cancelled"] is True

        # 4. Re-enqueue after cancellation succeeds cleanly and worker sends via mocked Gmail transport
        entry2, enq_err2 = enqueue_application_for_send(db_session, user=user, batch=batch, app_record=app_rec, conn=conn)
        assert entry2 is not None, enq_err2
        accept_tx = FakeTransport(outcome=HandoffOutcome.PROVIDER_ACCEPTED, reconcile_hit=True)
        claimed_send = claim_next_outbox_entry(db_session, worker_id="w-send", outbox_id=entry2.id)
        assert claimed_send is not None
        # Another tab confirms while a worker owns the lease. It must not dispatch or reset that claim.
        lease_token = claimed_send.lease_owner
        other_tab = client.post("/api/v1/drafts/send-approved",
                                json={"application_ids": [app_rec.id], "expected_hashes": {app_rec.id: application_draft_hash(app_rec)}, "expected_mailbox_id": conn.id, "acknowledge_incomplete_history": True, "dispatch_immediately": True},
                                headers={"X-CSRF-Token": csrf})
        assert other_tab.status_code == 200
        assert other_tab.json()["queued_ids"] == [app_rec.id]
        db_session.refresh(claimed_send)
        assert (claimed_send.status, claimed_send.lease_owner) == ("leased", lease_token)
        out_sent = process_outbox_entry(db_session, entry=claimed_send, transport_override=accept_tx, ignore_window_for_test=True)
        assert out_sent == "sent"
        db_session.refresh(app_rec)
        assert app_rec.transport_status == "sent"
        assert app_rec.skip_or_error_reason == ""
        assert app_rec.provider_message_id == "fake-msg-1"

        # Cancel-outbox MUST refuse an already-sent application
        r_cancel_sent = client.post(
            f"/api/v1/applications/{app_rec.id}/cancel-outbox",
            headers={"X-CSRF-Token": csrf},
        )
        assert r_cancel_sent.status_code == 409
    finally:
        monkeypatch.delenv("LIVE_SENDING_ENABLED", raising=False)
        get_settings.cache_clear()


def test_simplified_one_click_approval_and_immediate_mock_smtp_send_all_approved(
    client: TestClient, db_session, monkeypatch
) -> None:
    """Verify:
    1. Clicking Approve (POST /api/v1/drafts/approve with review_notes='' and allow_third_party_override=False)
       immediately approves both tier_1 and tier_3_third_party_claim / needs_human_review applications without
       requiring a reason, recording user.id, user.email, application_id, and approved_at.
    2. Approved counter in /api/v1/review-queue updates immediately.
    3. Clicking Send All Approved (POST /api/v1/drafts/send-approved) reuses any existing unsent queued
       OutboxEntry without creating duplicate entries, and the worker dispatches through the connected Gmail
       SMTP transport (mocked smtplib.SMTP server), recording sent_at and provider_message_id.
    4. Already-sent or uncertain applications are blocked from duplicate sends.
    5. Bulk /api/v1/outbox/cancel-queued cancels unsent queued entries cleanly.
    6. Unlocking Live Sending resets old pending unsent queued OutboxEntries to ready_for_approval so old pending
       applications are never automatically released without fresh approval.
    """

    from app.core.encryption import encrypt_json
    from app.db.models import ApplicationTransition
    from app.services.effective_config import get_or_create_installation_config
    from app.services.setup_service import set_installation_live_sending

    user, csrf, conn = _seed_owner_user(client, db_session, email="oneclick-smtp-owner@sample-aae466.example.org")
    conn.provider = "smtp_imap"
    conn.smtp_host = "smtp.gmail.com"
    conn.smtp_port = 587
    conn.smtp_use_tls = True
    conn.imap_host = "imap.gmail.com"
    conn.imap_port = 993
    conn.encrypted_credentials = encrypt_json({"username": user.email, "password": "mock-app-password"})
    db_session.commit()
    pdf_bytes = build_pdf_bytes(["Test Owner", "CAFM Specialist Dubai UAE", "Skills: Maximo, Power BI, SQL"])
    pdf_sha = compute_sha256(pdf_bytes)
    mini_zip = _build_zip_bytes(
        {
            "email_queue_fresh_10.json": json.dumps(
                [
                    {
                        "company": "Operon UAE",
                        "role": "CAFM Coordinator",
                        "location": "Dubai, UAE",
                        "to": "jobs@studio-2.test",
                        "subject": "Application for CAFM Coordinator",
                        "source_url": "https://fictional-studio.test/jobs/sample-0ddcb922",
                        "attachments": ["Owner_CV.pdf"],
                        "body": "Dear Operon Team, please find my attached PDF CV for the CAFM Coordinator position.",
                    },
                    {
                        "company": "Cloudlink UAE",
                        "role": "MIS Data Analyst",
                        "location": "Dubai, UAE",
                        "to": "jobs@studio-1.test",
                        "subject": "Application for MIS Data Analyst",
                        "source_url": "https://fictional-studio.test/jobs/sample-88776d50",
                        "attachments": ["Owner_CV.pdf"],
                        "body": "Dear Cloudlink Team, please find my attached PDF CV for the MIS Data Analyst role.",
                    },
                ]
            ).encode("utf-8"),
            "Owner_CV.pdf": pdf_bytes,
        }
    )

    res = import_gemini_bundle_zip(db_session, user=user, zip_bytes=mini_zip, skip_network=True)
    batch = res["batch"]
    batch.mode = "live"
    db_session.commit()

    apps = db_session.scalars(
        select(Application).where(Application.batch_id == batch.id).order_by(Application.recipient_normalized.asc())
    ).all()
    assert len(apps) == 2
    # Both start in needs_human_review (tier_3_third_party_claim when skip_network=True)
    assert all(a.approval_state == "needs_human_review" for a in apps)
    assert all(a.source_verification_tier == "tier_3_third_party_claim" for a in apps)

    # 1. One-click Approve WITHOUT any reason or third-party override flag
    r_approve = client.post(
        "/api/v1/drafts/approve",
        json={
            "application_ids": [a.id for a in apps], "expected_hashes": {a.id: application_draft_hash(a) for a in apps},
            "review_notes": "",
            "allow_third_party_override": False,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r_approve.status_code == 200, r_approve.text
    ap_data = r_approve.json()
    assert set(ap_data["approved_ids"]) == {a.id for a in apps}
    assert ap_data["failed"] == []

    # Verify approval recorded user.id, user.email, application_id, and approved_at automatically
    for a in apps:
        db_session.refresh(a)
        assert a.approval_state == "approved"
        assert a.approved_at is not None
        assert a.approved_content_hash == application_draft_hash(a)
        tr = db_session.scalar(
            select(ApplicationTransition)
            .where(ApplicationTransition.application_id == a.id, ApplicationTransition.to_state == "approved")
            .order_by(ApplicationTransition.created_at.desc())
        )
        assert tr is not None
        assert tr.actor == f"user:{user.id}"
        assert tr.metadata_json.get("approved_by_user_id") == user.id
        assert tr.metadata_json.get("approved_by_email") == user.email
        assert tr.metadata_json.get("application_id") == a.id
        assert tr.metadata_json.get("approved_at") is not None

    # 2. Review queue counter immediately reflects 2 approved applications
    rq_after_ap = client.get(f"/api/v1/review-queue?batch_id={batch.id}&include_all=true").json()
    assert rq_after_ap["workflow_counts"]["approved"] == 2

    # 3. While Live Sending is locked, Send All Approved returns 409 LIVE_SENDING_DISABLED and creates 0 outbox entries
    r_locked = client.post(
        "/api/v1/drafts/send-approved",
        json={"application_ids": [a.id for a in apps]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_locked.status_code == 409
    assert r_locked.json()["detail"]["code"] == "LIVE_SENDING_DISABLED"
    assert db_session.scalars(select(OutboxEntry).where(OutboxEntry.user_id == user.id)).all() == []

    # 4. Pre-stage one application in OutboxEntry(status='queued') to verify that unlocking Live Sending
    # cancels old pending unsent queued entries and requires fresh approval before sending
    inst = get_or_create_installation_config(db_session)
    inst.owner_user_id = user.id
    inst.live_sending_enabled = True
    db_session.commit()

    try:
        app_cloudlink, app_operon = apps[0], apps[1]
        pre_entry, pre_err = enqueue_application_for_send(
            db_session, user=user, batch=batch, app_record=app_cloudlink, conn=conn
        )
        assert pre_entry is not None, pre_err
        assert pre_entry.status == "queued"

        # Lock installation live sending, then simulate unlocking Live Sending via set_installation_live_sending (False -> True)
        inst.live_sending_enabled = False
        db_session.commit()
        set_installation_live_sending(db_session, user=user, enabled=True)
        db_session.refresh(pre_entry)
        db_session.refresh(app_cloudlink)
        assert pre_entry.status == "cancelled"
        assert app_cloudlink.approval_state == "ready_for_approval"
        assert app_cloudlink.approved_content_hash == ""
        assert app_cloudlink.transport_status == "draft"

        # Re-approve app_cloudlink in one click (no reason)
        r_reap = client.post(
            "/api/v1/drafts/approve",
            json={"application_ids": [app_cloudlink.id], "expected_hashes": {app_cloudlink.id: application_draft_hash(app_cloudlink)}},
            headers={"X-CSRF-Token": csrf},
        )
        assert r_reap.status_code == 200 and r_reap.json()["approved_ids"] == [app_cloudlink.id]
        db_session.refresh(app_cloudlink)

        # Pre-enqueue app_cloudlink as a quiet-hours 'queued' outbox entry, then verify Send All Approved
        # reuses that exact outbox entry idempotently and dispatches both applications immediately via mock SMTP!
        queued_entry, q_err = enqueue_application_for_send(
            db_session, user=user, batch=batch, app_record=app_cloudlink, conn=conn
        )
        assert queued_entry is not None, q_err
        queued_entry.last_error = "Paused for quiet hours (22:00-07:00 Asia/Dubai)"
        app_cloudlink.transport_status = "queued"
        app_cloudlink.skip_or_error_reason = queued_entry.last_error
        db_session.commit()

        # Wire a mock SMTP server capturing starttls, login, and send_message
        smtp_sent_messages: list = []

        class _MockSmtpServer:
            def __init__(self, host: str, port: int, timeout: int = 20):
                self.host = host
                self.port = port
                self.timeout = timeout
                self.logged_in_user = ""

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def ehlo(self):
                return (250, b"smtp.gmail.com at your service")

            def starttls(self, context=None):
                return (220, b"2.0.0 Ready to start TLS")

            def login(self, user_name: str, password: str):
                self.logged_in_user = user_name
                return (235, b"2.7.0 Accepted")

            def send_message(self, msg):
                smtp_sent_messages.append(msg)
                return {}

        monkeypatch.setattr("app.core.mail_sockets.PublicSMTP", _MockSmtpServer)
        monkeypatch.setattr("app.services.mailbox.smtp_imap.validate_mail_host", lambda *_a, **_kw: None)
        # A legacy immediate request during quiet hours can only enqueue.
        monkeypatch.setattr(
            "app.services.outbox.is_in_quiet_hours_or_outside_window",
            lambda **_kw: (True, "Paused for quiet hours (22:00-07:00 Asia/Dubai)"),
        )

        r_send_all = client.post(
            "/api/v1/drafts/send-approved",
            json={"application_ids": [app_cloudlink.id, app_operon.id], "expected_hashes": {a.id: application_draft_hash(a) for a in apps}, "expected_mailbox_id": conn.id, "acknowledge_incomplete_history": True, "dispatch_immediately": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert r_send_all.status_code == 200, r_send_all.text
        send_payload = r_send_all.json()
        assert not send_payload["sent_ids"] and not smtp_sent_messages
        assert set(send_payload["queued_ids"]) == {app_cloudlink.id, app_operon.id}
        assert send_payload["failed_ids"] == []
        assert send_payload["blocked"] == []
        from app.tasks.pipeline import task_drain_outbox

        monkeypatch.setattr("app.services.outbox.is_in_quiet_hours_or_outside_window", lambda **_kw: (False, ""))
        assert task_drain_outbox.run() == 2
        assert len(smtp_sent_messages) == 2

        # Verify each MIME message transmitted via mock SMTP has the PDF CV attachment matching pdf_sha
        for mime_msg in smtp_sent_messages:
            pdf_parts = [
                part for part in mime_msg.walk() if part.get_content_type() == "application/pdf"
            ]
            assert len(pdf_parts) == 1
            assert pdf_parts[0].get_filename() == "Owner_CV.pdf"
            assert compute_sha256(pdf_parts[0].get_payload(decode=True)) == pdf_sha

        # Verify no duplicate OutboxEntry was created for app_cloudlink (only the 1 cancelled + 1 reused sent entry)
        db_session.expire_all()
        cloudlink_entries = db_session.scalars(
            select(OutboxEntry).where(OutboxEntry.application_id == app_cloudlink.id)
        ).all()
        non_cancelled = [e for e in cloudlink_entries if e.status != "cancelled"]
        assert len(non_cancelled) == 1
        assert non_cancelled[0].id == queued_entry.id
        assert non_cancelled[0].status == "sent"

        # Verify Tracker API returns sent_at and provider_message_id for both applications
        tr_sent = client.get("/api/v1/applications?transport_status=sent").json()
        assert tr_sent["total"] == 2
        for item in tr_sent["applications"]:
            assert item["transport_status"] == "sent"
            assert item["sent_at"] is not None
            assert item["provider_message_id"]
            assert item["rfc_message_id"]

        # Calling Send All Approved again on already-sent applications is blocked (duplicate prevention)
        r_dup_send = client.post(
            "/api/v1/drafts/send-approved",
            json={"application_ids": [app_cloudlink.id, app_operon.id], "expected_mailbox_id": conn.id, "acknowledge_incomplete_history": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert r_dup_send.status_code == 200
        assert r_dup_send.json()["sent_ids"] == []
        assert len(r_dup_send.json()["blocked"]) == 2
    finally:
        inst.live_sending_enabled = False
        db_session.commit()
        monkeypatch.delenv("LIVE_SENDING_ENABLED", raising=False)
        get_settings.cache_clear()




