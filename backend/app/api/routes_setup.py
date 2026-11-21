"""Step-by-step installation setup wizard, ownership claim, integration settings, and connection test endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_optional_current_user
from app.api.routes_auth import _set_auth_cookies, _user_summary
from app.db.models import User, UserIntegrationConfig
from app.db.session import get_db
from app.services.auth import create_session
from app.services.connection_tests import (
    ConnectionTestResult,
    send_test_email_to_self,
    test_brave_connection,
    test_gemini_connection,
    test_google_oauth_config,
    test_ms_graph_oauth_config,
    test_openai_connection,
    test_tavily_connection,
    test_transactional_smtp,
    test_user_smtp_imap,
)
from app.services.effective_config import (
    get_installation_config,
    is_mask_placeholder,
    load_encrypted_secrets,
    resolve_effective_ai_config,
    resolve_effective_google_oauth,
    resolve_effective_ms_graph_oauth,
    resolve_effective_search_config,
    resolve_effective_transactional_email,
)
from app.services.setup_service import (
    SetupError,
    claim_installation_owner,
    complete_installation_setup,
    get_setup_status,
    record_installation_validation,
    record_user_validation,
    require_installation_manager,
    save_installation_ai,
    save_installation_google_oauth,
    save_installation_ms_graph_oauth,
    save_installation_search,
    save_installation_transactional_email,
    save_user_integrations,
    set_installation_live_sending,
    update_setup_progress,
)

router = APIRouter(tags=["setup-integrations"])


class SetupClaimRequest(BaseModel):
    setup_code: str = Field(min_length=4, max_length=256)
    email: str | None = Field(default=None, max_length=254)
    password: str | None = Field(default=None, max_length=256)
    full_name: str = Field(default="", max_length=120)
    ui_locale: Literal["en", "ar"] = "en"
    timezone: str = Field(default="UTC", max_length=80)


class SetupProgressRequest(BaseModel):
    current_step: int | None = Field(default=None, ge=1, le=7)
    complete_step: int | None = Field(default=None, ge=1, le=7)
    skip_step: int | None = Field(default=None, ge=1, le=7)
    deployment_mode: Literal["local", "hosted"] | None = None
    default_locale: Literal["en", "ar"] | None = None
    default_timezone: str | None = Field(default=None, max_length=80)
    allow_shared_ai_for_users: bool | None = None
    allow_shared_search_for_users: bool | None = None


class InstallationAiRequest(BaseModel):
    provider: Literal["openai", "gemini", "none"]
    model: str = Field(default="", max_length=80)
    api_key: str | None = Field(default=None, max_length=512)
    remove_api_key: bool = False
    allow_shared_ai_for_users: bool | None = None


class InstallationSearchRequest(BaseModel):
    provider: Literal["tavily", "brave", "both", "none"]
    tavily_api_key: str | None = Field(default=None, max_length=512)
    brave_api_key: str | None = Field(default=None, max_length=512)
    remove_tavily_key: bool = False
    remove_brave_key: bool = False
    allow_shared_search_for_users: bool | None = None


class InstallationGoogleOAuthRequest(BaseModel):
    client_id: str = Field(default="", max_length=255)
    client_secret: str | None = Field(default=None, max_length=512)
    disconnect: bool = False


class InstallationMsGraphRequest(BaseModel):
    client_id: str = Field(default="", max_length=255)
    tenant_id: str = Field(default="common", max_length=128)
    client_secret: str | None = Field(default=None, max_length=512)
    disconnect: bool = False


class InstallationTransactionalEmailRequest(BaseModel):
    provider: Literal["console", "smtp"]
    from_email: str = Field(default="", max_length=254)
    smtp_host: str = Field(default="", max_length=255)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = Field(default="", max_length=254)
    smtp_password: str | None = Field(default=None, max_length=512)
    smtp_use_tls: bool = True
    remove_password: bool = False


class LiveSendingToggleRequest(BaseModel):
    enabled: bool


class UserIntegrationsRequest(BaseModel):
    ai_provider: Literal["inherit", "openai", "gemini", "none"] | None = None
    ai_model: str | None = Field(default=None, max_length=80)
    openai_api_key: str | None = Field(default=None, max_length=512)
    gemini_api_key: str | None = Field(default=None, max_length=512)
    remove_openai_key: bool = False
    remove_gemini_key: bool = False
    search_provider: Literal["inherit", "tavily", "brave", "both", "none"] | None = None
    tavily_api_key: str | None = Field(default=None, max_length=512)
    brave_api_key: str | None = Field(default=None, max_length=512)
    remove_tavily_key: bool = False
    remove_brave_key: bool = False


class ConnectionTestRequest(BaseModel):
    scope: Literal["installation", "user", "mailbox_smtp"] = "installation"
    integration: Literal[
        "openai",
        "gemini",
        "tavily",
        "brave",
        "google_oauth",
        "ms_graph",
        "transactional_email",
        "user_smtp",
    ]
    model: str = Field(default="", max_length=80)
    api_key: str | None = Field(default=None, max_length=512)
    client_id: str = Field(default="", max_length=255)
    client_secret: str | None = Field(default=None, max_length=512)
    tenant_id: str = Field(default="common", max_length=128)
    email_provider: Literal["console", "smtp"] = "smtp"
    from_email: str = Field(default="", max_length=254)
    smtp_host: str = Field(default="", max_length=255)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = Field(default="", max_length=254)
    smtp_password: str | None = Field(default=None, max_length=512)
    smtp_use_tls: bool = True
    imap_host: str = Field(default="", max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_username: str = Field(default="", max_length=254)
    imap_password: str | None = Field(default=None, max_length=512)


def _raise_setup_http(exc: SetupError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.get("/setup/status")
def api_setup_status(
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return get_setup_status(db, user=user)


@router.post("/setup/claim", status_code=status.HTTP_200_OK)
def api_claim_installation(
    payload: SetupClaimRequest,
    request: Request,
    response: Response,
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if "setup_code" in request.query_params or "code" in request.query_params:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CODE_IN_URL_FORBIDDEN",
                "message": "Setup codes must never be transmitted in query parameters or URLs.",
            },
        )
    try:
        owner_user, _ = claim_installation_owner(
            db,
            setup_code=payload.setup_code,
            current_user=current_user,
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name,
            ui_locale=payload.ui_locale,
            timezone_name=payload.timezone,
        )
    except SetupError as exc:
        _raise_setup_http(exc)

    raw_sess, sess = create_session(
        db,
        owner_user,
        user_agent=request.headers.get("User-Agent", ""),
        ip_address=request.client.host if request.client else "",
    )
    _set_auth_cookies(response, raw_sess, sess.csrf_token)
    return {
        "user": _user_summary(db, owner_user, csrf_token=sess.csrf_token),
        "setup": get_setup_status(db, user=owner_user),
    }


@router.patch("/setup/progress")
def api_update_setup_progress(
    payload: SetupProgressRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        update_setup_progress(
            db,
            user=user,
            current_step=payload.current_step,
            complete_step=payload.complete_step,
            skip_step=payload.skip_step,
            deployment_mode=payload.deployment_mode,
            default_locale=payload.default_locale,
            default_timezone=payload.default_timezone,
            allow_shared_ai_for_users=payload.allow_shared_ai_for_users,
            allow_shared_search_for_users=payload.allow_shared_search_for_users,
        )
    except SetupError as exc:
        _raise_setup_http(exc)
    return get_setup_status(db, user=user)


@router.put("/setup/installation/ai")
def api_save_installation_ai(
    payload: InstallationAiRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        save_installation_ai(
            db,
            user=user,
            provider=payload.provider,
            model=payload.model,
            api_key=payload.api_key,
            remove_api_key=payload.remove_api_key,
            allow_shared_ai_for_users=payload.allow_shared_ai_for_users,
        )
    except SetupError as exc:
        _raise_setup_http(exc)
    return get_setup_status(db, user=user)


@router.put("/setup/installation/search")
def api_save_installation_search(
    payload: InstallationSearchRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        save_installation_search(
            db,
            user=user,
            provider=payload.provider,
            tavily_api_key=payload.tavily_api_key,
            brave_api_key=payload.brave_api_key,
            remove_tavily_key=payload.remove_tavily_key,
            remove_brave_key=payload.remove_brave_key,
            allow_shared_search_for_users=payload.allow_shared_search_for_users,
        )
    except SetupError as exc:
        _raise_setup_http(exc)
    return get_setup_status(db, user=user)


@router.put("/setup/installation/google-oauth")
def api_save_installation_google_oauth(
    payload: InstallationGoogleOAuthRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        save_installation_google_oauth(
            db,
            user=user,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            disconnect=payload.disconnect,
        )
    except SetupError as exc:
        _raise_setup_http(exc)
    return get_setup_status(db, user=user)


@router.put("/setup/installation/ms-graph")
def api_save_installation_ms_graph(
    payload: InstallationMsGraphRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        save_installation_ms_graph_oauth(
            db,
            user=user,
            client_id=payload.client_id,
            tenant_id=payload.tenant_id,
            client_secret=payload.client_secret,
            disconnect=payload.disconnect,
        )
    except SetupError as exc:
        _raise_setup_http(exc)
    return get_setup_status(db, user=user)


@router.put("/setup/installation/transactional-email")
def api_save_installation_transactional_email(
    payload: InstallationTransactionalEmailRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        save_installation_transactional_email(
            db,
            user=user,
            provider=payload.provider,
            from_email=payload.from_email,
            smtp_host=payload.smtp_host,
            smtp_port=payload.smtp_port,
            smtp_username=payload.smtp_username,
            smtp_password=payload.smtp_password,
            smtp_use_tls=payload.smtp_use_tls,
            remove_password=payload.remove_password,
        )
    except SetupError as exc:
        _raise_setup_http(exc)
    return get_setup_status(db, user=user)


@router.post("/setup/live-sending")
def api_toggle_live_sending(
    payload: LiveSendingToggleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        set_installation_live_sending(db, user=user, enabled=payload.enabled)
    except SetupError as exc:
        _raise_setup_http(exc)
    return get_setup_status(db, user=user)


@router.post("/setup/complete")
def api_complete_setup(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        complete_installation_setup(db, user=user)
    except SetupError as exc:
        _raise_setup_http(exc)
    return get_setup_status(db, user=user)


@router.put("/integrations/user")
def api_save_user_integrations(
    payload: UserIntegrationsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        save_user_integrations(
            db,
            user=user,
            ai_provider=payload.ai_provider,
            ai_model=payload.ai_model,
            openai_api_key=payload.openai_api_key,
            gemini_api_key=payload.gemini_api_key,
            remove_openai_key=payload.remove_openai_key,
            remove_gemini_key=payload.remove_gemini_key,
            search_provider=payload.search_provider,
            tavily_api_key=payload.tavily_api_key,
            brave_api_key=payload.brave_api_key,
            remove_tavily_key=payload.remove_tavily_key,
            remove_brave_key=payload.remove_brave_key,
        )
    except SetupError as exc:
        _raise_setup_http(exc)
    return get_setup_status(db, user=user)


def _pick_secret_candidate(supplied: str | None, stored: str) -> tuple[str, bool]:
    """Return (secret_to_test, is_unsaved_override)."""
    cleaned = (supplied or "").strip()
    if cleaned and not is_mask_placeholder(cleaned):
        return cleaned, True
    return stored, False


@router.post("/integrations/test")
async def api_test_integration_connection(
    payload: ConnectionTestRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if payload.scope == "installation":
        try:
            inst = require_installation_manager(db, user)
        except SetupError as exc:
            _raise_setup_http(exc)
        inst_secrets = load_encrypted_secrets(inst.encrypted_secrets_json)
    else:
        inst = get_installation_config(db)
        inst_secrets = {}

    if payload.scope == "user" and payload.integration in ("google_oauth", "ms_graph", "transactional_email"):
        raise HTTPException(
            status_code=403,
            detail={"code": "OWNER_REQUIRED", "message": "Only the installation owner can test installation OAuth/SMTP apps."},
        )

    u_cfg = db.scalar(select(UserIntegrationConfig).where(UserIntegrationConfig.user_id == user.id)) if payload.scope == "user" else None
    u_secrets = load_encrypted_secrets(u_cfg.encrypted_secrets_json) if u_cfg else {}

    result: ConnectionTestResult
    tested_unsaved = False

    if payload.integration == "openai":
        if payload.scope == "installation":
            eff = resolve_effective_ai_config(db, user_id=None)
            stored = inst_secrets.get("openai_api_key", "") or (eff.api_key if eff.provider == "openai" else "")
            model = payload.model.strip() or (eff.model if eff.provider == "openai" else "")
        else:
            stored = u_secrets.get("openai_api_key", "")
            model = payload.model.strip() or (u_cfg.ai_model if u_cfg else "")
        candidate, tested_unsaved = _pick_secret_candidate(payload.api_key, stored)
        result = await test_openai_connection(candidate, model=model)
        if not tested_unsaved:
            if payload.scope == "installation":
                record_installation_validation(db, user=user, key="ai", result=result)
            elif u_cfg is not None:
                record_user_validation(db, user=user, key="ai", result=result)

    elif payload.integration == "gemini":
        if payload.scope == "installation":
            eff = resolve_effective_ai_config(db, user_id=None)
            stored = inst_secrets.get("gemini_api_key", "") or (eff.api_key if eff.provider == "gemini" else "")
            model = payload.model.strip() or (eff.model if eff.provider == "gemini" else "")
        else:
            stored = u_secrets.get("gemini_api_key", "")
            model = payload.model.strip() or (u_cfg.ai_model if u_cfg else "")
        candidate, tested_unsaved = _pick_secret_candidate(payload.api_key, stored)
        result = await test_gemini_connection(candidate, model=model)
        if not tested_unsaved:
            if payload.scope == "installation":
                record_installation_validation(db, user=user, key="ai", result=result)
            elif u_cfg is not None:
                record_user_validation(db, user=user, key="ai", result=result)

    elif payload.integration == "tavily":
        if payload.scope == "installation":
            eff_s = resolve_effective_search_config(db, user_id=None)
            stored = inst_secrets.get("tavily_api_key", "") or eff_s.tavily_api_key
        else:
            stored = u_secrets.get("tavily_api_key", "")
        candidate, tested_unsaved = _pick_secret_candidate(payload.api_key, stored)
        result = await test_tavily_connection(candidate)
        if not tested_unsaved:
            if payload.scope == "installation":
                record_installation_validation(db, user=user, key="search", result=result)
            elif u_cfg is not None:
                record_user_validation(db, user=user, key="search", result=result)

    elif payload.integration == "brave":
        if payload.scope == "installation":
            eff_s = resolve_effective_search_config(db, user_id=None)
            stored = inst_secrets.get("brave_api_key", "") or eff_s.brave_api_key
        else:
            stored = u_secrets.get("brave_api_key", "")
        candidate, tested_unsaved = _pick_secret_candidate(payload.api_key, stored)
        result = await test_brave_connection(candidate)
        if not tested_unsaved:
            if payload.scope == "installation":
                record_installation_validation(db, user=user, key="search", result=result)
            elif u_cfg is not None:
                record_user_validation(db, user=user, key="search", result=result)

    elif payload.integration == "google_oauth":
        eff_g = resolve_effective_google_oauth(db)
        cid = payload.client_id.strip() or eff_g.client_id
        csec, tested_unsaved = _pick_secret_candidate(payload.client_secret, eff_g.client_secret)
        result = await test_google_oauth_config(
            cid,
            csec,
            redirect_uri_auth=eff_g.redirect_uri_auth,
            redirect_uri_gmail=eff_g.redirect_uri_gmail,
            redirect_uri_sheets=eff_g.redirect_uri_sheets,
        )
        if not tested_unsaved:
            record_installation_validation(db, user=user, key="google_oauth", result=result)

    elif payload.integration == "ms_graph":
        eff_m = resolve_effective_ms_graph_oauth(db)
        cid = payload.client_id.strip() or eff_m.client_id
        tenant = payload.tenant_id.strip() or eff_m.tenant_id
        csec, tested_unsaved = _pick_secret_candidate(payload.client_secret, eff_m.client_secret)
        result = await test_ms_graph_oauth_config(cid, csec, tenant, redirect_uri=eff_m.redirect_uri)
        if not tested_unsaved:
            record_installation_validation(db, user=user, key="ms_graph", result=result)

    elif payload.integration == "transactional_email":
        eff_tx = resolve_effective_transactional_email(db)
        provider = payload.email_provider or eff_tx.provider
        host = payload.smtp_host.strip() or eff_tx.smtp_host
        port = payload.smtp_port or eff_tx.smtp_port
        username = payload.smtp_username.strip() or eff_tx.smtp_username
        pwd, tested_unsaved = _pick_secret_candidate(payload.smtp_password, eff_tx.smtp_password)
        use_tls = payload.smtp_use_tls if payload.smtp_host.strip() else eff_tx.smtp_use_tls
        from_email = payload.from_email.strip() or eff_tx.from_email
        result = await test_transactional_smtp(
            provider=provider,
            host=host,
            port=port,
            username=username,
            password=pwd,
            use_tls=use_tls,
            from_email=from_email,
        )
        if not tested_unsaved:
            record_installation_validation(db, user=user, key="transactional_email", result=result)

    else:  # user_smtp
        result = await test_user_smtp_imap(
            smtp_host=payload.smtp_host,
            smtp_port=payload.smtp_port,
            smtp_username=payload.smtp_username,
            smtp_password=(payload.smtp_password or "").strip(),
            use_tls=payload.smtp_use_tls,
            imap_host=payload.imap_host,
            imap_port=payload.imap_port,
            imap_username=payload.imap_username,
            imap_password=(payload.imap_password or "").strip(),
        )

    out = result.to_dict()
    out["tested_unsaved_input"] = tested_unsaved
    return out


@router.post("/integrations/send-test-email")
def api_send_test_email_to_self(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        require_installation_manager(db, user)
    except SetupError as exc:
        _raise_setup_http(exc)
    return send_test_email_to_self(db, user=user).to_dict()
