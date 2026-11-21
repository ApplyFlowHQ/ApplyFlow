"""Integration regressions for administrative cancellation and mutable draft operations."""

import pytest
from sqlalchemy import event, select, update

from app.api.routes_automation import api_disconnect_mailbox
from app.db.base import utcnow
from app.db.models import Application, OutboxEntry, RecipientHistory
from app.db.session import session_scope
from app.services import bundle_import
from app.services.account_lifecycle import activate_stop_automation
from app.services.approval import revise_application_draft
from app.services.setup_service import get_or_create_installation_config, set_installation_live_sending

pytest_plugins = ("tests.test_outbox_review_races",)


def _accept(worker, app, entry):
    worker.execute(update(OutboxEntry).where(OutboxEntry.id == entry.id).values(
        status="sent", handoff_outcome="provider_accepted"))
    worker.execute(update(Application).where(Application.id == app.id).values(
        transport_status="sent", sent_at=utcnow(), provider_message_id="receipt"))


def _run_cancel(db, user, entry, action):
    if action == "stop":
        activate_stop_automation(db, user=user)
    elif action == "disconnect":
        api_disconnect_mailbox(entry.mailbox_connection_id, user=user, db=db)
    else:
        set_installation_live_sending(db, user=user, enabled=True)


@pytest.mark.parametrize("action", ["stop", "disconnect", "unlock"])
def test_worker_acceptance_after_cancel_selection_survives(db_session, queued, action):
    user, _, app, entry = queued
    inst = get_or_create_installation_config(db_session)
    inst.owner_user_id, inst.live_sending_enabled = user.id, False
    db_session.commit()
    original_hash = app.approved_content_hash
    # A second connection commits after the cancellation SELECT takes its WAL snapshot.
    raced = False

    def accept_after_select(conn, cursor, statement, *args):
        nonlocal raced
        if not raced and statement.startswith("SELECT") and "FROM outbox_entries" in statement:
            raced = True
            with session_scope() as worker:
                _accept(worker, app, entry)

    event.listen(db_session.connection(), "after_cursor_execute", accept_after_select)
    _run_cancel(db_session, user, entry, action)
    db_session.expire_all()
    assert raced
    assert (entry.status, app.transport_status, app.approved_content_hash) == ("sent", "sent", original_hash)
    assert db_session.scalars(select(RecipientHistory)).one().source == "reserved"


@pytest.mark.parametrize("action", ["stop", "disconnect", "unlock"])
def test_followup_cancellation_preserves_original(db_session, queued, action):
    user, _, app, entry = queued
    inst = get_or_create_installation_config(db_session)
    inst.owner_user_id, inst.live_sending_enabled = user.id, False
    followup = OutboxEntry(user_id=user.id, application_id=app.id, mailbox_connection_id=entry.mailbox_connection_id,
                           kind="followup", status="leased", lease_owner="worker")
    db_session.add(followup)
    db_session.commit()
    with session_scope() as worker:
        _accept(worker, app, entry)
    _run_cancel(db_session, user, entry, action)
    db_session.expire_all()
    assert (followup.status, followup.lease_owner) == ("cancelled", None)
    assert (app.transport_status, app.provider_message_id, entry.status) == ("sent", "receipt", "sent")
    assert db_session.scalars(select(RecipientHistory)).one().source == "reserved"


def test_stop_cancels_leftover_queue_entry_but_preserves_accepted_application(db_session, queued):
    user, _, app, entry = queued
    with session_scope() as worker:
        worker.execute(update(Application).where(Application.id == app.id).values(
            transport_status="provider_accepted", provider_message_id="receipt", sent_at=utcnow()))
    assert app.transport_status == "queued"
    result = activate_stop_automation(db_session, user=user)
    db_session.expire_all()
    assert (result["cancelled_outbox"], entry.status) == (1, "cancelled")
    assert (app.transport_status, app.provider_message_id) == ("provider_accepted", "receipt")
    assert db_session.scalars(select(RecipientHistory)).one().source == "reserved"


@pytest.mark.parametrize("action", ["revision", "revalidation"])
@pytest.mark.parametrize("status", ["sending", "sent", "send_uncertain"])
def test_stale_draft_cannot_mutate_initial_handoff(db_session, queued, action, status):
    user, _, app, entry = queued
    original_subject, original_hash = app.subject, app.approved_content_hash
    with session_scope() as worker:
        worker.execute(update(OutboxEntry).where(OutboxEntry.id == entry.id).values(status=status))
    assert entry.status == "queued"
    with pytest.raises(ValueError, match="(?i)handoff|cannot revalidate"):
        if action == "revision":
            revise_application_draft(db_session, user=user, app_rec=app, subject="New subject")
        else:
            bundle_import.revalidate_application_draft(db_session, user=user, app_rec=app, skip_network=True)
    db_session.commit()  # Even a caller that catches the error must have no partial draft edits.
    db_session.expire_all()
    assert (entry.status, app.subject, app.approved_content_hash) == (status, original_subject, original_hash)
    assert db_session.scalars(select(RecipientHistory)).one().source == "reserved"


@pytest.mark.parametrize("change", ["handoff", "edit"])
def test_revalidation_discards_result_if_draft_changes_during_checks(db_session, queued, monkeypatch, change):
    user, _, app, entry = queued
    original_tier = app.source_verification_tier
    evaluate = bundle_import.evaluate_imported_record

    def evaluate_then_change(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        with session_scope() as other:
            if change == "handoff":
                _accept(other, app, entry)
            else:
                other.execute(update(Application).where(Application.id == app.id).values(subject="Updated subject"))
        return result

    monkeypatch.setattr(bundle_import, "evaluate_imported_record", evaluate_then_change)
    with pytest.raises(bundle_import.BundleImportError) as exc:
        bundle_import.revalidate_application_draft(db_session, user=user, app_rec=app, skip_network=True)
    assert exc.value.code == ("ALREADY_SENT" if change == "handoff" else "DRAFT_CHANGED")
    db_session.commit()
    db_session.expire_all()
    assert app.source_verification_tier == original_tier
    if change == "handoff":
        assert (app.transport_status, app.provider_message_id, entry.status) == ("sent", "receipt", "sent")
    else:
        assert app.subject == "Updated subject"


@pytest.mark.parametrize("action", ["revision", "revalidation"])
def test_mutable_draft_invalidates_pre_handoff_lease(db_session, queued, action):
    user, _, app, entry = queued
    with session_scope() as worker:
        worker.execute(update(OutboxEntry).where(OutboxEntry.id == entry.id).values(status="leased", lease_owner="w"))
    if action == "revision":
        _, invalidated = revise_application_draft(db_session, user=user, app_rec=app, subject="Updated subject")
        assert invalidated
        db_session.commit()
    else:
        bundle_import.revalidate_application_draft(db_session, user=user, app_rec=app, skip_network=True)
    db_session.expire_all()
    assert (entry.status, entry.lease_owner, app.approved_content_hash) == ("cancelled", None, "")
    assert app.transport_status in ("draft", "skipped")
    assert db_session.scalar(select(RecipientHistory)) is None
