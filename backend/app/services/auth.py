"""Authentication, HttpOnly sessions, rate limiting, email verification, password recovery, and Google Sign-In."""

from __future__ import annotations

from datetime import timedelta, timezone
from urllib.parse import urlencode

import httpx
from google.auth import jwt as google_jwt
from google.auth.exceptions import GoogleAuthError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.encryption import generate_opaque_token, generate_pkce_pair, hash_token
from app.core.security import hash_password, normalize_email_dedupe_key, verify_password
from app.db.base import as_utc, utcnow
from app.db.models import (
    AuditEvent,
    AuthToken,
    CandidateProfile,
    LoginAttempt,
    SearchPreference,
    User,
    UserSession,
)
from app.services.effective_config import resolve_effective_google_oauth
from app.services.transactional_email import send_transactional_email

SESSION_COOKIE_NAME = "applyflow_session"
CSRF_COOKIE_NAME = "applyflow_csrf"


class AuthError(ValueError):
    """Domain authentication/authorization failure with stable error code."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _check_login_rate_limit(db: Session, email_norm: str, ip_address: str) -> None:
    settings = get_settings()
    cutoff = utcnow() - timedelta(minutes=15)
    failed_count = db.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.email_normalized == email_norm,
            LoginAttempt.succeeded.is_(False),
            LoginAttempt.attempted_at >= cutoff,
        )
    ) or 0
    if failed_count >= settings.login_rate_limit_per_15m:
        raise AuthError(
            "RATE_LIMITED",
            "Too many failed login attempts. Please wait 15 minutes or reset your password.",
            status_code=429,
        )


def _create_user_defaults(db: Session, user: User) -> None:
    profile = CandidateProfile(
        user_id=user.id,
        full_name=user.full_name,
        contact_email=user.email,
        signature_text=f"Best regards,\n{user.full_name or user.email}",
    )
    pref = SearchPreference(
        user_id=user.id,
        timezone=user.timezone or "UTC",
        daily_recipient_limit=get_settings().max_daily_recipients_default,
    )
    db.add(profile)
    db.add(pref)


def register_user(
    db: Session,
    *,
    email: str,
    password: str,
    full_name: str = "",
    ui_locale: str = "en",
    timezone_name: str = "UTC",
) -> tuple[User, str]:
    """Create an account and return (user, raw_verification_token)."""
    email_norm = normalize_email_dedupe_key(email)
    existing = db.scalar(select(User).where(User.email_normalized == email_norm, User.deleted_at.is_(None)))
    if existing:
        raise AuthError("EMAIL_EXISTS", "An account with this email already exists.", status_code=409)

    if len(password) < 10:
        raise AuthError("WEAK_PASSWORD", "Password must be at least 10 characters long.", status_code=400)

    settings = get_settings()
    is_operator = email_norm in settings.operator_emails_set

    user = User(
        email=email.strip(),
        email_normalized=email_norm,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
        ui_locale="ar" if ui_locale == "ar" else "en",
        timezone=timezone_name or "UTC",
        is_operator=is_operator,
    )
    db.add(user)
    db.flush()
    _create_user_defaults(db, user)

    raw_token = generate_opaque_token(32)
    db.add(
        AuthToken(
            user_id=user.id,
            purpose="email_verify",
            token_hash=hash_token(raw_token),
            expires_at=utcnow() + timedelta(hours=24),
        )
    )
    db.add(AuditEvent(user_id=user.id, actor_user_id=user.id, action="user_registered", target_type="user", target_id=user.id))
    db.commit()
    db.refresh(user)

    verify_url = f"{settings.app_url.rstrip('/')}/verify-email?token={raw_token}"
    send_transactional_email(
        user.email,
        "Verify your ApplyFlow Global account",
        f"Welcome to ApplyFlow Global.\n\nVerify your email address by opening:\n{verify_url}\n\nThis link expires in 24 hours.",
        db=db,
    )
    return user, raw_token


def verify_email_token(db: Session, raw_token: str) -> User:
    token_hash_val = hash_token(raw_token)
    tok = db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == token_hash_val,
            AuthToken.purpose == "email_verify",
        )
    )
    if not tok or tok.consumed_at is not None:
        raise AuthError("INVALID_TOKEN", "Verification token is invalid or already used.", status_code=400)
    if as_utc(tok.expires_at) < utcnow():
        raise AuthError("EXPIRED_TOKEN", "Verification token has expired.", status_code=400)

    user = db.scalar(select(User).where(User.id == tok.user_id, User.deleted_at.is_(None)))
    if not user:
        raise AuthError("USER_NOT_FOUND", "Account no longer exists.", status_code=404)
    tok.consumed_at = utcnow()
    user.email_verified_at = utcnow()
    db.add(AuditEvent(user_id=user.id, actor_user_id=user.id, action="email_verified", target_type="user", target_id=user.id))
    db.commit()
    db.refresh(user)
    return user


def issue_password_reset(db: Session, email: str) -> str | None:
    """Create a single-use expiring password reset token without leaking whether email exists."""
    email_norm = normalize_email_dedupe_key(email)
    user = db.scalar(select(User).where(User.email_normalized == email_norm, User.deleted_at.is_(None)))
    if not user:
        return None
    raw_token = generate_opaque_token(32)
    db.add(
        AuthToken(
            user_id=user.id,
            purpose="password_reset",
            token_hash=hash_token(raw_token),
            expires_at=utcnow() + timedelta(minutes=45),
        )
    )
    db.commit()

    reset_url = f"{get_settings().app_url.rstrip('/')}/reset-password?token={raw_token}"
    send_transactional_email(
        user.email,
        "ApplyFlow Global Password Recovery",
        f"Reset your password using this single-use link (expires in 45 minutes):\n{reset_url}",
        db=db,
    )
    return raw_token


def reset_password_with_token(db: Session, raw_token: str, new_password: str) -> None:
    if len(new_password) < 10:
        raise AuthError("WEAK_PASSWORD", "Password must be at least 10 characters long.", status_code=400)

    tok = db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == hash_token(raw_token),
            AuthToken.purpose == "password_reset",
        )
    )
    if not tok or tok.consumed_at is not None or as_utc(tok.expires_at) < utcnow():
        raise AuthError("INVALID_OR_EXPIRED_TOKEN", "Password reset token is invalid or expired.", status_code=400)

    user = db.scalar(select(User).where(User.id == tok.user_id, User.deleted_at.is_(None)))
    if not user:
        raise AuthError("USER_NOT_FOUND", "Account not found.", status_code=404)

    tok.consumed_at = utcnow()
    user.password_hash = hash_password(new_password)
    # Revoke all existing sessions on password reset
    sessions = db.scalars(select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))).all()
    now = utcnow()
    for s in sessions:
        s.revoked_at = now
    db.add(AuditEvent(user_id=user.id, actor_user_id=user.id, action="password_reset", target_type="user", target_id=user.id))
    db.commit()


def authenticate_user(
    db: Session,
    *,
    email: str,
    password: str,
    ip_address: str = "",
) -> User:
    email_norm = normalize_email_dedupe_key(email)
    _check_login_rate_limit(db, email_norm, ip_address)

    user = db.scalar(select(User).where(User.email_normalized == email_norm, User.deleted_at.is_(None)))
    ok = bool(user and user.password_hash and verify_password(password, user.password_hash))

    db.add(LoginAttempt(email_normalized=email_norm, ip_address=ip_address[:64], succeeded=ok))
    db.commit()

    if not ok or not user:
        raise AuthError("INVALID_CREDENTIALS", "Invalid email or password.", status_code=401)
    if user.is_suspended:
        raise AuthError("ACCOUNT_SUSPENDED", "This account is currently suspended.", status_code=403)
    return user


def create_session(
    db: Session,
    user: User,
    *,
    user_agent: str = "",
    ip_address: str = "",
) -> tuple[str, UserSession]:
    raw_session_token = generate_opaque_token(32)
    csrf_token = generate_opaque_token(24)
    ttl = timedelta(hours=get_settings().session_ttl_hours)
    sess = UserSession(
        user_id=user.id,
        token_hash=hash_token(raw_session_token),
        csrf_token=csrf_token,
        user_agent=(user_agent or "")[:300],
        ip_prefix=(ip_address or "")[:45],
        expires_at=utcnow() + ttl,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return raw_session_token, sess


def get_session_and_user(db: Session, raw_session_token: str | None) -> tuple[UserSession, User] | None:
    if not raw_session_token:
        return None
    sess = db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_token(raw_session_token),
            UserSession.revoked_at.is_(None),
        )
    )
    if not sess:
        return None
    now = utcnow()
    expires = sess.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        return None

    user = db.scalar(select(User).where(User.id == sess.user_id, User.deleted_at.is_(None)))
    if not user or user.is_suspended:
        return None
    return sess, user


def revoke_session(db: Session, raw_session_token: str | None) -> None:
    if not raw_session_token:
        return
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(raw_session_token)))
    if sess and sess.revoked_at is None:
        sess.revoked_at = utcnow()
        db.commit()


def consume_google_state(db: Session, *, state: str, flow: str, user_id: str | None) -> dict:
    now = utcnow()
    payload = db.scalar(
        update(AuthToken)
        .where(
            AuthToken.purpose == "oauth_state",
            AuthToken.token_hash == hash_token(state),
            AuthToken.payload_json["flow"].as_string() == flow,
            AuthToken.user_id == user_id,
            AuthToken.expires_at > now,
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
        .returning(AuthToken.payload_json)
    )
    db.commit()
    if not payload or not payload.get("code_verifier"):
        raise AuthError("INVALID_OAUTH_STATE", "Invalid or expired Google sign-in session. Please start again.")
    return payload


async def _verified_google_identity(client: httpx.AsyncClient, token: str, audience: str, nonce: str) -> dict:
    invalid = AuthError("GOOGLE_ID_TOKEN_INVALID", "Google could not verify this sign-in.")
    try:
        if not token or google_jwt.decode_header(token).get("alg") != "RS256":
            raise invalid
        certs = await client.get("https://www.googleapis.com/oauth2/v1/certs")
        certs.raise_for_status()
        claims = google_jwt.decode(token, certs=certs.json(), audience=audience)
        if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            raise invalid
        if claims.get("azp", audience) != audience:
            raise invalid
        if not nonce or claims.get("nonce") != nonce:
            raise AuthError("GOOGLE_NONCE_MISMATCH", "Google could not verify this sign-in.")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise invalid
        return claims
    except AuthError:
        raise
    except (GoogleAuthError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise invalid from exc


def build_google_signin_url(
    db: Session,
    *,
    connect_gmail: bool = True,
    include_inbox_readonly: bool = False,
) -> str:
    """
    Build Google OAuth 2.0 / OIDC authorization URL with PKCE (S256), CSRF state, and OIDC nonce.
    - Minimum scopes: `openid email profile` (+ `gmail.send` when `connect_gmail=True`).
    - Restricted `gmail.readonly` is kept optional and separate unless explicitly requested.
    """
    from app.services.mailbox.gmail import GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE

    gcfg = resolve_effective_google_oauth(db)
    if not gcfg.client_id or not gcfg.client_secret:
        raise AuthError(
            "GOOGLE_OAUTH_NOT_CONFIGURED",
            "Google Sign-In is not configured on this installation yet.",
            status_code=503,
        )

    scopes = ["openid", "email", "profile"]
    if connect_gmail:
        scopes.append(GMAIL_SEND_SCOPE)
        if include_inbox_readonly:
            scopes.append(GMAIL_READONLY_SCOPE)

    state = generate_opaque_token(24)
    nonce = generate_opaque_token(24)
    code_verifier, code_challenge = generate_pkce_pair()
    db.add(
        AuthToken(
            user_id=None,
            purpose="oauth_state",
            token_hash=hash_token(state),
            payload_json={
                "flow": "app_login",
                "client_id": gcfg.client_id,
                "code_verifier": code_verifier,
                "nonce": nonce,
                "connect_gmail": bool(connect_gmail),
                "include_inbox_readonly": bool(include_inbox_readonly),
                "redirect_uri": gcfg.redirect_uri_auth,
            },
            expires_at=utcnow() + timedelta(minutes=15),
        )
    )
    db.commit()

    params: dict[str, str] = {
        "client_id": gcfg.client_id,
        "redirect_uri": gcfg.redirect_uri_auth,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "include_granted_scopes": "true" if connect_gmail else "false",
    }
    if connect_gmail:
        params["access_type"] = "offline"
        params["prompt"] = "select_account consent"
    else:
        params["prompt"] = "select_account"
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


async def complete_google_signin(
    db: Session,
    *,
    code: str,
    state: str,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[User, dict[str, object]]:
    """
    Exchange Google OAuth authorization code with PKCE verifier, validate OIDC claims & nonce,
    safely sign in / register / link the user account, and connect Gmail automatically if `gmail.send` was granted.
    """
    from app.services.mailbox.gmail import (
        GMAIL_READONLY_SCOPE,
        GMAIL_SEND_SCOPE,
        google_token_fields,
        parse_google_oauth_error,
        upsert_gmail_oauth_connection,
    )

    settings = get_settings()
    gcfg = resolve_effective_google_oauth(db)
    flow = consume_google_state(db, state=state, flow="app_login", user_id=None)
    code_verifier = flow["code_verifier"]
    expected_nonce = flow.get("nonce", "")
    redirect_uri = flow["redirect_uri"]
    if redirect_uri != gcfg.redirect_uri_auth or flow.get("client_id") != gcfg.client_id:
        raise AuthError("GOOGLE_CONFIG_CHANGED", "Google sign-in settings changed. Please start again.")

    try:
        async with httpx.AsyncClient(timeout=15.0, transport=http_transport) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": gcfg.client_id,
                    "client_secret": gcfg.client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
            )
            if token_resp.status_code != 200:
                err_code, friendly_msg = parse_google_oauth_error(token_resp, code_exchange=True)
                raise AuthError(err_code, friendly_msg, status_code=400)
            token_data = token_resp.json()
            access_token, refresh_token, expires_in, granted_scopes = google_token_fields(token_data)
            info = await _verified_google_identity(client, token_data.get("id_token"), gcfg.client_id, expected_nonce)
    except AuthError:
        raise
    except (ValueError, AttributeError, TypeError) as exc:
        raise AuthError("GOOGLE_TOKEN_INVALID", "Google returned an invalid token response.") from exc
    except httpx.HTTPError as exc:
        raise AuthError(
            "GOOGLE_UNAVAILABLE",
            f"Unable to reach Google authentication services ({exc.__class__.__name__}). Please try again.",
            status_code=503,
        ) from exc

    sub = str(info.get("sub", ""))
    email = info["email"].strip() if isinstance(info.get("email"), str) else ""
    email_verified = info.get("email_verified") is True
    name = str(info.get("name", "")).strip()
    if not sub or not email or not email_verified:
        raise AuthError(
            "GOOGLE_UNVERIFIED_EMAIL",
            "Your Google account email must be verified to sign in with ApplyFlow.",
            status_code=400,
        )

    is_new_user = False
    account_linked = False
    user = db.scalar(select(User).where(User.google_oidc_sub == sub, User.deleted_at.is_(None)))
    if not user:
        email_norm = normalize_email_dedupe_key(email)
        user = db.scalar(select(User).where(User.email_normalized == email_norm).with_for_update())
        authoritative = email.lower().endswith("@gmail.com") or bool(info.get("hd"))
        if user:
            if user.google_oidc_sub or user.deleted_at or user.is_suspended or not authoritative or not user.email_verified_at:
                db.add(AuditEvent(
                    user_id=user.id, action="google_account_link_blocked_unverified",
                    target_type="user", target_id=user.id,
                ))
                db.commit()
                raise AuthError(
                    "ACCOUNT_LINK_VERIFICATION_REQUIRED",
                    "Use your existing ApplyFlow sign-in method for this account.",
                    status_code=409,
                )
            user.google_oidc_sub = sub
            if name and not user.full_name:
                user.full_name = name
            account_linked = True
            db.add(
                AuditEvent(
                    user_id=user.id,
                    actor_user_id=user.id,
                    action="google_account_linked",
                    target_type="user",
                    target_id=user.id,
                )
            )
        else:
            is_new_user = True
            user = User(
                email=email,
                email_normalized=email_norm,
                full_name=name,
                email_verified_at=utcnow() if authoritative else None,
                google_oidc_sub=sub,
                is_operator=authoritative and email_norm in settings.operator_emails_set,
            )
            db.add(user)
            try:
                db.flush()
            except IntegrityError as exc:
                db.rollback()
                raise AuthError("ACCOUNT_LINK_VERIFICATION_REQUIRED", "Please use your existing sign-in method.", 409) from exc
            _create_user_defaults(db, user)
            db.add(
                AuditEvent(
                    user_id=user.id,
                    actor_user_id=user.id,
                    action="user_registered_google",
                    target_type="user",
                    target_id=user.id,
                )
            )

    if user.is_suspended:
        raise AuthError("ACCOUNT_SUSPENDED", "This account is suspended.", status_code=403)
    db.add(AuditEvent(user_id=user.id, actor_user_id=user.id, action="google_signin", target_type="user", target_id=user.id))

    gmail_connected = False
    if flow.get("connect_gmail") and GMAIL_SEND_SCOPE in granted_scopes:
        provider_scopes = granted_scopes
        granted_scopes = [GMAIL_SEND_SCOPE] + (
            [GMAIL_READONLY_SCOPE] if flow.get("include_inbox_readonly") and GMAIL_READONLY_SCOPE in granted_scopes else []
        )
        conn = upsert_gmail_oauth_connection(
            db,
            user_id=user.id,
            email_addr=email,
            google_sub=sub,
            client_id=gcfg.client_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            granted_scopes=granted_scopes,
            login_provider_scopes=provider_scopes,
            commit=False,
            preserve_existing_smtp_primary=False,
        )
        granted_scopes = list(conn.granted_scopes_json)
        gmail_connected = conn.status == "connected"
        db.add(
            AuditEvent(
                user_id=user.id,
                actor_user_id=user.id,
                action="gmail_connected_via_google_signin",
                target_type="mailbox_connection",
                target_id=conn.id,
                details_json={"granted_scopes": granted_scopes},
            )
        )
    db.commit()

    return user, {
        "is_new_user": is_new_user,
        "account_linked": account_linked,
        "gmail_connected": gmail_connected,
        "gmail_email": email if gmail_connected else "",
        "granted_scopes": granted_scopes,
    }
