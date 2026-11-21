"""Two-session regression checks for outbox cancellation and claim fencing."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import Application, Batch, OutboxEntry, RecipientHistory
from app.db.session import session_scope
from app.services.approval import approve_application_for_send
from app.services.batch_runner import execute_batch_pipeline
from app.services.mailbox.base import HandoffOutcome, SendResult
from app.services.outbox import (
    cancel_queued_outbox_entry,
    claim_next_outbox_entry,
    enqueue_application_for_send,
    process_outbox_entry,
    reconcile_uncertain_outbox_entry,
    recover_stale_outbox_leases,
)
from tests.test_batch_pipeline import vacancy
from tests.test_csv_audit_remediation import _seed_cafm_user


@pytest.fixture
def queued(db_session, monkeypatch):
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "true")
    get_settings.cache_clear()
    user, _, _, _, _, _, conn = _seed_cafm_user(db_session, with_mailbox=True)
    batch = Batch(user_id=user.id, mode="live", run_mode="approval_required")
    db_session.add(batch)
    db_session.commit()
    execute_batch_pipeline(db_session, batch=batch, candidate_overrides=[vacancy()], skip_network_dns=True)
    app = db_session.scalars(select(Application)).one()
    ok, reason = approve_application_for_send(db_session, user=user, app_rec=app)
    assert ok, reason
    db_session.commit()
    entry, reason = enqueue_application_for_send(db_session, user=user, batch=batch, app_record=app, conn=conn)
    assert entry, reason
    return user, batch, app, entry


def test_cached_cancellation_cannot_release_handoff_reservation(db_session, queued):
    user, _, app, entry = queued
    with session_scope() as worker:
        worker.execute(update(OutboxEntry).where(OutboxEntry.id == entry.id).values(status="sending"))
        worker.execute(update(Application).where(Application.id == app.id).values(transport_status="sending"))
    # Both ORM instances still reflect the earlier queue selection in this session.
    assert entry.status == app.transport_status == "queued"
    with pytest.raises(ValueError, match="CANCEL_NOT_ALLOWED"):
        cancel_queued_outbox_entry(db_session, user=user, app_rec=app)
    db_session.rollback()
    assert entry.status == app.transport_status == "sending"
    assert db_session.scalars(select(RecipientHistory)).one().source == "reserved"


def test_slow_provider_acceptance_resolves_uncertainty_only_once(db_session, queued):
    _, batch, app, entry = queued
    leased = claim_next_outbox_entry(db_session, worker_id="original", outbox_id=entry.id)

    class SlowProvider:
        def reconcile_sent_message(self, *_args):
            return None

        def send_message(self, msg):
            with session_scope() as recovery:
                recovery.execute(update(OutboxEntry).where(OutboxEntry.id == entry.id).values(
                    lease_expires_at=utcnow() - timedelta(seconds=1)))
                recovery.commit()
                stats = recover_stale_outbox_leases(recovery, transport_factory=lambda *_: self)
                assert stats["marked_uncertain"] == 1
            return SendResult(HandoffOutcome.PROVIDER_ACCEPTED, provider_message_id="positive-receipt",
                              rfc_message_id=msg.rfc_message_id)

    assert process_outbox_entry(db_session, entry=leased, transport_override=SlowProvider(), ignore_window_for_test=True) == "sent"
    db_session.refresh(batch)
    assert (batch.sent_count, batch.uncertain_count) == (1, 0)
    assert app.provider_message_id == "positive-receipt"
    assert claim_next_outbox_entry(db_session, worker_id="retry", outbox_id=entry.id) is None


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("found", [False, True])
def test_sender_receipt_wins_during_sent_lookup(db_session, queued, manual, found):
    user, batch, app, entry = queued
    entry.status = app.transport_status = "send_uncertain" if manual else "sending"
    entry.lease_expires_at = utcnow() - timedelta(seconds=1)
    entry.lease_owner = "original-claim"
    db_session.commit()

    class SentLookup:
        def reconcile_sent_message(self, rfc_id, _action_id):
            # An OAuth refresh can commit while the original sender receives its receipt.
            db_session.commit()
            with session_scope() as sender:
                sender.execute(update(OutboxEntry).where(OutboxEntry.id == entry.id).values(
                    status="sent", handoff_outcome=HandoffOutcome.PROVIDER_ACCEPTED.value))
                sender.execute(update(Application).where(Application.id == app.id).values(
                    transport_status="sent", provider_message_id="sender-receipt", sent_at=utcnow()))
                sender.execute(update(Batch).where(Batch.id == batch.id).values(sent_count=1, uncertain_count=0))
            if found:
                return SendResult(HandoffOutcome.PROVIDER_ACCEPTED, provider_message_id="lookup-receipt", rfc_message_id=rfc_id)
            return None

    if manual:
        result = reconcile_uncertain_outbox_entry(db_session, user=user, app_rec=app, transport_override=SentLookup())
        assert result["reconciled_outcome"] == "sent"
    else:
        stats = recover_stale_outbox_leases(db_session, transport_factory=lambda *_: SentLookup())
        assert stats["reconciled_sent"] == stats["marked_uncertain"] == 0
    db_session.expire_all()
    assert (entry.status, app.transport_status, app.provider_message_id) == ("sent", "sent", "sender-receipt")
    assert (batch.sent_count, batch.uncertain_count) == (1, 0)
