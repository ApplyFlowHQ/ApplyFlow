"""Automated OAuth 2.0/OIDC, safe account linking, token refresh, Gmail API send,
fallback SMTP preservation, and HTTP 401 invalid_client diagnostic tests.
"""

from __future__ import annotations

import base64
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from google.auth import crypt
from google.auth import jwt as google_jwt
from sqlalchemy import select

from app.core.config import get_settings
from app.core.encryption import decrypt_json, encrypt_json
from app.db.base import utcnow
from app.db.models import AuditEvent, AuthToken, MailboxConnection, User
from app.services.auth import AuthError, build_google_signin_url, complete_google_signin, register_user
from app.services.connection_tests import test_google_oauth_config as check_google_oauth_config
from app.services.effective_config import get_or_create_installation_config, update_encrypted_secrets
from app.services.mailbox.base import HandoffOutcome, OutboundEmailMessage, SendAttachment
from app.services.mailbox.gmail import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    GmailMailboxTransport,
    build_gmail_oauth_url,
    complete_gmail_oauth_callback,
    get_gmail_connection_health,
)
from app.services.storage import compute_sha256
from tests.conftest import build_pdf_bytes

_TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_CERTS = {"fixture": _TEST_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()}
_CERT_ENDPOINT = "https://www.googleapis.com/oauth2/v1/certs"


def _signed_id_token(*, sub: str, email: str, aud: str, nonce: str, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com", "aud": aud, "sub": sub, "email": email,
        "email_verified": True, "nonce": nonce, "iat": now, "exp": now + 600, **overrides,
    }
    return google_jwt.encode(crypt.RSASigner(_TEST_KEY, "fixture"), claims).decode()


def _configure_installation_google_oauth(db_session, *, client_id: str = "123456-test.apps.googleusercontent.com", client_secret: str = "synthetic-credential-590cbfdd") -> None:
    inst = get_or_create_installation_config(db_session)
    inst.google_oauth_client_id = client_id
    inst.encrypted_secrets_json = update_encrypted_secrets(
        inst.encrypted_secrets_json,
        {"google_oauth_client_secret": client_secret},
    )
    inst.setup_state = "completed"
    db_session.commit()


