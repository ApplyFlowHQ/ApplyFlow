"""Mailbox history, reply evidence and approval-bound follow-up delivery (mock providers only)."""
from datetime import timedelta

import httpx
import pytest

from app.core.config import get_settings
from app.core.encryption import encrypt_json
from app.db.base import utcnow
from app.db.models import Application, Batch, JobPosting, MailboxConnection
from app.services import followup
from app.services.inbox_processor import ingest_inbound_messages
from app.services.mailbox.base import InboundMessageRecord
from app.services.mailbox.gmail_sent import sync_gmail_sent_history
from app.services.outbox import claim_next_outbox_entry, process_outbox_entry
from tests.test_outbox_dedupe_inbox_followup_exports import RecordingTransport, _seed_live_user


@pytest.fixture
def sent(db_session):
    db = db_session
    user, conn, *_ = _seed_live_user(db)
    batch = Batch(user_id=user.id, mode="live", status="completed")
    db.add(batch)
    db.flush()
    post = JobPosting(user_id=user.id, batch_id=batch.id, canonical_url_hash="abc", content_hash="def",
                      source_provider="mock", source_url="https://hiring.test/jobs/123", title="Analyst", jd_text="Analyst position")
    db.add(post)
    db.flush()
    app = Application(user_id=user.id, batch_id=batch.id, job_posting_id=post.id,
                      mailbox_connection_id=conn.id, transport_status="sent", subject="Analyst",
                      recipient_email="jobs@hiring.test", recipient_normalized="jobs@hiring.test",
                      provider_thread_id="thr", provider_message_id="original", rfc_message_id="<a@sender.test>",
                      sent_at=utcnow()-timedelta(days=8), next_follow_up_at=utcnow()-timedelta(days=1))
    db.add(app)
    db.commit()
    return user, conn, app


def test_unrelated_or_other_mailbox_messages_are_ignored(db_session, sent):
    user, conn, app = sent
    other = MailboxConnection(user_id=user.id, sender_email="other@sender.test", provider="gmail_oauth")
    db_session.add(other)
    db_session.commit()

    def ingest(connection, thread, subject, body, sender=app.recipient_email):
        return ingest_inbound_messages(db_session, conn=connection, records=[InboundMessageRecord(
            provider_message_id=subject, provider_thread_id=thread, sender_email=sender,
            recipient_email=connection.sender_email, subject=subject, body_snippet=body,
            rfc_message_id="", in_reply_to="", references="", received_at=utcnow())])

    assert ingest(other, "thr", "wrong mailbox", "hello") == 0
    assert ingest(conn, "", "Office newsletter", "hello") == 0
    assert ingest(conn, "thr", "Delayed", "Delivery delayed; will retry", "mailer-daemon@sender.test") == 1
    assert app.response_status == "delivery_delay"
    assert ingest(conn, "thr", "Application", "Thank you for applying. Can we schedule an interview?") == 1
    assert app.outcome_status == "interview"


def test_followup_approval_freezes_sender_body_and_retains_initial_state(db_session, sent, monkeypatch):
    user, conn, app = sent
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    assert followup.evaluate_and_queue_followups_for_user(db_session, user_id=user.id)[0] == 1
    assert claim_next_outbox_entry(db_session, worker_id="early") is None
    draft = followup.followup_view(db_session, app)
    followup.approve_followup(db_session, app=app, draft_id=draft["id"], confirm_inbox=False)
    claimed = claim_next_outbox_entry(db_session, worker_id="worker")
    assert claimed.kind == "followup"
    transport = RecordingTransport()
    assert process_outbox_entry(db_session, entry=claimed, transport_override=transport, ignore_window_for_test=True) == "sent"
    assert transport.sent[0].body_text == draft["body"]
    assert transport.sent[0].from_email == conn.sender_email
    assert transport.sent[0].in_reply_to == app.rfc_message_id
    assert app.transport_status == "sent" and app.follow_up_count == 1
    assert app.provider_message_id == "original"


