"""Authentication, account lifecycle, stop automation, export/delete, and usage endpoints."""

from __future__ import annotations

from datetime import timedelta
from secrets import compare_digest
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth, get_current_user
from app.core.config import get_settings
from app.core.encryption import generate_opaque_token, hash_token
from app.db.base import utcnow
from app.db.models import (
    AuditEvent,
    AuthToken,
    CandidateProfile,
    CvDocument,
    MailboxConnection,
    SearchPreference,
    UsageLedger,
    User,
    UserSession,
)
from app.db.session import get_db
from app.schemas import (
    AccountSettingsPatch,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenVerifyRequest,
)
from app.services.account_lifecycle import (
    activate_stop_automation,
    delete_user_account_and_data,
    export_user_account_bundle,
    resume_automation,
)
from app.services.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthError,
    authenticate_user,
    build_google_signin_url,
    complete_google_signin,
    consume_google_state,
    create_session,
    issue_password_reset,
    register_user,
    reset_password_with_token,
    revoke_session,
    verify_email_token,
)
from app.services.dates import ensure_valid_timezone
from app.services.effective_config import get_installation_config, resolve_effective_live_sending
from app.services.transactional_email import send_transactional_email

router = APIRouter(tags=["auth-account"])


def _set_auth_cookies(response: Response, raw_session: str, csrf_token: str) -> None:
    settings = get_settings()
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_session,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


def _onboarding_checklist(db: Session, user: User) -> dict[str, object]:
    cv_count = db.scalar(
        select(func.count(CvDocument.id)).where(CvDocument.user_id == user.id, CvDocument.is_archived.is_(False))
    ) or 0
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    mailbox_conn = db.scalar(
        select(MailboxConnection).where(
            MailboxConnection.user_id == user.id,
            MailboxConnection.status == "connected",
            MailboxConnection.revoked_at.is_(None),
        )
    )
    return {
        "email_verified": user.email_verified_at is not None,
        "cv_uploaded": cv_count > 0,
        "profile_confirmed": bool(profile and profile.confirmed_at is not None),
        "preferences_configured": bool(pref and (pref.target_titles_json or pref.role_families_json)),
        "mailbox_connected": mailbox_conn is not None,
        "automation_active": user.automation_stopped_at is None,
    }


