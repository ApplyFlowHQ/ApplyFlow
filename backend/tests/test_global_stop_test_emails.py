"""Global Stop and authorization at the two explicit test-email endpoints."""

from unittest.mock import Mock

import pytest

from app.db.base import utcnow
from app.db.models import InstallationConfig, MailboxConnection, User
from app.services.auth import create_session
from app.services.mailbox.base import HandoffOutcome, MailboxTransport, SendResult


def _account(db, email, *, stopped):
    user = User(email=email, email_normalized=email, automation_stopped_at=utcnow() if stopped else None)
    db.add(user)
    db.flush()
    token, _ = create_session(db, user)
    return user, {"Authorization": f"Bearer {token}"}


def _assert_stopped(response):
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "AUTOMATION_STOPPED",
        "message": "Global Stop Automation is active; resume automation first.",
    }


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "active"])
def test_mailbox_self_test_respects_owner_stop(client, db_session, monkeypatch, stopped):
    owner, headers = _account(db_session, "owner@example.test", stopped=stopped)
    _, other_headers = _account(db_session, "other@example.test", stopped=not stopped)
    mailbox = MailboxConnection(user_id=owner.id, provider="smtp_imap", sender_email=owner.email)
    db_session.add(mailbox)
    db_session.commit()
    url = f"/api/v1/mailboxes/{mailbox.id}/send-test"
    transport = Mock(spec=MailboxTransport)
    transport.send_message.return_value = SendResult(HandoffOutcome.PROVIDER_ACCEPTED)
    transport.reconcile_sent_message.return_value = None
    get_transport = Mock(return_value=transport)
    monkeypatch.setattr("app.services.outbox.get_mailbox_transport", get_transport)

    foreign = client.post(url, headers=other_headers, json={"explicit_confirm": True})
    assert foreign.status_code == 404
    assert foreign.json()["detail"]["code"] == "MAILBOX_NOT_FOUND"
    unconfirmed = client.post(url, headers=headers, json={"explicit_confirm": False})
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"
    get_transport.assert_not_called()

    response = client.post(url, headers=headers, json={"explicit_confirm": True})
    if stopped:
        _assert_stopped(response)
        get_transport.assert_not_called()
    else:
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["recipient_email"] == owner.email
        transport.send_message.assert_called_once()
        assert transport.send_message.call_args.args[0].to_email == owner.email


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "active"])
def test_installation_test_email_respects_manager_stop(client, db_session, monkeypatch, stopped):
    manager, headers = _account(db_session, "manager@example.test", stopped=stopped)
    _, other_headers = _account(db_session, "member@example.test", stopped=not stopped)
    db_session.add(InstallationConfig(owner_user_id=manager.id))
    db_session.commit()
    deliver = Mock(return_value={"provider": "smtp"})
    monkeypatch.setattr("app.services.connection_tests.send_transactional_email", deliver)
    url = "/api/v1/integrations/send-test-email"

    denied = client.post(url, headers=other_headers)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "OWNER_REQUIRED"
    deliver.assert_not_called()

    response = client.post(url, headers=headers)
    if stopped:
        _assert_stopped(response)
        deliver.assert_not_called()
    else:
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["details"] == {"delivery_mode": "smtp", "recipient": manager.email}
        deliver.assert_called_once()
        assert deliver.call_args.args[0] == manager.email


def test_password_reset_email_available_during_global_stop(client, db_session, monkeypatch):
    user, _ = _account(db_session, "recovery@example.org", stopped=True)
    deliver = Mock()
    monkeypatch.setattr("app.services.auth.send_transactional_email", deliver)

    response = client.post("/api/v1/auth/forgot-password", json={"email": user.email})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    deliver.assert_called_once()
    recipient, _, body = deliver.call_args.args
    assert recipient == user.email
    assert f"/reset-password?token={response.json()['reset_token_for_test']}" in body


def test_verification_email_available_during_global_stop(client, db_session, monkeypatch):
    user, headers = _account(db_session, "verification@example.org", stopped=True)
    deliver = Mock()
    monkeypatch.setattr("app.api.routes_auth.send_transactional_email", deliver)

    response = client.post("/api/v1/auth/resend-verification", headers=headers)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    deliver.assert_called_once()
    recipient, _, body = deliver.call_args.args
    assert recipient == user.email
    assert f"/verify-email?token={response.json()['verification_token_for_test']}" in body