def test_paginated_sent_history_resumes_and_retries_metadata(db_session, sent):
    _, conn, _ = sent
    conn.encrypted_credentials = encrypt_json({"access_token": "mock"})
    db_session.commit()
    fail = True

    def provider(request):
        nonlocal fail
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "31", "emailAddress": conn.sender_email})
        if path.endswith("/messages"):
            if request.url.params.get("pageToken"):
                return httpx.Response(200, json={"messages": [{"id": "two"}]})
            return httpx.Response(200, json={"messages": [{"id": "one"}], "nextPageToken": "next"})
        if path.endswith("/two") and fail:
            fail = False
            return httpx.Response(503)
        return httpx.Response(200, json={"historyId": "32", "payload": {"headers": [
            {"name": "To", "value": "jobs@prior.test"}, {"name": "Subject", "value": "Application for analyst"}
        ]}})

    def sync():
        return sync_gmail_sent_history(db_session, conn=conn, max_pages=1, http_transport=httpx.MockTransport(provider))
    assert sync()["status"] == "partial"
    assert sync()["status"] == "error"
    assert sync()["status"] == "ok"
    assert conn.sent_records_count == 2 and conn.sent_sync_cursor == "31"



def test_manual_send_only_review_and_reply_after_approval(db_session, sent, monkeypatch):
    user, conn, app = sent
    conn.inbox_poll_enabled = False
    db_session.commit()
    assert followup.prepare_followup(db_session, app=app, manual=True)
    view = followup.followup_view(db_session, app)
    assert view["confirm_inbox_required"]
    with pytest.raises(ValueError, match="Check the original mailbox"):
        followup.approve_followup(db_session, app=app, draft_id=view["id"], confirm_inbox=False)
    approved = followup.approve_followup(db_session, app=app, draft_id=view["id"], confirm_inbox=True)
    assert followup.approve_followup(db_session, app=app, draft_id=view["id"], confirm_inbox=True).id == approved.id
    app.response_status = "human_reply"
    db_session.commit()
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    claimed = claim_next_outbox_entry(db_session, worker_id="mock")
    transport = RecordingTransport()
    assert process_outbox_entry(db_session, entry=claimed, transport_override=transport,
                                ignore_window_for_test=True) == "cancelled"
    assert not transport.sent and app.transport_status == "sent" and app.follow_up_count == 0


def test_uncertain_followup_reconciles_only_child_action(db_session, sent, monkeypatch):
    from app.services.mailbox.base import HandoffOutcome, SendResult
    from app.services.outbox import reconcile_application_outbox_states, reconcile_uncertain_outbox_entry

    user, conn, app = sent
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    followup.prepare_followup(db_session, app=app)
    draft = followup.followup_view(db_session, app)
    followup.approve_followup(db_session, app=app, draft_id=draft["id"], confirm_inbox=False)
    transport = RecordingTransport(HandoffOutcome.AMBIGUOUS_HANDOFF)
    claimed = claim_next_outbox_entry(db_session, worker_id="mock")
    process_outbox_entry(db_session, entry=claimed, transport_override=transport, ignore_window_for_test=True)
    assert claimed.status == "send_uncertain"
    assert reconcile_application_outbox_states(db_session, user_id=user.id) == 0
    assert app.transport_status == "sent" and app.follow_up_count == 0
    assert not followup.prepare_followup(db_session, app=app, manual=True)
    assert reconcile_uncertain_outbox_entry(db_session, user=user, app_rec=app, transport_override=transport)["reconciled_outcome"] == "still_uncertain"
    transport.reconcile_result = SendResult(outcome=HandoffOutcome.PROVIDER_ACCEPTED, provider_message_id="followup-1")
    result = reconcile_uncertain_outbox_entry(db_session, user=user, app_rec=app, transport_override=transport)
    assert result["reconciled_outcome"] == "sent" and app.follow_up_count == 1
    assert app.provider_message_id == "original" and app.provider_thread_id == "thr"
    assert db_session.get(Batch, app.batch_id).sent_count == 0