def _user_summary(db: Session, user: User, csrf_token: str = "") -> dict[str, object]:
    inst = get_installation_config(db)
    live_sending_enabled, _ = resolve_effective_live_sending(db)
    is_owner = bool(inst and inst.owner_user_id == user.id)
    can_manage_installation = bool(
        is_owner or (user.is_operator and (inst is None or inst.owner_user_id is None))
    )
    setup_state = inst.setup_state if inst else "unclaimed"
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "email_verified": user.email_verified_at is not None,
        "ui_locale": user.ui_locale,
        "timezone": user.timezone,
        "is_operator": user.is_operator,
        "is_owner": is_owner,
        "can_manage_installation": can_manage_installation,
        "setup_state": setup_state,
        "automation_stopped_at": user.automation_stopped_at.isoformat() if user.automation_stopped_at else None,
        "retention_days": user.retention_days,
        "live_sending_enabled_on_server": live_sending_enabled,
        "csrf_token": csrf_token,
        "onboarding": _onboarding_checklist(db, user),
    }


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
def api_register(payload: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    try:
        ensure_valid_timezone(payload.timezone)
        user, raw_verify_token = register_user(
            db,
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name,
            ui_locale=payload.ui_locale,
            timezone_name=payload.timezone,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_TIMEZONE", "message": str(exc)}) from exc

    raw_sess, sess = create_session(
        db,
        user,
        user_agent=request.headers.get("User-Agent", ""),
        ip_address=request.client.host if request.client else "",
    )
    _set_auth_cookies(response, raw_sess, sess.csrf_token)
    out = {"user": _user_summary(db, user, csrf_token=sess.csrf_token)}
    if get_settings().environment == "test":
        out["verification_token_for_test"] = raw_verify_token
    return out


@router.post("/auth/verify-email")
def api_verify_email(payload: TokenVerifyRequest, db: Session = Depends(get_db)) -> dict:
    try:
        user = verify_email_token(db, payload.token)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return {"verified": True, "email": user.email}


@router.post("/auth/resend-verification")
def api_resend_verification(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if user.email_verified_at is not None:
        return {"ok": True, "message": "Email already verified"}
    raw_token = generate_opaque_token(32)
    db.add(
        AuthToken(
            user_id=user.id,
            purpose="email_verify",
            token_hash=hash_token(raw_token),
            expires_at=utcnow() + timedelta(hours=24),
        )
    )
    db.commit()
    verify_url = f"{get_settings().app_url.rstrip('/')}/verify-email?token={raw_token}"
    send_transactional_email(
        user.email,
        "Verify your ApplyFlow Global account",
        f"Verify your email address by opening:\n{verify_url}",
        db=db,
    )
    out: dict[str, object] = {"ok": True, "message": "Verification email dispatched"}
    if get_settings().environment == "test":
        out["verification_token_for_test"] = raw_token
    return out


@router.post("/auth/login")
def api_login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    ip = request.client.host if request.client else ""
    try:
        user = authenticate_user(db, email=payload.email, password=payload.password, ip_address=ip)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    raw_sess, sess = create_session(db, user, user_agent=request.headers.get("User-Agent", ""), ip_address=ip)
    _set_auth_cookies(response, raw_sess, sess.csrf_token)
    return {"user": _user_summary(db, user, csrf_token=sess.csrf_token)}


@router.post("/auth/logout")
def api_logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    revoke_session(db, raw_token)
    _clear_auth_cookies(response)
    return {"ok": True}


@router.post("/auth/forgot-password")
def api_forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> dict:
    tok = issue_password_reset(db, payload.email)
    out: dict[str, object] = {
        "ok": True,
        "message": "If an account exists for that email, a single-use password reset link has been sent.",
    }
    if get_settings().environment == "test" and tok:
        out["reset_token_for_test"] = tok
    return out


@router.post("/auth/reset-password")
def api_reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> dict:
    try:
        reset_password_with_token(db, payload.token, payload.new_password)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return {"ok": True, "message": "Password updated successfully"}


@router.get("/auth/google/start")
def api_google_signin_start(
    response: Response,
    connect_gmail: bool = True,
    include_inbox_readonly: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    try:
        url = build_google_signin_url(
            db,
            connect_gmail=connect_gmail,
            include_inbox_readonly=include_inbox_readonly,
        )
        state = parse_qs(urlsplit(url).query)["state"][0]
        response.set_cookie(
            "applyflow_google_state", state, max_age=900, path="/api/v1/auth/google/callback",
            httponly=True, samesite="lax", secure=get_settings().cookie_secure,
        )
        response.headers["Cache-Control"] = "no-store"
        return {"authorization_url": url}
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.get("/auth/google/callback")
async def api_google_signin_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    app_base = get_settings().app_url.rstrip("/")
    wants_html = "text/html" in (request.headers.get("accept") or "").lower()

    try:
        cookie_state = request.cookies.get("applyflow_google_state", "")
        if not state or not compare_digest(cookie_state.encode(), state.encode()):
            raise AuthError("INVALID_OAUTH_STATE", "Invalid or expired Google sign-in session. Please start again.")
        if error or not code:
            consume_google_state(db, state=state, flow="app_login", user_id=None)
            if error == "access_denied":
                raise AuthError("GOOGLE_CONSENT_DENIED", "Google sign-in was cancelled. You can try again anytime.")
            raise AuthError("GOOGLE_OAUTH_ERROR", "Google sign-in failed. Please start again.")
        user, meta = await complete_google_signin(db, code=code, state=state)
    except AuthError as exc:
        db.rollback()
        db.add(AuditEvent(action="google_signin_failed", target_type="user", details_json={"reason": exc.code}))
        db.commit()
        if wants_html:
            return RedirectResponse(
                url=f"{app_base}/?{urlencode({'auth_error': exc.message, 'auth_error_code': exc.code})}",
                status_code=302,
            )
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc

    raw_sess, sess = create_session(
        db,
        user,
        user_agent=request.headers.get("User-Agent", ""),
        ip_address=request.client.host if request.client else "",
    )
    qs: dict[str, str] = {"google_auth": "connected"}
    if meta.get("gmail_connected"):
        qs["gmail_connected"] = "1"
    redirect = RedirectResponse(url=f"{app_base}/dashboard?{urlencode(qs)}", status_code=302)
    redirect.delete_cookie("applyflow_google_state", path="/api/v1/auth/google/callback")
    _set_auth_cookies(redirect, raw_sess, sess.csrf_token)
    return redirect


@router.get("/auth/me")
def api_me(auth: tuple[UserSession, User] = Depends(get_current_auth), db: Session = Depends(get_db)) -> dict:
    sess, user = auth
    return {"user": _user_summary(db, user, csrf_token=sess.csrf_token)}


@router.patch("/account/settings")
def api_update_account_settings(
    payload: AccountSettingsPatch,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if payload.ui_locale is not None:
        user.ui_locale = payload.ui_locale
    if payload.timezone is not None:
        try:
            ensure_valid_timezone(payload.timezone)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": "INVALID_TIMEZONE", "message": str(exc)}) from exc
        user.timezone = payload.timezone
    if payload.retention_days is not None:
        user.retention_days = payload.retention_days
    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    db.commit()
    return {"user": _user_summary(db, user)}


@router.post("/account/stop-automation")
def api_stop_automation(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    counts = activate_stop_automation(db, user=user)
    return {"ok": True, "stopped": True, **counts}


@router.post("/account/resume-automation")
def api_resume_automation(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    resume_automation(db, user=user)
    return {"ok": True, "stopped": False}


@router.get("/account/export")
def api_export_account(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    bundle = export_user_account_bundle(db, user=user)
    return JSONResponse(
        content=bundle,
        headers={"Content-Disposition": 'attachment; filename="applyflow-account-export.json"'},
    )


@router.delete("/account")
def api_delete_account(
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    res = delete_user_account_and_data(db, user=user)
    _clear_auth_cookies(response)
    return res


@router.get("/account/usage")
def api_account_usage(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    start_month = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = db.scalars(
        select(UsageLedger)
        .where(UsageLedger.user_id == user.id, UsageLedger.created_at >= start_month)
        .order_by(UsageLedger.created_at.desc())
        .limit(100)
    ).all()
    monthly_total = round(sum(r.estimated_cost_usd for r in rows), 4)
    pref = db.scalar(select(SearchPreference).where(SearchPreference.user_id == user.id))
    return {
        "monthly_estimated_cost_usd": monthly_total,
        "monthly_limit_usd": pref.max_monthly_cost_usd if pref else get_settings().max_monthly_user_cost_usd,
        "cost_disclaimer": "Configured estimates using operator env rates; verify actual invoices in provider consoles.",
        "entries": [
            {
                "id": r.id,
                "batch_id": r.batch_id,
                "provider": r.provider,
                "operation": r.operation,
                "units": r.units,
                "estimated_cost_usd": r.estimated_cost_usd,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }
