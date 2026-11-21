"""Gmail OAuth 2.0 (PKCE, offline refresh, revocation) and Gmail REST API send/poll/reconcile adapter."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_json, encrypt_json, generate_opaque_token, generate_pkce_pair, hash_token
from app.db.base import utcnow
from app.db.models import AuditEvent, AuthToken, MailboxConnection, User
from app.services.auth import AuthError, consume_google_state
from app.services.effective_config import resolve_effective_google_oauth
from app.services.mailbox.base import (
    HandoffOutcome,
    InboundMessageRecord,
    OutboundEmailMessage,
    SendResult,
    build_mime_message,
)
from app.services.mailbox.gmail_sent import (
    GMAIL_COMPOSE_SCOPE as GMAIL_COMPOSE_SCOPE,
)
from app.services.mailbox.gmail_sent import (
    classify_sent_message_for_dedup as classify_sent_message_for_dedup,
)
from app.services.mailbox.gmail_sent import (
    create_gmail_draft as create_gmail_draft,
)
from app.services.mailbox.gmail_sent import (
    get_mailbox_dedup_coverage as get_mailbox_dedup_coverage,
)
from app.services.mailbox.gmail_sent import (
    sync_gmail_sent_history as sync_gmail_sent_history,
)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"


def parse_google_oauth_error(resp: httpx.Response, *, code_exchange: bool = False) -> tuple[str, str]:
    """Extract a safe, user-actionable (error_code, message) from a Google OAuth/Gmail API error response."""
    err_code = f"HTTP_{resp.status_code}"
    err_desc = ""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            if isinstance(payload.get("error"), dict):
                err_obj = payload["error"]
                err_code = str(err_obj.get("status") or err_obj.get("code") or err_code)
                err_desc = str(err_obj.get("message") or "")
            else:
                err_code = str(payload.get("error") or err_code)
                err_desc = str(payload.get("error_description") or "")
    except Exception:
        err_desc = resp.text[:240]

    low_code = err_code.lower()
    low_desc = err_desc.lower()
    if low_code == "invalid_client":
        return (
            "GOOGLE_INVALID_CLIENT",
            "Google rejected the installation OAuth client credentials. "
            "Ask the installation owner to check the active Web application client and its secret.",
        )
    if low_code == "redirect_uri_mismatch":
        return (
            "GOOGLE_REDIRECT_URI_MISMATCH",
            "Google OAuth callback URI mismatch. Verify that the exact Authorized Redirect URI is registered in Google Cloud Console.",
        )
    if low_code == "invalid_grant" and code_exchange:
        return "GOOGLE_CODE_REJECTED", "Google rejected the authorization code. Please start the connection again."
    if low_code == "invalid_grant":
        return (
            "GMAIL_RECONNECT_REQUIRED",
            "Your Google authorization expired or was revoked. Click Reconnect Gmail to grant permission again.",
        )
    if low_code == "access_denied":
        return (
            "GOOGLE_CONSENT_DENIED",
            "Google sign-in or Gmail permission was cancelled before completion. You can try again anytime.",
        )
    if "accessnotconfigured" in low_desc or "gmail api has not been used" in low_desc or "service disabled" in low_desc:
        return (
            "GMAIL_API_DISABLED",
            "The Gmail API is not enabled on the Google Cloud OAuth project. Enable the Gmail API in Google Cloud Console and retry.",
        )
    if "insufficient" in low_desc and "permission" in low_desc:
        return (
            "GMAIL_INSUFFICIENT_SCOPE",
            "Your Gmail connection is missing the required permission. Click Reconnect Gmail to grant the needed scope.",
        )
    if resp.status_code >= 500:
        return (
            "GMAIL_API_UNAVAILABLE",
            f"Google services are temporarily unavailable (HTTP {resp.status_code}). Please try again shortly.",
        )
    return (
        f"GOOGLE_HTTP_{resp.status_code}",
        f"Google OAuth/API request failed (HTTP {resp.status_code}).",
    )


def google_token_fields(payload: dict) -> tuple[str, str, int, list[str]]:
    """Validate the token endpoint response before using identity or changing the mailbox."""
    if not isinstance(payload, dict):
        raise AuthError("GOOGLE_TOKEN_INVALID", "Google returned an incomplete token response.")
    access = payload.get("access_token")
    refresh = payload.get("refresh_token", "")
    scope = payload.get("scope", "")
    try:
        lifetime = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError, OverflowError):
        lifetime = 0
    if (not isinstance(access, str) or not access or not isinstance(refresh, str)
            or not isinstance(scope, str) or not 0 < lifetime <= 2**31 - 1):
        raise AuthError("GOOGLE_TOKEN_INVALID", "Google returned an invalid token response.")
    return access, refresh, lifetime, scope.split()


def upsert_gmail_oauth_connection(
    db: Session,
    *,
    user_id: str,
    email_addr: str,
    google_sub: str,
    client_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    granted_scopes: list[str],
    preserve_existing_smtp_primary: bool = False,
    login_provider_scopes: list[str] | None = None,
    commit: bool = True,
) -> MailboxConnection:
    """
    Create or update a user's Gmail OAuth MailboxConnection with Fernet-encrypted tokens.
    Non-destructive: preserves any existing working SMTP/IMAP fallback connections intact.
    """
    has_readonly = GMAIL_READONLY_SCOPE in granted_scopes
    has_compose = GMAIL_COMPOSE_SCOPE in granted_scopes

    creds_payload = {
        "google_sub": google_sub,
        "client_id": client_id,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "expires_at": (utcnow() + timedelta(seconds=int(expires_in or 3600))).isoformat(),
    }

    # Serialize simultaneous callbacks for this owner before choosing the primary mailbox.
    owner = db.scalar(select(User.id).where(
        User.id == user_id, User.deleted_at.is_(None), User.is_suspended.is_(False),
    ).with_for_update())
    if not owner:
        raise AuthError("GMAIL_ACCOUNT_UNAVAILABLE", "Sign in again to connect Gmail.")
    existing = db.scalar(
        select(MailboxConnection).where(
            MailboxConnection.user_id == user_id,
            MailboxConnection.provider == "gmail_oauth",
            MailboxConnection.sender_email == email_addr,
        )
    )
    all_user_conns = db.scalars(select(MailboxConnection).where(MailboxConnection.user_id == user_id)).all()
    has_active_smtp_primary = any(
        c.provider == "smtp_imap" and c.status == "connected" and c.revoked_at is None and c.is_primary
        for c in all_user_conns
    )
    make_primary = not (preserve_existing_smtp_primary and has_active_smtp_primary)

    if existing:
        existing_creds = decrypt_json(existing.encrypted_credentials) if existing.encrypted_credentials else {}
        if existing_creds.get("google_sub") and existing_creds["google_sub"] != google_sub:
            raise AuthError("GMAIL_IDENTITY_CONFLICT", "This Gmail address is linked to a different Google account.")
        same_grant = existing_creds.get("google_sub") == google_sub and existing_creds.get("client_id") == client_id
        if same_grant and login_provider_scopes and existing.status == "connected" and existing.revoked_at is None:
            # A normal login should preserve a prior explicit upgrade if Google still grants it.
            prior_scopes = set(existing.granted_scopes_json or []) & set(login_provider_scopes)
            if existing.inbox_poll_enabled and GMAIL_READONLY_SCOPE in prior_scopes and not has_readonly:
                granted_scopes = [*granted_scopes, GMAIL_READONLY_SCOPE]
                has_readonly = True
            if existing.save_drafts_in_gmail and GMAIL_COMPOSE_SCOPE in prior_scopes and not has_compose:
                granted_scopes = [*granted_scopes, GMAIL_COMPOSE_SCOPE]
                has_compose = True
        if not refresh_token and same_grant and existing.status == "connected" and existing_creds.get("refresh_token"):
            creds_payload["refresh_token"] = existing_creds["refresh_token"]
        if make_primary:
            for other in all_user_conns:
                other.is_primary = other.id == existing.id
        existing.encrypted_credentials = encrypt_json(creds_payload)
        existing.granted_scopes_json = granted_scopes
        existing.status = "connected"
        existing.revoked_at = None
        existing.last_error = ""
        existing.is_primary = make_primary or existing.is_primary
        existing.inbox_poll_enabled = has_readonly
        existing.inbox_health_status = "healthy" if has_readonly else "disabled"
        existing.save_drafts_in_gmail = has_compose
        if not has_readonly:
            existing.sent_sync_status = "disabled"
        elif existing.sent_sync_status == "disabled":
            existing.sent_sync_status = "never_synced"
        conn = existing
    else:
        if make_primary:
            for other in all_user_conns:
                other.is_primary = False

        conn = MailboxConnection(
            user_id=user_id,
            provider="gmail_oauth",
            label=f"Gmail ({email_addr})",
            sender_email=email_addr,
            granted_scopes_json=granted_scopes,
            encrypted_credentials=encrypt_json(creds_payload),
            is_primary=make_primary,
            status="connected",
            inbox_poll_enabled=has_readonly,
            inbox_health_status="healthy" if has_readonly else "disabled",
            save_drafts_in_gmail=has_compose,
            sent_sync_status="never_synced" if has_readonly else "disabled",
        )
        db.add(conn)
    db.flush()
    if commit:
        db.commit()
    return conn


def get_gmail_connection_health(conn: MailboxConnection) -> dict[str, object]:
    """Return safe, user-facing connection health & permission metadata without exposing raw tokens."""
    scopes = list(conn.granted_scopes_json or [])
    if conn.provider != "gmail_oauth":
        is_ok = conn.status == "connected" and conn.revoked_at is None
        return {
            "health_status": "healthy" if is_ok else (conn.status or "disconnected"),
            "health_message": (
                "Fallback TLS SMTP/IMAP connection active."
                if is_ok
                else (conn.last_error or "Mailbox is disconnected.")
            ),
            "needs_reconnect": not is_ok,
            "has_send_scope": is_ok,
            "has_readonly_scope": bool(conn.imap_host),
            "has_compose_scope": False,
            "has_refresh_token": False,
            "token_expires_at": None,
        }

    has_send = GMAIL_SEND_SCOPE in scopes
    has_readonly = GMAIL_READONLY_SCOPE in scopes
    has_compose = GMAIL_COMPOSE_SCOPE in scopes
    has_access = False
    has_refresh = False
    expires_at_iso: str | None = None
    is_expired = False

    if conn.encrypted_credentials:
        try:
            creds = decrypt_json(conn.encrypted_credentials)
            has_access = bool(creds.get("access_token"))
            has_refresh = bool(creds.get("refresh_token"))
            raw_exp = str(creds.get("expires_at") or "")
            if raw_exp:
                expires_at_iso = raw_exp
                exp = datetime.fromisoformat(raw_exp)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                is_expired = exp <= utcnow()
        except Exception:
            pass

    if conn.revoked_at is not None or conn.status == "revoked":
        health_status = "revoked"
        health_msg = conn.last_error or "Gmail connection was disconnected or revoked. Reconnect Gmail to resume."
        needs_reconnect = True
    elif conn.status == "needs_reconnect":
        health_status = "needs_reconnect"
        health_msg = conn.last_error or "Google permissions expired or were revoked. Click Reconnect Gmail."
        needs_reconnect = True
    elif not has_access and not has_refresh:
        health_status = "needs_reconnect"
        health_msg = "Gmail credentials are unavailable. Click Reconnect Gmail."
        needs_reconnect = True
    elif not has_send:
        health_status = "missing_send_scope"
        health_msg = "Connected without Gmail send permission. Click Reconnect Gmail to grant send access."
        needs_reconnect = True
    elif is_expired and not has_refresh:
        health_status = "expired"
        health_msg = "Access token expired and no refresh token is stored. Click Reconnect Gmail."
        needs_reconnect = True
    else:
        health_status = "healthy"
        health_msg = (
            "Gmail Connected · Automatic token refresh active."
            if has_refresh
            else "Gmail Connected · Active session."
        )
        needs_reconnect = False

    return {
        "health_status": health_status,
        "health_message": health_msg,
        "needs_reconnect": needs_reconnect,
        "has_send_scope": has_send,
        "has_readonly_scope": has_readonly,
        "has_compose_scope": has_compose,
        "has_refresh_token": has_refresh,
        "token_expires_at": expires_at_iso,
    }


def build_gmail_oauth_url(
    db: Session,
    *,
    user_id: str,
    include_inbox_readonly: bool = False,
    include_compose_drafts: bool = False,
) -> str:
    """Create Google OAuth2 authorization URL with PKCE and minimal Gmail scopes."""
    gcfg = resolve_effective_google_oauth(db)
    if not gcfg.client_id or not gcfg.client_secret:
        raise ValueError("Google OAuth client credentials are not configured on this installation")

    scopes = ["openid", GOOGLE_USERINFO_EMAIL_SCOPE, GMAIL_SEND_SCOPE]
    if include_inbox_readonly:
        scopes.append(GMAIL_READONLY_SCOPE)
    if include_compose_drafts:
        scopes.append(GMAIL_COMPOSE_SCOPE)

    state = generate_opaque_token(24)
    verifier, challenge = generate_pkce_pair()
    db.add(
        AuthToken(
            user_id=user_id,
            purpose="oauth_state",
            token_hash=hash_token(state),
            payload_json={
                "flow": "gmail_mailbox",
                "client_id": gcfg.client_id,
                "redirect_uri": gcfg.redirect_uri_gmail,
                "include_compose_drafts": include_compose_drafts,
                "code_verifier": verifier,
                "include_inbox_readonly": include_inbox_readonly,
            },
            expires_at=utcnow() + timedelta(minutes=15),
        )
    )
    db.commit()

    params = {
        "client_id": gcfg.client_id,
        "redirect_uri": gcfg.redirect_uri_gmail,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def complete_gmail_oauth_callback(
    db: Session,
    *,
    user_id: str,
    code: str,
    state: str,
    http_transport: httpx.BaseTransport | None = None,
) -> MailboxConnection:
    """Validate OAuth state bound to the authenticated user, exchange tokens, and encrypt credentials."""
    gcfg = resolve_effective_google_oauth(db)
    if not user_id:
        raise AuthError("GMAIL_CALLBACK_FAILED", "Sign in again to connect Gmail.")
    flow = consume_google_state(db, state=state, flow="gmail_mailbox", user_id=user_id)
    if flow.get("client_id") != gcfg.client_id or flow.get("redirect_uri") != gcfg.redirect_uri_gmail:
        raise AuthError("GOOGLE_CONFIG_CHANGED", "Google OAuth settings changed. Please reconnect Gmail.")
    verifier = flow["code_verifier"]

    with httpx.Client(timeout=20.0, transport=http_transport) as client:
        resp = client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": gcfg.client_id,
                "client_secret": gcfg.client_secret,
                "redirect_uri": gcfg.redirect_uri_gmail,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
        if resp.status_code != 200:
            err_code, friendly_msg = parse_google_oauth_error(resp, code_exchange=True)
            raise AuthError(err_code, friendly_msg)
        token_data = resp.json()
        access_token, refresh_token, expires_in, granted = google_token_fields(token_data)
        if GMAIL_SEND_SCOPE not in granted:
            raise AuthError("GMAIL_INSUFFICIENT_SCOPE", "Gmail send permission is required to connect this mailbox.")
        profile_resp = client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        email_addr = ""
        google_sub = ""
        if profile_resp.status_code == 200:
            info = profile_resp.json()
            google_sub = info.get("sub")
            if info.get("email_verified") is True:
                email_addr = (info.get("email") or "").strip()

    if not email_addr or not isinstance(google_sub, str) or not google_sub:
        raise AuthError("GMAIL_IDENTITY_INVALID", "Unable to verify the Gmail account.")

    requested = [GMAIL_SEND_SCOPE]
    if flow.get("include_inbox_readonly"):
        requested.append(GMAIL_READONLY_SCOPE)
    if flow.get("include_compose_drafts"):
        requested.append(GMAIL_COMPOSE_SCOPE)
    granted_scopes = [scope for scope in requested if scope in granted]
    db.add(AuditEvent(user_id=user_id, actor_user_id=user_id, action="gmail_oauth_authorized", target_type="user", target_id=user_id))
    return upsert_gmail_oauth_connection(
        db,
        user_id=user_id,
        email_addr=email_addr,
        google_sub=google_sub,
        client_id=gcfg.client_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        granted_scopes=granted_scopes,
    )


def revoke_gmail_connection(db: Session, conn: MailboxConnection) -> None:
    """Revoke OAuth token with Google when possible, scrub encrypted credentials, and preserve any fallback SMTP mailbox."""
    try:
        creds = decrypt_json(conn.encrypted_credentials) if conn.encrypted_credentials else {}
        token = creds.get("refresh_token") or creds.get("access_token")
        if token:
            with httpx.Client(timeout=10.0) as client:
                client.post("https://oauth2.googleapis.com/revoke", data={"token": token})
    except Exception:
        pass
    was_primary = bool(conn.is_primary)
    conn.encrypted_credentials = ""
    conn.status = "revoked"
    conn.revoked_at = utcnow()
    conn.is_primary = False
    conn.inbox_poll_enabled = False
    conn.inbox_health_status = "disabled"

    # Non-destructive fallback preservation: if user has another connected mailbox (e.g. SMTP/IMAP), promote it to primary
    if was_primary:
        fallback = db.scalar(
            select(MailboxConnection)
            .where(
                MailboxConnection.user_id == conn.user_id,
                MailboxConnection.id != conn.id,
                MailboxConnection.status == "connected",
                MailboxConnection.revoked_at.is_(None),
            )
            .order_by(MailboxConnection.created_at.desc())
        )
        if fallback is not None:
            fallback.is_primary = True
    db.commit()


class GmailMailboxTransport:
    """Gmail API transport supporting OAuth token refresh, MIME sending, reconciliation, and history polling."""

    def __init__(self, db: Session, conn: MailboxConnection, http_transport: httpx.BaseTransport | None = None) -> None:
        self.db = db
        self.conn = conn
        self.http_transport = http_transport

    def _get_valid_access_token(self, *, force_refresh: bool = False) -> str:
        if not self.conn.encrypted_credentials:
            self.conn.status = "needs_reconnect"
            self.conn.last_error = "Gmail credentials are missing or disconnected. Click Reconnect Gmail."
            self.db.commit()
            raise PermissionError(self.conn.last_error)

        creds = decrypt_json(self.conn.encrypted_credentials)
        access_token = creds.get("access_token", "")
        refresh_token = creds.get("refresh_token", "")
        expires_raw = creds.get("expires_at", "")
        if not force_refresh and access_token:
            if not expires_raw and not refresh_token:
                return access_token  # Legacy credentials without an expiry
            if expires_raw:
                try:
                    exp = datetime.fromisoformat(expires_raw)
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if exp > utcnow() + timedelta(seconds=90 if refresh_token else 0):
                        return access_token
                except (ValueError, TypeError):
                    pass

        if not refresh_token:
            self.conn.status = "needs_reconnect"
            self.conn.last_error = "Gmail access token expired and no refresh token is available. Click Reconnect Gmail."
            self.db.commit()
            raise PermissionError(self.conn.last_error)

        gcfg = resolve_effective_google_oauth(self.db)
        try:
            with httpx.Client(timeout=15.0, transport=self.http_transport) as client:
                resp = client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": gcfg.client_id,
                        "client_secret": gcfg.client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
        except httpx.HTTPError as exc:
            raise PermissionError(f"Unable to reach Google token service to refresh credentials: {exc.__class__.__name__}") from exc

        if resp.status_code != 200:
            err_code, friendly_msg = parse_google_oauth_error(resp)
            if err_code == "GMAIL_RECONNECT_REQUIRED":
                self.conn.status = "needs_reconnect"
            self.conn.last_error = friendly_msg
            self.db.commit()
            raise PermissionError(friendly_msg)

        data = resp.json()
        creds["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            creds["refresh_token"] = data["refresh_token"]
        creds["expires_at"] = (utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))).isoformat()
        self.conn.encrypted_credentials = encrypt_json(creds)
        self.conn.status = "connected"
        self.conn.last_error = ""
        self.db.commit()
        return creds["access_token"]

    def send_message(self, msg: OutboundEmailMessage) -> SendResult:
        try:
            token = self._get_valid_access_token()
            mime = build_mime_message(msg)
        except Exception as exc:
            return SendResult(
                outcome=HandoffOutcome.PRE_HANDOFF_FAILURE,
                rfc_message_id=msg.rfc_message_id,
                error_code="GMAIL_RECONNECT_REQUIRED" if isinstance(exc, PermissionError) else "PRE_HANDOFF_ERROR",
                error_message=str(exc),
            )

        raw_b64 = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii").rstrip("=")
        payload: dict[str, object] = {"raw": raw_b64}
        if msg.thread_id:
            payload["threadId"] = msg.thread_id

        try:
            with httpx.Client(timeout=25.0, transport=self.http_transport) as client:
                resp = client.post(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
                # If access token was revoked/expired right before send, refresh once and retry cleanly
                if resp.status_code == 401:
                    try:
                        refreshed = self._get_valid_access_token(force_refresh=True)
                    except PermissionError as exc:
                        return SendResult(
                            outcome=HandoffOutcome.PROVIDER_REJECTED,
                            rfc_message_id=msg.rfc_message_id,
                            error_code="GMAIL_RECONNECT_REQUIRED",
                            error_message=str(exc),
                        )
                    resp = client.post(
                        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                        headers={"Authorization": f"Bearer {refreshed}"},
                        json=payload,
                    )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # Ambiguous network/timeout AFTER initiating HTTP POST: MUST NOT blindly retry!
            return SendResult(
                outcome=HandoffOutcome.AMBIGUOUS_HANDOFF,
                rfc_message_id=msg.rfc_message_id,
                error_code="NETWORK_TIMEOUT",
                error_message=f"Transport timeout/error after initiating send: {exc}",
            )

        if resp.status_code in (200, 201):
            body = resp.json()
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_ACCEPTED,
                provider_message_id=str(body.get("id", "")),
                provider_thread_id=str(body.get("threadId", "")),
                rfc_message_id=msg.rfc_message_id,
            )

        if resp.status_code == 429 or (resp.status_code == 403 and "rateLimit" in resp.text):
            retry_after = int(resp.headers.get("Retry-After", "60") or 60)
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_REJECTED,
                rfc_message_id=msg.rfc_message_id,
                error_code="RATE_LIMITED",
                error_message="Gmail API quota or rate limit exceeded",
                retry_after_seconds=retry_after,
                is_rate_limit=True,
            )

        if resp.status_code in (400, 401, 403):
            err_code, friendly_msg = parse_google_oauth_error(resp)
            if resp.status_code == 401:
                self.conn.status = "needs_reconnect"
                self.conn.last_error = friendly_msg
                self.db.commit()
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_REJECTED,
                rfc_message_id=msg.rfc_message_id,
                error_code=err_code,
                error_message=friendly_msg,
            )

        # 5xx after POST could have been committed server-side
        return SendResult(
            outcome=HandoffOutcome.AMBIGUOUS_HANDOFF,
            rfc_message_id=msg.rfc_message_id,
            error_code=f"HTTP_{resp.status_code}",
            error_message=f"Ambiguous Gmail server response HTTP {resp.status_code}",
        )

    def reconcile_sent_message(self, rfc_message_id: str, action_id: str) -> SendResult | None:
        """Check Gmail Sent messages by RFC Message-ID to reconcile an ambiguous send."""
        if GMAIL_READONLY_SCOPE not in (self.conn.granted_scopes_json or []):
            return None
        try:
            token = self._get_valid_access_token()
            query = f'in:sent rfc822msgid:{rfc_message_id.strip("<>")}'
            with httpx.Client(timeout=15.0, transport=self.http_transport) as client:
                resp = client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"q": query, "maxResults": 5},
                )
            if resp.status_code != 200:
                return None
            msgs = resp.json().get("messages", [])
            if not msgs:
                return None
            first = msgs[0]
            return SendResult(
                outcome=HandoffOutcome.PROVIDER_ACCEPTED,
                provider_message_id=str(first.get("id", "")),
                provider_thread_id=str(first.get("threadId", "")),
                rfc_message_id=rfc_message_id,
            )
        except Exception:
            return None

    def poll_inbox(self, cursor: str) -> tuple[list[InboundMessageRecord], str]:
        """Incremental Gmail polling using startHistoryId with 404 fallback."""
        if not self.conn.inbox_poll_enabled or GMAIL_READONLY_SCOPE not in (self.conn.granted_scopes_json or []):
            raise PermissionError("Gmail inbox reading is unavailable")
        from sqlalchemy import func

        from app.db.base import as_utc
        from app.db.models import Application

        headers = {"Authorization": f"Bearer {self._get_valid_access_token()}"}
        records: list[InboundMessageRecord] = []
        new_cursor = cursor
        root = "https://gmail.googleapis.com/gmail/v1/users/me/"
        with httpx.Client(timeout=20.0, transport=self.http_transport, base_url=root, headers=headers) as client:
            ids = {}
            page = None
            while True:
                if cursor:
                    params = {"startHistoryId": cursor, "historyTypes": "messageAdded"}
                    if page:
                        params["pageToken"] = page
                    response = client.get("history", params=params)
                    if response.status_code == 404:
                        cursor, page = "", None
                        continue
                    response.raise_for_status()
                    data = response.json()
                    for change in data.get("history", []):
                        ids.update(dict.fromkeys(item["message"]["id"] for item in change.get("messagesAdded", [])))
                    new_cursor = str(data["historyId"])
                else:
                    if not page:
                        response = client.get("profile")
                        response.raise_for_status()
                        new_cursor = str(response.json()["historyId"])
                        first = self.db.scalar(select(func.min(Application.sent_at)).where(
                            Application.user_id == self.conn.user_id, Application.mailbox_connection_id == self.conn.id))
                        start = as_utc(first) if first else utcnow() - timedelta(days=7)
                    params = {"q": f"after:{int(start.timestamp()) - 1} -in:sent", "maxResults": 100}
                    if page:
                        params["pageToken"] = page
                    response = client.get("messages", params=params)
                    response.raise_for_status()
                    data = response.json()
                    ids.update(dict.fromkeys(item["id"] for item in data.get("messages", [])))
                page = data.get("nextPageToken")
                if not page:
                    break
            for mid in ids:
                m_resp = client.get(f"messages/{mid}", params={"format": "metadata", "metadataHeaders": [
                    "From", "To", "Subject", "Message-ID", "In-Reply-To", "References", "Date", "Auto-Submitted", "Content-Type"]})
                if m_resp.status_code == 404:
                    continue
                m_resp.raise_for_status()
                m_data = m_resp.json()
                if set(m_data.get("labelIds", [])) & {"SENT", "DRAFT"}:
                    continue
                hdrs = {h["name"].lower(): h["value"] for h in m_data.get("payload", {}).get("headers", [])}
                dt = utcnow()
                if hdrs.get("date"):
                    try:
                        dt = parsedate_to_datetime(hdrs["date"]).astimezone(timezone.utc)
                    except Exception:
                        pass
                records.append(
                    InboundMessageRecord(
                        provider_message_id=str(m_data.get("id", mid)),
                        provider_thread_id=str(m_data.get("threadId", "")),
                        rfc_message_id=hdrs.get("message-id", ""),
                        in_reply_to=hdrs.get("in-reply-to", ""),
                        references=hdrs.get("references", ""),
                        sender_email=hdrs.get("from", ""),
                        recipient_email=hdrs.get("to", ""),
                        subject=hdrs.get("subject", ""),
                        body_snippet=str(m_data.get("snippet", "")),
                        received_at=dt,
                        headers=hdrs,
                    )
                )
        return records, new_cursor