def test_different_vacancy_override_is_scoped_and_cannot_waive_hard_blocks(db_session, sent):
    from app.db.models import RecipientHistory, SearchPreference
    from app.services.dedupe import (
        DuplicateRecipientError,
        find_existing_recipients,
        record_duplicate_override,
        reserve_recipients_atomically,
    )

    user, conn, app = sent
    app.transport_status = "skipped"
    app.primary_skip_code = "PREVIOUSLY_CONTACTED"
    prior = RecipientHistory(user_id=user.id, normalized_email=app.recipient_normalized,
                             display_email=app.recipient_email, source="imported")
    db_session.add(prior)
    db_session.commit()
    args = dict(user_id=user.id, emails=[app.recipient_email])
    assert find_existing_recipients(db_session, **args)
    record_duplicate_override(db_session, app=app, reason="Separate posted role, reference 123")
    assert not find_existing_recipients(db_session, **args, exclude_application_id=app.id)
    assert find_existing_recipients(db_session, **args)
    reserve = dict(user_id=user.id, application_id=app.id, to_email=app.recipient_email, cc_emails=[])
    reserve_recipients_atomically(db_session, **reserve)
    assert prior.source == "imported"
    pref = db_session.query(SearchPreference).filter_by(user_id=user.id).one()
    pref.excluded_recipients_json = [app.recipient_email.upper()]
    db_session.flush()
    with pytest.raises(DuplicateRecipientError):
        reserve_recipients_atomically(db_session, **reserve)
    pref.excluded_recipients_json = []
    prior.source = "suppressed"
    db_session.flush()
    assert find_existing_recipients(db_session, **args, exclude_application_id=app.id)
    assert find_existing_recipients(db_session, user_id=user.id, emails=["do-not-reply@site.test"])


def test_gmail_incremental_pages_include_only_sent_and_retry_same_cursor(db_session, sent):
    from app.services.dedupe import find_existing_recipients
    _, conn, _ = sent
    conn.encrypted_credentials = encrypt_json({"access_token": "mock"})
    conn.sent_sync_checkpoint_json = {"mode": "incremental", "anchor": "10"}
    conn.sent_sync_cursor = "10"
    db_session.commit()
    pages = []

    def handler(req):
        if req.url.path.endswith("/history"):
            assert req.url.params["startHistoryId"] == "10"
            pages.append(req.url.params.get("pageToken"))
            if len(pages) == 1:
                return httpx.Response(200, json={"nextPageToken": "p2", "historyId": "90", "history": [
                    {"messagesAdded": [{"message": {"id": "in"}}]}]})
            return httpx.Response(200, json={"historyId": "100", "history": [
                {"labelsAdded": [{"message": {"id": "out"}, "labelIds": ["SENT"]}]}]})
        labels = ["INBOX"] if req.url.path.endswith("/in") else ["SENT"]
        return httpx.Response(200, json={"labelIds": labels, "payload": {"headers": [
            {"name": "To", "value": conn.sender_email}, {"name": "Cc", "value": "jobs@prior.test"},
            {"name": "Bcc", "value": "hr@other.test"}, {"name": "Subject", "value": "Job application"},
        ]}})
    transport = httpx.MockTransport(handler)
    assert sync_gmail_sent_history(db_session, conn=conn, max_pages=1, http_transport=transport)["status"] == "partial"
    assert conn.sent_sync_cursor == "10"
    assert sync_gmail_sent_history(db_session, conn=conn, max_pages=1, http_transport=transport)["status"] == "ok"
    assert conn.sent_sync_cursor == "100" and pages == [None, "p2"] and conn.sent_records_count == 1
    hits = find_existing_recipients(db_session, user_id=conn.user_id, emails=[conn.sender_email, "jobs@prior.test", "hr@other.test"])
    assert set(hits) == {"jobs@prior.test", "hr@other.test"}
    from app.services.mailbox.gmail_sent import get_mailbox_dedup_coverage
    assert get_mailbox_dedup_coverage(db_session, conn=conn)["suppressed_job_recipients_count"] == 2