def test_oneclick_google_signin_registers_user_connects_gmail_and_enforces_pkce_nonce(
    client: TestClient, db_session, monkeypatch
) -> None:
    """New user clicks Continue with Google -> registers verified user -> Gmail Connected with minimal scopes."""
    client_id = "123456-oneclick.apps.googleusercontent.com"
    _configure_installation_google_oauth(db_session, client_id=client_id)

    monkeypatch.setenv("APP_URL", "http://localhost")
    monkeypatch.setenv("API_PUBLIC_URL", "http://localhost")
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI_AUTH", raising=False)
    get_settings.cache_clear()
    try:
        r_start = client.get("/api/v1/auth/google/start?connect_gmail=true")
        assert r_start.status_code == 200, r_start.text
        auth_url = r_start.json()["authorization_url"]
        qs = parse_qs(urlparse(auth_url).query)
        assert qs["redirect_uri"] == ["http://localhost/api/v1/auth/google/callback"]
        assert qs["code_challenge_method"] == ["S256"]
        assert qs["access_type"] == ["offline"]
        scopes = set(qs["scope"][0].split())
        assert {"openid", "email", "profile", GMAIL_SEND_SCOPE}.issubset(scopes)
        # Restricted gmail.readonly MUST NOT be requested by default
        assert GMAIL_READONLY_SCOPE not in scopes
        state = qs["state"][0]
        nonce = qs["nonce"][0]

        def _mock_google(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://oauth2.googleapis.com/token":
                form = parse_qs(request.content.decode("utf-8"))
                assert form["redirect_uri"] == ["http://localhost/api/v1/auth/google/callback"]
                assert len(form.get("code_verifier", [""])[0]) >= 32
                return httpx.Response(
                    200,
                    json={
                        "access_token": "synthetic-credential-c0d46b27",
                        "refresh_token": "1//oneclick-refresh",
                        "expires_in": 3600,
                        "scope": f"openid email profile {GMAIL_SEND_SCOPE}",
                        "id_token": _signed_id_token(
                            sub="google-sub-new-user-1",
                            email="fictional-cbed0dbb@gmail.com",
                            aud=client_id,
                            nonce=nonce,
                        ),
                        "token_type": "Bearer",
                    },
                )
            if str(request.url) == _CERT_ENDPOINT:
                return httpx.Response(200, json=_TEST_CERTS)
            return httpx.Response(404)

        mock_tx = httpx.MockTransport(_mock_google)
        monkeypatch.setattr(
            "app.api.routes_auth.complete_google_signin",
            lambda db, *, code, state: complete_google_signin(db, code=code, state=state, http_transport=mock_tx),
        )

        # A callback copied into a different browser cannot claim the original login.
        with TestClient(client.app) as other_browser:
            wrong_browser = other_browser.get(
                "/api/v1/auth/google/callback", params={"code": "unused", "state": state},
            )
        assert wrong_browser.status_code == 400
        # Complete callback
        r_cb = client.get(
            f"/api/v1/auth/google/callback?code=4/0AeanOneClickCode&state={state}",
            follow_redirects=False,
        )
        assert r_cb.status_code == 302, r_cb.text
        assert r_cb.headers["location"] == "http://localhost/dashboard?google_auth=connected&gmail_connected=1"
        assert "applyflow_session=" in r_cb.headers.get("set-cookie", "")

        # Verify session is active and Gmail mailbox is connected with encrypted tokens
        r_me = client.get("/api/v1/auth/me")
        assert r_me.status_code == 200
        assert r_me.json()["user"]["email"] == "fictional-cbed0dbb@gmail.com"
        assert r_me.json()["user"]["onboarding"]["mailbox_connected"] is True

        r_mb = client.get("/api/v1/mailboxes")
        assert r_mb.status_code == 200
        mbs = r_mb.json()["mailboxes"]
        assert len(mbs) == 1
        assert mbs[0]["provider"] == "gmail_oauth"
        assert mbs[0]["status"] == "connected"
        assert mbs[0]["connection_health"]["health_status"] == "healthy"
        assert mbs[0]["connection_health"]["has_send_scope"] is True
        assert mbs[0]["connection_health"]["has_readonly_scope"] is False
        assert mbs[0]["connection_health"]["has_refresh_token"] is True

        # Replay of consumed state MUST fail closed
        r_replay = client.get(
            f"/api/v1/auth/google/callback?code=4/0AeanOneClickCode&state={state}",
            follow_redirects=False,
        )
        assert r_replay.status_code == 400
        assert r_replay.json()["detail"]["code"] == "INVALID_OAUTH_STATE"

        # Browser consent cancellation redirects cleanly with understandable auth_error message
        new_url = client.get("/api/v1/auth/google/start").json()["authorization_url"]
        cancel_state = parse_qs(urlparse(new_url).query)["state"][0]
        r_denied = client.get(
            f"/api/v1/auth/google/callback?error=access_denied&state={cancel_state}",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r_denied.status_code == 302
        assert "auth_error=" in r_denied.headers["location"]
        assert "cancelled" in r_denied.headers["location"].lower()
    finally:
        monkeypatch.delenv("APP_URL", raising=False)
        monkeypatch.delenv("API_PUBLIC_URL", raising=False)
        get_settings.cache_clear()


def test_safe_account_linking_blocks_unverified_password_account_and_preserves_smtp_fallback(
    client: TestClient, db_session, monkeypatch
) -> None:
    """Unverified password account cannot be silently hijacked; verified account links cleanly and preserves SMTP fallback."""
    client_id = "123456-linking.apps.googleusercontent.com"
    _configure_installation_google_oauth(db_session, client_id=client_id)

    # 1. Seed unverified password-protected user
    unverified_user, _ = register_user(
        db_session,
        email="fictional-afbca3a1@gmail.com",
        password="StrongPassword123!",
        full_name="Victim User",
    )
    assert unverified_user.email_verified_at is None

    r_s1 = client.get("/api/v1/auth/google/start?connect_gmail=true")
    qs1 = parse_qs(urlparse(r_s1.json()["authorization_url"]).query)
    state1, nonce1 = qs1["state"][0], qs1["nonce"][0]

    def _mock_victim(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://oauth2.googleapis.com/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.v",
                    "refresh_token": "1//v",
                    "expires_in": 3600,
                    "scope": f"openid email profile {GMAIL_SEND_SCOPE}",
                    "id_token": _signed_id_token(
                        sub="google-sub-victim",
                        email="fictional-afbca3a1@gmail.com",
                        aud=client_id,
                        nonce=nonce1,
                    ),
                },
            )
        assert str(request.url) == _CERT_ENDPOINT
        return httpx.Response(200, json=_TEST_CERTS)

    monkeypatch.setattr(
        "app.api.routes_auth.complete_google_signin",
        lambda db, *, code, state: complete_google_signin(
            db, code=code, state=state, http_transport=httpx.MockTransport(_mock_victim)
        ),
    )
    r_blocked = client.get(f"/api/v1/auth/google/callback?code=c1&state={state1}", follow_redirects=False)
    assert r_blocked.status_code == 409
    assert r_blocked.json()["detail"]["code"] == "ACCOUNT_LINK_VERIFICATION_REQUIRED"
    assert db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.user_id == unverified_user.id,
            AuditEvent.action == "google_account_link_blocked_unverified",
        )
    ) is not None

    # 2. Verify existing verified user with working SMTP/IMAP fallback links cleanly & preserves SMTP
    verified_user, _ = register_user(
        db_session,
        email="fictional-5a457efa@gmail.com",
        password="StrongPassword123!",
        full_name="Verified Owner",
    )
    verified_user.email_verified_at = utcnow()
    smtp_conn = MailboxConnection(
        user_id=verified_user.id,
        provider="smtp_imap",
        label="Existing Gmail SMTP",
        sender_email="fictional-5a457efa@gmail.com",
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        imap_host="imap.gmail.com",
        imap_port=993,
        encrypted_credentials=encrypt_json({"username": "fictional-5a457efa@gmail.com", "password": "app-password"}),
        is_primary=True,
        status="connected",
    )
    db_session.add(smtp_conn)
    db_session.commit()

    r_s2 = client.get("/api/v1/auth/google/start?connect_gmail=true")
    qs2 = parse_qs(urlparse(r_s2.json()["authorization_url"]).query)
    state2, nonce2 = qs2["state"][0], qs2["nonce"][0]

    def _mock_verified(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://oauth2.googleapis.com/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.ok",
                    "refresh_token": "1//ok-ref",
                    "expires_in": 3600,
                    "scope": f"openid email profile {GMAIL_SEND_SCOPE}",
                    "id_token": _signed_id_token(
                        sub="google-sub-verified",
                        email="fictional-5a457efa@gmail.com",
                        aud=client_id,
                        nonce=nonce2,
                    ),
                },
            )
        assert str(request.url) == _CERT_ENDPOINT
        return httpx.Response(200, json=_TEST_CERTS)

    monkeypatch.setattr(
        "app.api.routes_auth.complete_google_signin",
        lambda db, *, code, state: complete_google_signin(
            db, code=code, state=state, http_transport=httpx.MockTransport(_mock_verified)
        ),
    )
    r_ok = client.get(f"/api/v1/auth/google/callback?code=c2&state={state2}", follow_redirects=False)
    assert r_ok.status_code == 302
    assert "gmail_connected=1" in r_ok.headers["location"]

    # Both Gmail OAuth (primary) AND existing SMTP/IMAP fallback (connected, untouched) must coexist!
    db_session.expire_all()
    smtp_after = db_session.scalar(select(MailboxConnection).where(MailboxConnection.id == smtp_conn.id))
    gmail_after = db_session.scalar(
        select(MailboxConnection).where(
            MailboxConnection.user_id == verified_user.id,
            MailboxConnection.provider == "gmail_oauth",
        )
    )
    assert smtp_after is not None and smtp_after.status == "connected" and smtp_after.revoked_at is None
    assert decrypt_json(smtp_after.encrypted_credentials)["password"] == "app-password"
    assert gmail_after is not None and gmail_after.status == "connected" and gmail_after.is_primary is True

    # Disconnecting Gmail OAuth restores SMTP/IMAP fallback as primary!
    me = client.get("/api/v1/auth/me").json()["user"]
    original_post = httpx.Client.post

    def mock_revocation(self, url, **kwargs):
        if url == "https://oauth2.googleapis.com/revoke":
            return httpx.Response(200)
        return original_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", mock_revocation)
    r_disc = client.post(
        f"/api/v1/mailboxes/{gmail_after.id}/disconnect",
        headers={"X-CSRF-Token": me["csrf_token"]},
    )
    assert r_disc.status_code == 200
    db_session.expire_all()
    smtp_restored = db_session.scalar(select(MailboxConnection).where(MailboxConnection.id == smtp_conn.id))
    assert smtp_restored is not None and smtp_restored.is_primary is True and smtp_restored.status == "connected"


def test_gmail_token_refresh_revoked_handling_and_send_with_pdf(db_session) -> None:
    """Expired access token refreshes automatically, sends PDF CV via Gmail API, and revoked token marks needs_reconnect."""
    _configure_installation_google_oauth(db_session)
    user = User(
        email="fictional-3696f03b@gmail.com",
        email_normalized="fictional-3696f03b@gmail.com",
        full_name="Refresh Tester",
        email_verified_at=utcnow(),
    )
    db_session.add(user)
    db_session.flush()

    conn = MailboxConnection(
        user_id=user.id,
        provider="gmail_oauth",
        label="Gmail (fictional-3696f03b@gmail.com)",
        sender_email="fictional-3696f03b@gmail.com",
        granted_scopes_json=["openid", "email", GMAIL_SEND_SCOPE],
        encrypted_credentials=encrypt_json(
            {
                "access_token": "synthetic-credential-1e5ab6ac",
                "refresh_token": "1//valid-refresh-token",
                "expires_at": (utcnow() - timedelta(minutes=10)).isoformat(),
            }
        ),
        is_primary=True,
        status="connected",
    )
    db_session.add(conn)
    db_session.commit()

    calls: list[str] = []

    def _mock_refresh_and_send(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://oauth2.googleapis.com/token":
            return httpx.Response(
                200,
                json={"access_token": "synthetic-credential-d4c4060c", "expires_in": 3600, "token_type": "Bearer"},
            )
        if str(request.url) == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send":
            assert request.headers["Authorization"] == "Bearer synthetic-credential-d4c4060c"
            return httpx.Response(200, json={"id": "gmail-msg-999", "threadId": "gmail-thr-999"})
        return httpx.Response(404)

    pdf_bytes = build_pdf_bytes(["Refresh Tester", "CAFM Specialist"])
    tx = GmailMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(_mock_refresh_and_send))
    send_res = tx.send_message(
        OutboundEmailMessage(
            from_email="fictional-3696f03b@gmail.com",
            from_name="Refresh Tester",
            to_email="careers@employer.example",
            cc_emails=[],
            subject="Application for CAFM Specialist",
            body_text="Please find my attached PDF CV.",
            rfc_message_id="<msg-refresh-1@applyflow.test>",
            attachments=[
                SendAttachment(
                    filename="Refresh_CV.pdf",
                    mime_type="application/pdf",
                    data=pdf_bytes,
                    expected_sha256=compute_sha256(pdf_bytes),
                )
            ],
        )
    )
    assert send_res.outcome == HandoffOutcome.PROVIDER_ACCEPTED
    assert send_res.provider_message_id == "gmail-msg-999"
    assert "https://oauth2.googleapis.com/token" in calls
    assert decrypt_json(conn.encrypted_credentials)["access_token"] == "synthetic-credential-d4c4060c"

    # Now simulate revoked refresh token (invalid_grant) -> marks needs_reconnect with clear message
    creds = decrypt_json(conn.encrypted_credentials)
    creds["expires_at"] = (utcnow() - timedelta(minutes=5)).isoformat()
    conn.encrypted_credentials = encrypt_json(creds)
    db_session.commit()

    def _mock_revoked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})

    tx_rev = GmailMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(_mock_revoked))
    rev_res = tx_rev.send_message(
        OutboundEmailMessage(
            from_email="fictional-3696f03b@gmail.com",
            from_name="Refresh Tester",
            to_email="careers@employer.example",
            cc_emails=[],
            subject="Application",
            body_text="Body",
            rfc_message_id="<msg-refresh-2@applyflow.test>",
        )
    )
    assert rev_res.outcome == HandoffOutcome.PRE_HANDOFF_FAILURE
    assert rev_res.error_code == "GMAIL_RECONNECT_REQUIRED"
    db_session.refresh(conn)
    assert conn.status == "needs_reconnect"
    health = get_gmail_connection_health(conn)
    assert health["needs_reconnect"] is True
    assert "Reconnect Gmail" in str(health["health_message"])


@pytest.mark.asyncio
async def test_google_oauth_config_probe_detects_http_401_invalid_client() -> None:
    """test_google_oauth_config detects HTTP 401 invalid_client (e.g., truncated Client Secret missing GOCSPX-)."""
    def _mock_401(request: httpx.Request) -> httpx.Response:
        if "openid-configuration" in str(request.url):
            return httpx.Response(200, json={"issuer": "https://accounts.google.com"})
        if "oauth2.googleapis.com/token" in str(request.url):
            return httpx.Response(
                401,
                json={"error": "invalid_client", "error_description": "The provided client secret is invalid."},
            )
        return httpx.Response(404)

    res_bad = await check_google_oauth_config(
        "968664633649-test.apps.googleusercontent.com",
        "vgGv_missing_prefix_28_chars",
        redirect_uri_auth="http://localhost/api/v1/auth/google/callback",
        redirect_uri_gmail="http://localhost/api/v1/mailboxes/gmail/callback",
        redirect_uri_sheets="http://localhost/api/v1/sheets/callback",
        transport=httpx.MockTransport(_mock_401),
    )
    assert res_bad.ok is False
    assert res_bad.status == "needs_attention"
    assert "401 invalid_client" in res_bad.message
    assert "provided client secret is invalid" not in res_bad.message.lower()

    def _mock_valid_client(request: httpx.Request) -> httpx.Response:
        if "openid-configuration" in str(request.url):
            return httpx.Response(200, json={"issuer": "https://accounts.google.com"})
        if "oauth2.googleapis.com/token" in str(request.url):
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Malformed auth code."})
        return httpx.Response(404)

    res_ok = await check_google_oauth_config(
        "968664633649-test.apps.googleusercontent.com",
        "synthetic-credential-01c4b7cb",
        redirect_uri_auth="http://localhost/api/v1/auth/google/callback",
        redirect_uri_gmail="http://localhost/api/v1/mailboxes/gmail/callback",
        redirect_uri_sheets="http://localhost/api/v1/sheets/callback",
        transport=httpx.MockTransport(_mock_valid_client),
    )
    assert res_ok.ok is True
    assert res_ok.status == "interactive_required"


