"""A mailbox or tracker callback must prove ownership before redeeming its one-use grant."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.encryption import hash_token
from app.db.base import utcnow
from app.db.models import AuthToken, MailboxConnection, SheetsConnection, User
from app.services.auth import create_session, revoke_session


@pytest.fixture(params=["sheets", "graph"])
def flow(request, client, db_session, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("MS_GRAPH_CLIENT_ID", "test-client")
    monkeypatch.setenv("MS_GRAPH_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()
    prefix = "/api/v1/"
    if request.param == "sheets":
        start, callback = "sheets/connect-url", "sheets/callback"
        model = SheetsConnection
    else:
        start, callback = "mailboxes/ms-graph/start", "mailboxes/ms-graph/callback"
        model = MailboxConnection

    def create_owner(name):
        email = f"{name}@example.test"
        user = User(email=email, email_normalized=email)
        db_session.add(user)
        db_session.flush()
        token, _ = create_session(db_session, user)
        return user.id, {"Authorization": f"Bearer {token}"}

    owner_id, owner = create_owner("owner")
    _, other = create_owner("other")
    start_response = client.get(prefix + start, headers=owner)
    assert start_response.status_code == 200
    state = parse_qs(urlsplit(start_response.json()["authorization_url"]).query)["state"][0]
    condition = AuthToken.token_hash == hash_token(state)
    calls = []
    engine = db_session.get_bind()

    def provider(_transport, req):
        calls.append(req.method)
        if req.method == "POST":
            assert str(req.url) in (
                "https://oauth2.googleapis.com/token", "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            )
            form = parse_qs(req.content.decode())
            with Session(engine) as witness:
                token = witness.scalar(select(AuthToken).where(condition))
                assert token.consumed_at is not None
                assert form["code_verifier"] == [token.payload_json["code_verifier"]]
            if form["code"] == ["rejected"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "mock-token", "expires_in": 3600})
        assert str(req.url) == "https://graph.microsoft.com/v1.0/me"
        return httpx.Response(200, json={"mail": "owner@example.test"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", provider)

    def finish(headers, **params):
        return client.get(prefix + callback, params={"state": state, "code": "fixture", **params},
                          headers=headers, follow_redirects=False).status_code

    return owner_id, owner, other, finish, condition, calls, model


def test_only_initiating_owner_can_complete(flow, db_session):
    owner_id, owner, other, finish, condition, calls, model = flow
    assert finish({}) == 401
    assert finish(other) == 400
    assert calls == []
    assert db_session.scalar(select(AuthToken.consumed_at).where(condition)) is None
    assert finish(owner) == 302
    assert finish(owner) == 400
    assert calls.count("POST") == 1
    assert db_session.scalars(select(model.user_id)).all() == [owner_id]


@pytest.mark.parametrize("reason", ["expired", "wrong_flow", "revoked_session", "declined", "exchange_failed"])
def test_callback_failure_paths(flow, db_session, reason):
    _, owner, _, finish, condition, calls, model = flow
    state = db_session.scalar(select(AuthToken).where(condition))
    if reason == "expired":
        state.expires_at = utcnow() - timedelta(seconds=1)
    if reason == "wrong_flow":
        state.payload_json = {**state.payload_json, "flow": "unrelated"}
    db_session.commit()

    if reason == "revoked_session":
        revoke_session(db_session, owner["Authorization"].removeprefix("Bearer "))
        assert finish(owner) == 401
    elif reason == "declined":
        assert finish(owner, error="access_denied") == 400
    elif reason == "exchange_failed":
        assert finish(owner, code="rejected") == 400
    else:
        assert finish(owner) == 400

    assert finish(owner) in (400, 401)
    assert calls == (["POST"] if reason == "exchange_failed" else [])
    db_session.refresh(state)
    assert (state.consumed_at is not None) == (reason in ("declined", "exchange_failed"))
    assert db_session.scalar(select(model.id)) is None


def test_simultaneous_callbacks_exchange_exactly_once(flow, db_session):
    owner_id, owner, _, finish, _, calls, model = flow
    barrier = Barrier(2)

    def callback():
        barrier.wait(timeout=10)
        return finish(owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(callback) for _ in range(2)]
        assert sorted(f.result(timeout=20) for f in futures) == [302, 400]
    assert calls.count("POST") == 1
    assert db_session.scalars(select(model.user_id)).all() == [owner_id]