def test_gmail_inbox_pages_and_failed_metadata_do_not_advance_cursor(db_session, sent):
    from app.services.mailbox.gmail import GmailMailboxTransport
    _, conn, _ = sent
    conn.encrypted_credentials = encrypt_json({"access_token": "mock"})
    fail = True
    def handler(req):
        nonlocal fail
        if req.url.path.endswith("/history"):
            if req.url.params.get("pageToken") == "second":
                return httpx.Response(200, json={"historyId": "101", "history": [{"messagesAdded": [{"message": {"id": "32"}}]}]})
            return httpx.Response(200, json={"nextPageToken": "second", "historyId": "101", "history": [{
                "messagesAdded": [{"message": {"id": str(i)}} for i in range(32)]}]})
        if req.url.path.endswith("/32") and fail:
            fail = False
            return httpx.Response(503)
        return httpx.Response(200, json={"labelIds": ["INBOX"], "payload": {"headers": []}})
    transport = GmailMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        transport.poll_inbox("100")
    messages, cursor = transport.poll_inbox("100")
    assert len(messages) == 33 and cursor == "101"


def test_exact_external_contacts_without_job_signals_are_blocked(db_session, sent):
    from app.db.models import GmailSentRecord, RecipientHistory
    from app.services.dedupe import find_existing_recipients
    from app.services.mailbox.gmail_sent import get_mailbox_dedup_coverage
    user, conn, _ = sent
    conn.encrypted_credentials = encrypt_json({"access_token": "mock"})
    db_session.commit()
    def handler(req):
        if req.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": conn.sender_email, "historyId": "42"})
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "personal"}, {"id": "business"}]})
        personal = req.url.path.endswith("/personal")
        return httpx.Response(200, json={"payload": {"headers": [
            {"name": "From", "value": conn.sender_email},
            {"name": "To", "value": "Alex+old@hiring.test" if personal else "billing@other.test"},
            {"name": "Cc", "value": f"{conn.sender_email}, do-not-reply@other.test"},
            {"name": "Subject", "value": "Coffee tomorrow?" if personal else "Invoice"},
        ]}})
    assert sync_gmail_sent_history(db_session, conn=conn, http_transport=httpx.MockTransport(handler))["status"] == "ok"
    records = {r.provider_message_id: r for r in db_session.query(GmailSentRecord).all()}
    assert records["personal"].classification == "ambiguous"
    assert records["business"].classification == "non_job"
    hits = find_existing_recipients(db_session, user_id=user.id, emails=[
        "alex+OLD@hiring.test", "alex@hiring.test", "billing@other.test", conn.sender_email, "do-not-reply@other.test"])
    assert hits == {"alex+old@hiring.test": records["personal"], "billing@other.test": records["business"],
                    "do-not-reply@other.test": "Blocked recipient or non-reply address"}
    assert db_session.query(RecipientHistory).count() == 2
    coverage = get_mailbox_dedup_coverage(db_session, conn=conn)
    assert coverage["prior_contact_count"] == 2 and coverage["suppressed_job_recipients_count"] == 0


def test_tracker_endpoints_bind_followup_approval_to_its_owner(client, db_session, sent):
    from app.api.deps import get_current_user
    from app.db.models import User
    user, conn, app = sent
    conn.inbox_poll_enabled = False
    stranger = User(email="other@tenant.test", email_normalized="other@tenant.test")
    db_session.add(stranger)
    db_session.commit()
    base = f"/api/v1/applications/{app.id}"
    client.app.dependency_overrides[get_current_user] = lambda: user
    draft = client.post(base + "/followup/draft").json()
    assert draft["status"] == "awaiting_approval" and draft["confirm_inbox_required"]
    params = {"draft_id": draft["id"], "confirm_inbox": True}
    client.app.dependency_overrides[get_current_user] = lambda: stranger
    assert client.get(base).status_code == 404
    assert client.post(base + "/followup/approve", json=params).status_code == 404
    assert client.post(base + "/duplicate-override", json={"reason": "Separate role"}).status_code == 404
    client.app.dependency_overrides[get_current_user] = lambda: user
    assert client.post(base + "/followup/approve", json=params).json()["status"] == "queued"
    assert client.post(base + "/followup/approve", json=params).json()["id"] == draft["id"]
    detail = client.get(base).json()
    assert detail["application"]["transport_status"] == "sent" and detail["followup"]["status"] == "queued"
    assert sum(e["event_type"] == "followup_approved" for e in detail["events"]) == 1