@pytest.mark.parametrize("case", [
    "expired", "nonce_missing", "nonce_mismatch", "issuer", "audience", "signature", "unsigned", "no_id_token",
    "wrong_subject", "third_party", "email_unverified", "expired_state", "suspended",
    "partial_consent", "signin_only", "extra_scopes",
])
async def test_google_callback_trust_boundaries(db_session, case):
    client_id = "local-fixture.apps.googleusercontent.com"
    _configure_installation_google_oauth(db_session, client_id=client_id)
    email = "existing@example.org" if case == "third_party" else "fictional-afafb16a@gmail.com"
    original_sub = "different-subject" if case == "wrong_subject" else None
    owner = User(
        email=email, email_normalized=email, email_verified_at=utcnow(), google_oidc_sub=original_sub,
        is_suspended=case == "suspended",
    )
    db_session.add(owner)
    db_session.commit()
    url = build_google_signin_url(db_session, connect_gmail=case != "signin_only")
    params = parse_qs(urlparse(url).query)
    state = params["state"][0]
    claims = dict(sub="subject", email=email, aud=client_id, nonce=params["nonce"][0])
    if case == "expired_state":
        transaction = db_session.scalar(select(AuthToken).where(AuthToken.purpose == "oauth_state"))
        transaction.expires_at = utcnow() - timedelta(seconds=1)
        db_session.commit()
    if case == "expired":
        claims["exp"] = int(time.time()) - 120
    if case == "nonce_missing":
        claims["nonce"] = None
    if case == "nonce_mismatch":
        claims["nonce"] = "different"
    if case == "audience":
        claims["aud"] = "another-client"
    if case == "issuer":
        claims["iss"] = "https://issuer.example"
    if case == "email_unverified":
        claims["email_verified"] = "false"
    token = _signed_id_token(**claims)
    if case == "signature":
        token = token.rsplit(".", 1)[0] + ".ZmFrZQ"
    if case == "unsigned":
        token = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=") + "." + token.split(".")[1] + "."
    scopes = "openid" if case == "partial_consent" else f"openid {GMAIL_SEND_SCOPE} {GMAIL_READONLY_SCOPE}"
    calls = []

    def provider(request):
        calls.append(str(request.url))
        if str(request.url) == _CERT_ENDPOINT:
            return httpx.Response(200, json=_TEST_CERTS)
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        payload = {"access_token": "test-access", "scope": scopes}
        if case != "no_id_token":
            payload["id_token"] = token
        return httpx.Response(200, json=payload)

    async def callback():
        return await complete_google_signin(db_session, state=state, code="unused", http_transport=httpx.MockTransport(provider))

    if case in ("signin_only", "partial_consent", "extra_scopes"):
        user, result = await callback()
        assert user.id == owner.id
        assert result["gmail_connected"] is (case == "extra_scopes")
        mailbox = db_session.scalar(select(MailboxConnection))
        if case == "extra_scopes":
            assert mailbox.granted_scopes_json == [GMAIL_SEND_SCOPE]
            assert not mailbox.inbox_poll_enabled
        else:
            assert mailbox is None
    else:
        with pytest.raises(AuthError):
            await callback()
        db_session.expire_all()
        assert owner.google_oidc_sub == original_sub
        assert db_session.scalar(select(MailboxConnection)) is None
        if case == "expired_state":
            assert calls == []
    call_count = len(calls)
    with pytest.raises(AuthError, match="Invalid or expired"):
        await callback()
    assert len(calls) == call_count


