from datetime import timedelta

import pytest
from sqlalchemy import event, select, update

from app.api.routes_tracker import api_suppress_application_recipient
from app.db.base import as_utc, utcnow
from app.db.models import (
    Application,
    Batch,
    JobPosting,
    MailboxConnection,
    OutboxEntry,
    RecipientHistory,
    SearchPreference,
    User,
)
from app.db.session import session_scope


@pytest.fixture
def queued(db_session):
    user = User(email="owner@sample-48ae6e.example.org", email_normalized="owner@sample-48ae6e.example.org")
    db_session.add(user)
    db_session.flush()
    batch = Batch(user_id=user.id)
    mailbox = MailboxConnection(user_id=user.id, provider="smtp", sender_email=user.email)
    db_session.add_all([batch, mailbox, SearchPreference(user_id=user.id)])
    db_session.flush()
    posting = JobPosting(user_id=user.id, batch_id=batch.id, canonical_url_hash="url", content_hash="body",
                         source_provider="test", source_url="", title="Analyst", jd_text="")
    db_session.add(posting)
    db_session.flush()
    recipient = "jobs@sample-48ae6e.example.org"
    app = Application(user_id=user.id, batch_id=batch.id, job_posting_id=posting.id, mailbox_connection_id=mailbox.id,
                      recipient_email=recipient, recipient_normalized=recipient, transport_status="queued")
    db_session.add(app)
    db_session.flush()
    entry = OutboxEntry(user_id=user.id, application_id=app.id, mailbox_connection_id=mailbox.id)
    db_session.add_all([entry, RecipientHistory(user_id=user.id, first_application_id=app.id,
                                                normalized_email=recipient, display_email=recipient, source="reserved")])
    db_session.commit()
    return user, app, entry


def queue_followup(db, app, initial, now):
    initial.status, initial.handoff_outcome = "sent", "provider_accepted"
    app.transport_status, app.provider_message_id, app.sent_at = "sent", "original", now
    db.scalars(select(RecipientHistory)).one().source = "sent"
    entry = OutboxEntry(user_id=app.user_id, application_id=app.id, mailbox_connection_id=initial.mailbox_connection_id,
                        kind="followup", status="queued")
    db.add(entry)
    return entry


@pytest.mark.parametrize("kind", ["initial", "followup"])
@pytest.mark.parametrize("worker_status", ["sending", "send_uncertain", "sent"])
def test_suppression_preserves_worker_handoff(db_session, queued, kind, worker_status):
    user, app, initial = queued
    now = utcnow()
    entry = queue_followup(db_session, app, initial, now) if kind == "followup" else initial
    db_session.commit()
    assert entry.status == "queued"

    # Commit on a separate connection at the tracker's first write. On the old
    # route this lands after it selected the queued row and before its ORM flush.
    tracker_connection = db_session.connection()
    outcome = {"sending": "none", "send_uncertain": "ambiguous_handoff", "sent": "provider_accepted"}[worker_status]
    worker_committed = False

    def worker_wins(_conn, _cursor, _statement, _params, context, _many):
        nonlocal worker_committed
        if worker_committed or not (context.isupdate or context.isinsert or context.isdelete):
            return
        with session_scope() as worker:
            worker.execute(update(OutboxEntry).where(OutboxEntry.id == entry.id).values(
                status=worker_status, handoff_outcome=outcome, attempt_count=1, lease_owner="worker"))
            if kind == "initial":
                values = {"transport_status": worker_status}
                if worker_status == "sent":
                    values.update(provider_message_id="receipt", sent_at=now, next_follow_up_at=now + timedelta(days=7))
                    worker.execute(update(RecipientHistory).where(RecipientHistory.first_application_id == app.id)
                                   .values(source="sent"))
                worker.execute(update(Application).where(Application.id == app.id).values(**values))
            elif worker_status == "sent":
                worker.execute(update(Application).where(Application.id == app.id).values(follow_up_count=1))
        worker_committed = True

    event.listen(tracker_connection, "before_cursor_execute", worker_wins)
    try:
        assert api_suppress_application_recipient(app.id, user, db_session) == {"ok": True}
    finally:
        event.remove(tracker_connection, "before_cursor_execute", worker_wins)

    db_session.expire_all()
    assert worker_committed
    assert (entry.status, entry.handoff_outcome, entry.lease_owner) == (worker_status, outcome, "worker")
    accepted = kind == "followup" or worker_status == "sent"
    assert db_session.scalars(select(RecipientHistory)).one().source == ("sent" if accepted else "reserved")
    assert app.transport_status == ("sent" if kind == "followup" else worker_status)
    assert app.provider_message_id == ("original" if kind == "followup" else "receipt" if accepted else "")
    assert as_utc(app.sent_at) == (now if accepted else None)
    assert app.follow_up_count == int(kind == "followup" and worker_status == "sent")
    assert app.next_follow_up_at is None
    assert app.recipient_normalized in db_session.scalars(select(SearchPreference)).one().excluded_recipients_json


@pytest.mark.parametrize(("kind", "status"), [
    ("initial", "queued"), ("initial", "leased"), ("followup", "awaiting_approval"),
    ("followup", "queued"), ("followup", "leased"),
])
def test_suppression_wins_before_handoff(db_session, queued, kind, status):
    user, app, initial = queued
    entry = queue_followup(db_session, app, initial, utcnow()) if kind == "followup" else initial
    if kind == "followup":
        app.follow_up_count = 1
    entry.status = status
    if status == "leased":
        entry.lease_owner, entry.lease_expires_at = "worker", utcnow() + timedelta(minutes=1)
    app.next_follow_up_at = utcnow()
    pref = db_session.scalars(select(SearchPreference)).one()
    pref.excluded_recipients_json = ["existing@sample-48ae6e.example.org"]
    db_session.commit()

    with session_scope() as tracker:
        for _ in range(2):
            api_suppress_application_recipient(app.id, user, tracker)
    db_session.expire_all()
    assert (entry.status, entry.lease_owner, entry.lease_expires_at) == ("cancelled", None, None)
    assert sorted(pref.excluded_recipients_json) == sorted(["existing@sample-48ae6e.example.org", app.recipient_normalized])
    assert app.next_follow_up_at is None
    if kind == "initial":
        assert app.transport_status == "cancelled"
        assert not db_session.scalars(select(RecipientHistory)).all()
    else:
        assert initial.status == app.transport_status == "sent"
        assert app.provider_message_id == "original" and app.follow_up_count == 1
        assert db_session.scalars(select(RecipientHistory)).one().source == "sent"