def test_legacy_sent_rows_remain_auditable_and_block_external_exact_matches(db_session, sent):
    from app.db.models import GmailSentRecord
    from app.services.dedupe import find_existing_recipients

    user, conn, _ = sent
    rows = []
    for i, address in enumerate(["pat@hiring.test", conn.sender_email, "noreply@hiring.test"]):
        rows.append(GmailSentRecord(user_id=user.id, mailbox_connection_id=conn.id, provider_message_id=str(i),
            recipient_email=address, recipient_normalized=address, classification="ambiguous",
            is_suppressed_recipient=False, contact_keys_json=[]))
    db_session.add_all(rows)
    db_session.commit()
    hits = find_existing_recipients(db_session, user_id=user.id, emails=[r.recipient_email for r in rows])
    assert hits == {"pat@hiring.test": rows[0], "noreply@hiring.test": "Blocked recipient or non-reply address"}
    assert all(not row.is_suppressed_recipient and row.classification == "ambiguous" for row in rows)


def test_example_address_followup_returns_specific_reason_preserving_initial_receipt(db_session, sent):
    user, conn, app = sent
    app.recipient_email = "jobs@demo.example"
    db_session.commit()
    count, reasons = followup.evaluate_and_queue_followups_for_user(db_session, user_id=user.id)
    assert count == 0 and any("Demo/example" in reason for reason in reasons)
    assert app.transport_status == "sent" and app.provider_message_id == "original"


@pytest.mark.parametrize("recover", [False, True])
def test_send_only_acceptance_sets_seven_day_due_date(db_session, sent, monkeypatch, recover):
    from app.db.base import as_utc
    from app.db.models import CvVersion, OutboxEntry, SearchPreference
    from app.services.approval import application_draft_hash
    from app.services.mailbox.base import HandoffOutcome, SendResult
    from app.services.outbox import recover_stale_outbox_leases

    user, conn, app = sent
    conn.granted_scopes_json = ["https://www.googleapis.com/auth/gmail.send"]
    conn.inbox_poll_enabled = False
    db_session.query(SearchPreference).one().followup_interval_days = 3
    ver = db_session.query(CvVersion).one()
    app.selected_cv_version_id = ver.id
    app.frozen_cv_sha256 = app.attachment_pdf_sha256 = ver.sha256
    app.frozen_cv_filename = ver.original_filename
    app.import_origin = "gemini_zip"
    app.sent_at = app.next_follow_up_at = None
    app.transport_status = "sending" if recover else "queued"
    app.approval_state = "approved"
    app.approved_content_hash = application_draft_hash(app)
    entry = OutboxEntry(user_id=user.id, application_id=app.id, mailbox_connection_id=conn.id,
        kind="initial", status=app.transport_status, rfc_message_id=app.rfc_message_id,
        lease_owner="w1" if recover else None, lease_expires_at=utcnow()-timedelta(minutes=10) if recover else None)
    db_session.add(entry)
    db_session.commit()
    transport = RecordingTransport()
    if recover:
        transport.reconcile_result = SendResult(outcome=HandoffOutcome.PROVIDER_ACCEPTED, provider_message_id="confirmed")
        assert recover_stale_outbox_leases(db_session, transport_factory=lambda *_: transport)["reconciled_sent"] == 1
        assert not transport.sent
    else:
        monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
        get_settings.cache_clear()
        claimed = claim_next_outbox_entry(db_session, worker_id="w1")
        assert process_outbox_entry(db_session, entry=claimed, transport_override=transport, ignore_window_for_test=True) == "sent"
    assert app.transport_status == "sent" and as_utc(app.next_follow_up_at) - as_utc(app.sent_at) == timedelta(days=7)