@pytest.mark.parametrize(("status", "payload"), [
    (200, {"access_token": "never-display"}), (302, {}), (403, {"error": "never-display"}),
    (429, {"error_description": "never-display"}), (500, {"error": "invalid_grant"}), (400, None),
    (400, {"error": "redirect_uri_mismatch"}), (400, {"error": "deleted_client"}),
])
async def test_probe_fails_closed_without_echoing_provider_body(status, payload):
    requests = []

    def provider(request):
        requests.append(request)
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        assert request.method == "POST"
        if payload is None:
            return httpx.Response(status, text="never-display")
        return httpx.Response(status, json=payload)

    result = await check_google_oauth_config(
        "fixture-client", "never-display", redirect_uri_auth="http://localhost/api/v1/auth/google/callback",
        redirect_uri_gmail="", redirect_uri_sheets="", transport=httpx.MockTransport(provider),
    )
    assert len(requests) == 1
    assert not result.ok
    assert result.status == "needs_attention"
    assert "never-display" not in str(result.to_dict())


@pytest.mark.parametrize("case", ["wrong_user", "send_denied", "unverified", "unrequested_read", "no_read_upgrade"])
def test_gmail_connect_requires_owner_and_grant(db_session, case):
    _configure_installation_google_oauth(db_session)
    owner = User(email="a@example.org", email_normalized="a@example.org")
    other = User(email="b@example.org", email_normalized="b@example.org")
    db_session.add_all([owner, other])
    db_session.commit()
    url = build_gmail_oauth_url(db_session, user_id=owner.id, include_inbox_readonly=case == "no_read_upgrade")
    state = parse_qs(urlparse(url).query)["state"][0]
    requests = []

    def provider(request):
        requests.append(str(request.url))
        if str(request.url) == "https://oauth2.googleapis.com/token":
            scope = GMAIL_READONLY_SCOPE if case == "send_denied" else GMAIL_SEND_SCOPE
            if case == "unrequested_read":
                scope += " " + GMAIL_READONLY_SCOPE
            return httpx.Response(200, json={"access_token": "test-access", "scope": scope})
        assert str(request.url) == "https://openidconnect.googleapis.com/v1/userinfo"
        return httpx.Response(200, json={"sub": "fixture-sub", "email": owner.email, "email_verified": case != "unverified"})

    def complete(user):
        return complete_gmail_oauth_callback(
            db_session, user_id=user.id, code="unused", state=state, http_transport=httpx.MockTransport(provider),
        )

    if case == "wrong_user":
        with pytest.raises(AuthError):
            complete(other)
        assert not requests
    if case in ("send_denied", "unverified"):
        with pytest.raises(AuthError):
            complete(owner)
        assert db_session.scalar(select(MailboxConnection)) is None
    else:
        conn = complete(owner)
        assert conn.user_id == owner.id and conn.granted_scopes_json == [GMAIL_SEND_SCOPE]
        assert not conn.inbox_poll_enabled


def test_login_preserves_explicit_upgrade_only_for_same_google_subject_and_client(db_session):
    from app.services.mailbox.gmail import GMAIL_COMPOSE_SCOPE, upsert_gmail_oauth_connection

    owner = User(email="fictional-4c102969@gmail.com", email_normalized="fictional-4c102969@gmail.com")
    db_session.add(owner)
    db_session.commit()
    scopes = [GMAIL_SEND_SCOPE, GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE]
    args = dict(user_id=owner.id, email_addr=owner.email, google_sub="first", client_id="client",
                expires_in=3600, access_token="test-access")
    upsert_gmail_oauth_connection(db_session, **args, refresh_token="test-refresh", granted_scopes=scopes)
    refreshed = upsert_gmail_oauth_connection(
        db_session, **args, refresh_token="", granted_scopes=[GMAIL_SEND_SCOPE], login_provider_scopes=scopes,
    )
    assert set(refreshed.granted_scopes_json) == set(scopes)
    assert decrypt_json(refreshed.encrypted_credentials)["refresh_token"] == "test-refresh"
    with pytest.raises(AuthError, match="different Google account"):
        upsert_gmail_oauth_connection(
            db_session, **{**args, "google_sub": "second"}, refresh_token="", granted_scopes=[GMAIL_SEND_SCOPE],
        )
    args["client_id"] = "replacement-client"
    replacement = upsert_gmail_oauth_connection(
        db_session, **args, refresh_token="", granted_scopes=[GMAIL_SEND_SCOPE], login_provider_scopes=scopes,
    )
    assert decrypt_json(replacement.encrypted_credentials)["refresh_token"] == ""
    assert replacement.granted_scopes_json == [GMAIL_SEND_SCOPE]
    assert not replacement.save_drafts_in_gmail and not replacement.inbox_poll_enabled
    assert replacement.sent_sync_status == "disabled"


def test_concurrent_callbacks_claim_state_once(db_session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy.orm import Session

    from app.services.auth import consume_google_state

    _configure_installation_google_oauth(db_session)
    url = build_google_signin_url(db_session, connect_gmail=False)
    state = parse_qs(urlparse(url).query)["state"][0]
    engine = db_session.get_bind()
    barrier = Barrier(2)

    def claim():
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                consume_google_state(session, state=state, flow="app_login", user_id=None)
            except AuthError as exc:
                return exc.code
            return "claimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(claim) for _ in range(2)]
        assert sorted(result.result(timeout=10) for result in results) == ["INVALID_OAUTH_STATE", "claimed"]


