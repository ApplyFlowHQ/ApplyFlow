"""Setup wizard state machine, atomic ownership claim, credential management, and honest readiness reporting."""

from __future__ import annotations

import hmac
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.encryption import hash_token
from app.core.security import hash_password, normalize_email_dedupe_key, verify_password
from app.core.ssrf import SSRFBlockedError, validate_mail_host
from app.db.base import as_utc, utcnow
from app.db.models import (
    AuditEvent,
    InstallationConfig,
    MailboxConnection,
    SetupClaimToken,
    SheetsConnection,
    User,
    UserIntegrationConfig,
)
from app.services.auth import _create_user_defaults
from app.services.connection_tests import ConnectionTestResult, check_worker_and_scheduler_status
from app.services.dates import ensure_valid_timezone
from app.services.effective_config import (
    SUPPORTED_AI_MODELS,
    get_installation_config,
    get_or_create_installation_config,
    is_mask_placeholder,
    load_encrypted_secrets,
    mask_secret,
    resolve_effective_ai_config,
    resolve_effective_google_oauth,
    resolve_effective_live_sending,
    resolve_effective_ms_graph_oauth,
    resolve_effective_search_config,
    resolve_effective_transactional_email,
    update_encrypted_secrets,
)

CLAIM_MAX_ATTEMPTS = 5
CLAIM_LOCKOUT_MINUTES = 15
ALLOWED_SETUP_STEPS = (1, 2, 3, 4, 5, 6, 7)


class SetupError(ValueError):
    """Structured domain error raised during setup wizard or integration management."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def user_can_manage_installation(inst: InstallationConfig | None, user: User | None) -> bool:
    if user is None:
        return False
    if inst is not None and inst.owner_user_id is not None:
        return inst.owner_user_id == user.id
    return bool(user.is_operator)


def require_installation_manager(db: Session, user: User | None) -> InstallationConfig:
    if user is None:
        raise SetupError("UNAUTHORIZED", "Authentication required.", status_code=401)
    inst = get_or_create_installation_config(db)
    if not user_can_manage_installation(inst, user):
        raise SetupError(
            "OWNER_REQUIRED",
            "Only the installation owner can view or modify installation-wide credentials.",
            status_code=403,
        )
    return inst


def _detect_installation_kind(db: Session, inst: InstallationConfig) -> str:
    if inst.setup_state == "completed":
        return "existing"
    if inst.setup_state == "in_progress" or inst.completed_steps_json or inst.skipped_steps_json:
        return "resumed"
    settings = get_settings()
    has_env_integrations = bool(
        (settings.llm_provider != "none" and (settings.openai_api_key or settings.gemini_api_key))
        or (settings.search_provider != "none" and (settings.tavily_api_key or settings.brave_api_key))
        or settings.google_oauth_client_id
        or settings.ms_graph_client_id
        or settings.transactional_email_provider == "smtp"
    )
    user_count = db.scalar(select(func.count(User.id)).where(User.deleted_at.is_(None))) or 0
    if has_env_integrations or user_count > 0:
        return "existing"
    return "fresh"


def claim_installation_owner(
    db: Session,
    *,
    setup_code: str,
    current_user: User | None = None,
    email: str | None = None,
    password: str | None = None,
    full_name: str = "",
    ui_locale: str = "en",
    timezone_name: str = "UTC",
) -> tuple[User, InstallationConfig]:
    """
    Atomically verify an expiring one-time local bootstrap code and establish the installation owner.
    Never trusts HTTP query strings/URLs and prevents concurrent requests from claiming competing owners.
    """
    inst = get_or_create_installation_config(db)
    if inst.owner_user_id is not None:
        raise SetupError("ALREADY_CLAIMED", "Installation ownership has already been established.", status_code=409)

    cleaned_code = (setup_code or "").strip()
    if not cleaned_code:
        raise SetupError("SETUP_CODE_REQUIRED", "A one-time setup code from the local launcher is required.", status_code=400)

    token_row = db.scalar(
        select(SetupClaimToken)
        .where(SetupClaimToken.consumed_at.is_(None))
        .order_by(SetupClaimToken.created_at.desc())
    )
    if token_row is None:
        raise SetupError(
            "NO_ACTIVE_CLAIM_TOKEN",
            "No active bootstrap setup code exists. Run Start-ApplyFlow.ps1 or cli_bootstrap.py ensure-setup-claim-token.",
            status_code=403,
        )

    now = utcnow()
    locked_until = as_utc(token_row.locked_until)
    if locked_until is not None and locked_until > now:
        raise SetupError(
            "CLAIM_RATE_LIMITED",
            "Too many failed setup code attempts. Wait before retrying or regenerate a fresh code locally.",
            status_code=429,
        )

    expires_at = as_utc(token_row.expires_at)
    if expires_at is not None and expires_at <= now:
        raise SetupError(
            "SETUP_CODE_EXPIRED",
            "The setup code has expired. Generate a fresh code with Start-ApplyFlow.ps1 or cli_bootstrap.py.",
            status_code=410,
        )

    candidate_hash = hash_token(cleaned_code)
    if not hmac.compare_digest(candidate_hash, token_row.token_hash):
        attempts = int(token_row.attempt_count or 0) + 1
        token_row.attempt_count = attempts
        if attempts >= CLAIM_MAX_ATTEMPTS:
            token_row.locked_until = now + timedelta(minutes=CLAIM_LOCKOUT_MINUTES)
        db.commit()
        raise SetupError(
            "INVALID_SETUP_CODE",
            "Invalid setup code. Check the code printed by Start-ApplyFlow.ps1.",
            status_code=403,
        )

    # Resolve or create owner account BEFORE atomic token/installation update so input validation does not burn the code
    try:
        ensure_valid_timezone(timezone_name)
    except ValueError as exc:
        raise SetupError("INVALID_TIMEZONE", str(exc), status_code=400) from exc

    owner_user: User | None = None
    if current_user is not None:
        if current_user.is_suspended or current_user.deleted_at is not None:
            raise SetupError("ACCOUNT_UNAVAILABLE", "Current account cannot claim ownership.", status_code=403)
        owner_user = current_user
        owner_user.is_operator = True
        if owner_user.email_verified_at is None:
            owner_user.email_verified_at = now
        db.flush()
    else:
        cleaned_email = (email or "").strip()
        raw_password = password or ""
        if not cleaned_email or "@" not in cleaned_email:
            raise SetupError("EMAIL_REQUIRED", "A valid owner email address is required.", status_code=400)
        email_norm = normalize_email_dedupe_key(cleaned_email)
        existing = db.scalar(select(User).where(User.email_normalized == email_norm, User.deleted_at.is_(None)))
        if existing is not None:
            if existing.is_suspended:
                raise SetupError("ACCOUNT_SUSPENDED", "This account is suspended.", status_code=403)
            if not raw_password or not verify_password(raw_password, existing.password_hash):
                raise SetupError(
                    "INVALID_CREDENTIALS",
                    "Account already exists; enter its current password to bind installation ownership.",
                    status_code=401,
                )
            existing.is_operator = True
            if existing.email_verified_at is None:
                existing.email_verified_at = now
            owner_user = existing
            db.flush()
        else:
            if len(raw_password) < 10:
                raise SetupError(
                    "WEAK_PASSWORD",
                    "Owner password must be at least 10 characters long.",
                    status_code=400,
                )
            owner_user = User(
                email=cleaned_email.lower(),
                email_normalized=email_norm,
                password_hash=hash_password(raw_password),
                full_name=full_name.strip(),
                ui_locale=ui_locale if ui_locale in ("en", "ar") else "en",
                timezone=timezone_name or "UTC",
                is_operator=True,
                # Owner claiming local bootstrap does not depend on transactional email being configured yet
                email_verified_at=now,
            )
            db.add(owner_user)
            db.flush()
            _create_user_defaults(db, owner_user)
            db.flush()

    # Atomic compare-and-set on both token and singleton installation row
    token_res = db.execute(
        update(SetupClaimToken)
        .where(
            SetupClaimToken.id == token_row.id,
            SetupClaimToken.consumed_at.is_(None),
            SetupClaimToken.expires_at > now,
        )
        .values(consumed_at=now, consumed_by_user_id=owner_user.id)
        .execution_options(synchronize_session=False)
    )
    if token_res.rowcount != 1:
        db.rollback()
        raise SetupError("CLAIM_CONFLICT", "Setup code was already consumed by another request.", status_code=409)

    inst_res = db.execute(
        update(InstallationConfig)
        .where(
            InstallationConfig.id == "default",
            InstallationConfig.owner_user_id.is_(None),
        )
        .values(
            owner_user_id=owner_user.id,
            claimed_at=now,
            setup_state="in_progress",
            default_locale=ui_locale if ui_locale in ("en", "ar") else inst.default_locale,
            default_timezone=timezone_name or inst.default_timezone,
            updated_by_user_id=owner_user.id,
            config_version=InstallationConfig.config_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if inst_res.rowcount != 1:
        db.rollback()
        raise SetupError("ALREADY_CLAIMED", "Installation ownership was claimed concurrently.", status_code=409)

    db.add(
        AuditEvent(
            user_id=owner_user.id,
            actor_user_id=owner_user.id,
            action="installation.owner_claimed",
            target_type="installation",
            target_id="default",
            details_json={"purpose": token_row.purpose},
        )
    )
    db.commit()
    db.refresh(inst)
    db.refresh(owner_user)
    return owner_user, inst


def _merge_step_list(existing: list[int] | None, add_steps: list[int] | None = None, remove_steps: list[int] | None = None) -> list[int]:
    items = {int(s) for s in (existing or []) if int(s) in ALLOWED_SETUP_STEPS}
    for s in add_steps or []:
        if int(s) in ALLOWED_SETUP_STEPS:
            items.add(int(s))
    for s in remove_steps or []:
        items.discard(int(s))
    return sorted(items)


def update_setup_progress(
    db: Session,
    *,
    user: User,
    current_step: int | None = None,
    complete_step: int | None = None,
    skip_step: int | None = None,
    deployment_mode: str | None = None,
    default_locale: str | None = None,
    default_timezone: str | None = None,
    allow_shared_ai_for_users: bool | None = None,
    allow_shared_search_for_users: bool | None = None,
) -> InstallationConfig:
    inst = require_installation_manager(db, user)
    if inst.setup_state == "unclaimed" and inst.owner_user_id is None:
        # An existing authorized operator accessing setup on an upgraded installation claims ownership cleanly
        inst.owner_user_id = user.id
        inst.claimed_at = utcnow()
        inst.setup_state = "in_progress"
    elif inst.setup_state == "unclaimed":
        inst.setup_state = "in_progress"

    if current_step is not None:
        if current_step not in ALLOWED_SETUP_STEPS:
            raise SetupError("INVALID_STEP", "Wizard step must be between 1 and 7.", status_code=400)
        inst.current_step = current_step
    if complete_step is not None:
        inst.completed_steps_json = _merge_step_list(inst.completed_steps_json, add_steps=[complete_step])
        inst.skipped_steps_json = _merge_step_list(inst.skipped_steps_json, remove_steps=[complete_step])
    if skip_step is not None:
        inst.skipped_steps_json = _merge_step_list(inst.skipped_steps_json, add_steps=[skip_step])
        inst.completed_steps_json = _merge_step_list(inst.completed_steps_json, remove_steps=[skip_step])
    if deployment_mode is not None:
        if deployment_mode not in ("local", "hosted"):
            raise SetupError("INVALID_DEPLOYMENT_MODE", "deployment_mode must be 'local' or 'hosted'.", status_code=400)
        inst.deployment_mode = deployment_mode
    if default_locale is not None:
        if default_locale not in ("en", "ar"):
            raise SetupError("INVALID_LOCALE", "default_locale must be 'en' or 'ar'.", status_code=400)
        inst.default_locale = default_locale
    if default_timezone is not None:
        try:
            ensure_valid_timezone(default_timezone)
        except ValueError as exc:
            raise SetupError("INVALID_TIMEZONE", str(exc), status_code=400) from exc
        inst.default_timezone = default_timezone
    if allow_shared_ai_for_users is not None:
        inst.allow_shared_ai_for_users = bool(allow_shared_ai_for_users)
    if allow_shared_search_for_users is not None:
        inst.allow_shared_search_for_users = bool(allow_shared_search_for_users)

    inst.updated_by_user_id = user.id
    inst.config_version = int(inst.config_version or 1) + 1
    db.commit()
    db.refresh(inst)
    return inst


def _set_validation_entry(current: dict[str, Any] | None, key: str, result: ConnectionTestResult | dict[str, Any]) -> dict[str, Any]:
    copied = dict(current or {})
    copied[key] = result.to_dict() if isinstance(result, ConnectionTestResult) else dict(result)
    return copied


def record_installation_validation(db: Session, *, user: User, key: str, result: ConnectionTestResult) -> None:
    inst = require_installation_manager(db, user)
    inst.validation_statuses_json = _set_validation_entry(inst.validation_statuses_json, key, result)
    db.commit()


def record_user_validation(db: Session, *, user: User, key: str, result: ConnectionTestResult) -> None:
    cfg = _get_or_create_user_integration(db, user.id)
    cfg.validation_statuses_json = _set_validation_entry(cfg.validation_statuses_json, key, result)
    db.commit()


def save_installation_ai(
    db: Session,
    *,
    user: User,
    provider: str,
    model: str = "",
    api_key: str | None = None,
    remove_api_key: bool = False,
    allow_shared_ai_for_users: bool | None = None,
) -> InstallationConfig:
    if provider not in ("openai", "gemini", "none"):
        raise SetupError("INVALID_PROVIDER", "AI provider must be 'openai', 'gemini', or 'none'.", status_code=400)
    inst = require_installation_manager(db, user)
    inst.ai_provider = provider
    if model.strip():
        inst.ai_model = model.strip()[:80]
    elif provider == "openai":
        inst.ai_model = inst.ai_model if inst.ai_provider == "openai" and inst.ai_model else get_settings().openai_model
    elif provider == "gemini":
        inst.ai_model = inst.ai_model if inst.ai_provider == "gemini" and inst.ai_model else get_settings().gemini_model
    else:
        inst.ai_model = ""

    if allow_shared_ai_for_users is not None:
        inst.allow_shared_ai_for_users = bool(allow_shared_ai_for_users)

    secret_key = "openai_api_key" if provider == "openai" else ("gemini_api_key" if provider == "gemini" else "")
    remove_keys: set[str] = set()
    updates: dict[str, str | None] = {}
    if remove_api_key:
        if secret_key:
            remove_keys.add(secret_key)
        else:
            remove_keys.update({"openai_api_key", "gemini_api_key"})
    elif secret_key and api_key is not None and api_key.strip() and not is_mask_placeholder(api_key):
        updates[secret_key] = api_key

    inst.encrypted_secrets_json = update_encrypted_secrets(inst.encrypted_secrets_json, updates, remove_keys=remove_keys)
    secrets_map = load_encrypted_secrets(inst.encrypted_secrets_json)
    has_key = bool(secret_key and secrets_map.get(secret_key))

    if provider == "none" or not has_key:
        inst.validation_statuses_json = _set_validation_entry(
            inst.validation_statuses_json,
            "ai",
            {
                "status": "not_configured",
                "message": "AI provider is not configured.",
                "checked_at": utcnow().isoformat(),
            },
        )
    elif updates:
        inst.validation_statuses_json = _set_validation_entry(
            inst.validation_statuses_json,
            "ai",
            {
                "status": "saved_not_tested",
                "message": "Credentials saved securely. Run Test Connection to verify.",
                "checked_at": utcnow().isoformat(),
            },
        )

    inst.updated_by_user_id = user.id
    inst.config_version = int(inst.config_version or 1) + 1
    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="installation.ai_updated",
            target_type="installation",
            target_id="default",
            details_json={"provider": provider, "model": inst.ai_model, "key_removed": remove_api_key, "key_updated": bool(updates)},
        )
    )
    db.commit()
    db.refresh(inst)
    return inst


def save_installation_search(
    db: Session,
    *,
    user: User,
    provider: str,
    tavily_api_key: str | None = None,
    brave_api_key: str | None = None,
    remove_tavily_key: bool = False,
    remove_brave_key: bool = False,
    allow_shared_search_for_users: bool | None = None,
) -> InstallationConfig:
    if provider not in ("tavily", "brave", "both", "none"):
        raise SetupError("INVALID_PROVIDER", "Search provider must be 'tavily', 'brave', 'both', or 'none'.", status_code=400)
    inst = require_installation_manager(db, user)
    inst.search_provider = provider
    if allow_shared_search_for_users is not None:
        inst.allow_shared_search_for_users = bool(allow_shared_search_for_users)

    remove_keys: set[str] = set()
    if remove_tavily_key:
        remove_keys.add("tavily_api_key")
    if remove_brave_key:
        remove_keys.add("brave_api_key")

    updates = {
        "tavily_api_key": tavily_api_key,
        "brave_api_key": brave_api_key,
    }
    inst.encrypted_secrets_json = update_encrypted_secrets(inst.encrypted_secrets_json, updates, remove_keys=remove_keys)
    secrets_map = load_encrypted_secrets(inst.encrypted_secrets_json)
    has_required = (
        (provider == "tavily" and bool(secrets_map.get("tavily_api_key")))
        or (provider == "brave" and bool(secrets_map.get("brave_api_key")))
        or (provider == "both" and bool(secrets_map.get("tavily_api_key") or secrets_map.get("brave_api_key")))
    )
    changed_secret = any(v and v.strip() and not is_mask_placeholder(v) for v in updates.values())
    if provider == "none" or not has_required:
        inst.validation_statuses_json = _set_validation_entry(
            inst.validation_statuses_json,
            "search",
            {
                "status": "not_configured",
                "message": "Automated search provider is not configured.",
                "checked_at": utcnow().isoformat(),
            },
        )
    elif changed_secret:
        inst.validation_statuses_json = _set_validation_entry(
            inst.validation_statuses_json,
            "search",
            {
                "status": "saved_not_tested",
                "message": "Search credentials saved securely. Run Test Connection to verify.",
                "checked_at": utcnow().isoformat(),
            },
        )

    inst.updated_by_user_id = user.id
    inst.config_version = int(inst.config_version or 1) + 1
    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="installation.search_updated",
            target_type="installation",
            target_id="default",
            details_json={
                "provider": provider,
                "tavily_updated": bool(tavily_api_key and not is_mask_placeholder(tavily_api_key)),
                "brave_updated": bool(brave_api_key and not is_mask_placeholder(brave_api_key)),
            },
        )
    )
    db.commit()
    db.refresh(inst)
    return inst


def save_installation_google_oauth(
    db: Session,
    *,
    user: User,
    client_id: str,
    client_secret: str | None = None,
    disconnect: bool = False,
) -> InstallationConfig:
    inst = require_installation_manager(db, user)
    if disconnect:
        inst.google_oauth_client_id = ""
        inst.encrypted_secrets_json = update_encrypted_secrets(
            inst.encrypted_secrets_json, {}, remove_keys={"google_oauth_client_secret"}
        )
        inst.validation_statuses_json = _set_validation_entry(
            inst.validation_statuses_json,
            "google_oauth",
            {"status": "not_configured", "message": "Google OAuth app credentials cleared.", "checked_at": utcnow().isoformat()},
        )
    else:
        inst.google_oauth_client_id = client_id.strip()[:255]
        inst.encrypted_secrets_json = update_encrypted_secrets(
            inst.encrypted_secrets_json,
            {"google_oauth_client_secret": client_secret},
        )
        secrets_map = load_encrypted_secrets(inst.encrypted_secrets_json)
        if inst.google_oauth_client_id and secrets_map.get("google_oauth_client_secret"):
            inst.validation_statuses_json = _set_validation_entry(
                inst.validation_statuses_json,
                "google_oauth",
                {
                    "status": "saved_not_tested",
                    "message": "Google OAuth client credentials saved. Click Test Connection to verify endpoints.",
                    "checked_at": utcnow().isoformat(),
                },
            )
    inst.updated_by_user_id = user.id
    inst.config_version = int(inst.config_version or 1) + 1
    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="installation.google_oauth_updated",
            target_type="installation",
            target_id="default",
            details_json={"disconnected": disconnect, "has_client_id": bool(inst.google_oauth_client_id)},
        )
    )
    db.commit()
    db.refresh(inst)
    return inst


def save_installation_ms_graph_oauth(
    db: Session,
    *,
    user: User,
    client_id: str,
    tenant_id: str = "common",
    client_secret: str | None = None,
    disconnect: bool = False,
) -> InstallationConfig:
    inst = require_installation_manager(db, user)
    if disconnect:
        inst.ms_graph_client_id = ""
        inst.ms_graph_tenant_id = "common"
        inst.encrypted_secrets_json = update_encrypted_secrets(
            inst.encrypted_secrets_json, {}, remove_keys={"ms_graph_client_secret"}
        )
        inst.validation_statuses_json = _set_validation_entry(
            inst.validation_statuses_json,
            "ms_graph",
            {"status": "not_configured", "message": "Microsoft Graph OAuth credentials cleared.", "checked_at": utcnow().isoformat()},
        )
    else:
        inst.ms_graph_client_id = client_id.strip()[:255]
        inst.ms_graph_tenant_id = (tenant_id.strip() or "common")[:128]
        inst.encrypted_secrets_json = update_encrypted_secrets(
            inst.encrypted_secrets_json,
            {"ms_graph_client_secret": client_secret},
        )
        secrets_map = load_encrypted_secrets(inst.encrypted_secrets_json)
        if inst.ms_graph_client_id and secrets_map.get("ms_graph_client_secret"):
            inst.validation_statuses_json = _set_validation_entry(
                inst.validation_statuses_json,
                "ms_graph",
                {
                    "status": "saved_not_tested",
                    "message": "Microsoft Graph client credentials saved.",
                    "checked_at": utcnow().isoformat(),
                },
            )
    inst.updated_by_user_id = user.id
    inst.config_version = int(inst.config_version or 1) + 1
    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="installation.ms_graph_oauth_updated",
            target_type="installation",
            target_id="default",
            details_json={"disconnected": disconnect, "has_client_id": bool(inst.ms_graph_client_id)},
        )
    )
    db.commit()
    db.refresh(inst)
    return inst


def save_installation_transactional_email(
    db: Session,
    *,
    user: User,
    provider: str,
    from_email: str = "",
    smtp_host: str = "",
    smtp_port: int = 587,
    smtp_username: str = "",
    smtp_password: str | None = None,
    smtp_use_tls: bool = True,
    remove_password: bool = False,
) -> InstallationConfig:
    if provider not in ("console", "smtp"):
        raise SetupError("INVALID_PROVIDER", "Transactional email provider must be 'console' or 'smtp'.", status_code=400)
    inst = require_installation_manager(db, user)
    if provider == "smtp":
        cleaned_host = smtp_host.strip()
        if not cleaned_host:
            raise SetupError("SMTP_HOST_REQUIRED", "SMTP host is required when transactional SMTP is selected.", status_code=400)
        try:
            validate_mail_host(cleaned_host, int(smtp_port))
        except SSRFBlockedError as exc:
            raise SetupError("INVALID_SMTP_HOST", str(exc), status_code=400) from exc
        inst.transactional_email_provider = "smtp"
        inst.transactional_from_email = from_email.strip()[:254]
        inst.transactional_smtp_host = cleaned_host[:255]
        inst.transactional_smtp_port = int(smtp_port)
        inst.transactional_smtp_username = smtp_username.strip()[:254]
        inst.transactional_smtp_use_tls = bool(smtp_use_tls)
        remove_keys = {"transactional_smtp_password"} if remove_password else set()
        inst.encrypted_secrets_json = update_encrypted_secrets(
            inst.encrypted_secrets_json,
            {"transactional_smtp_password": smtp_password},
            remove_keys=remove_keys,
        )
        inst.validation_statuses_json = _set_validation_entry(
            inst.validation_statuses_json,
            "transactional_email",
            {
                "status": "saved_not_tested",
                "message": "Transactional SMTP saved. Run Test Connection to verify login.",
                "checked_at": utcnow().isoformat(),
            },
        )
    else:
        inst.transactional_email_provider = "console"
        inst.transactional_from_email = from_email.strip()[:254]
        inst.transactional_smtp_host = ""
        inst.transactional_smtp_username = ""
        if remove_password:
            inst.encrypted_secrets_json = update_encrypted_secrets(
                inst.encrypted_secrets_json, {}, remove_keys={"transactional_smtp_password"}
            )
        inst.validation_statuses_json = _set_validation_entry(
            inst.validation_statuses_json,
            "transactional_email",
            {
                "status": "verified",
                "message": "Console/log transactional mode active (for local or single-owner verification).",
                "checked_at": utcnow().isoformat(),
            },
        )
    inst.updated_by_user_id = user.id
    inst.config_version = int(inst.config_version or 1) + 1
    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="installation.transactional_email_updated",
            target_type="installation",
            target_id="default",
            details_json={"provider": provider, "smtp_host": inst.transactional_smtp_host},
        )
    )
    db.commit()
    db.refresh(inst)
    return inst


def set_installation_live_sending(db: Session, *, user: User, enabled: bool) -> InstallationConfig:
    from app.services.approval import record_application_transition
    from app.services.dedupe import release_reserved_recipients_if_pre_handoff_failed
    from app.services.pre_handoff import cancel_pending_for_applications

    inst = require_installation_manager(db, user)
    if get_settings().lock_env_config:
        raise SetupError(
            "LOCKED_BY_ENV",
            "LOCK_ENV_CONFIG=true is active; live sending is locked by environment configuration.",
            status_code=409,
        )
    was_enabled = bool(inst.live_sending_enabled)
    target_enabled = bool(enabled)
    cancelled_stale_outbox_ids: list[str] = []

    # Never automatically release old pending queued entries when Live Sending is unlocked without fresh approval
    if not was_enabled and target_enabled:
        reason = "Cancelled on Live Sending unlock; fresh approval & confirmation required before sending"
        for app_rec, cancelled_ids, initial in cancel_pending_for_applications(db, reason):
            cancelled_stale_outbox_ids.extend(cancelled_ids)
            if initial:
                release_reserved_recipients_if_pre_handoff_failed(db, user_id=app_rec.user_id, application_id=app_rec.id)
                prev_ap = app_rec.approval_state
                app_rec.transport_status = "draft"
                app_rec.approval_state = "ready_for_approval"
                app_rec.approved_content_hash = ""
                app_rec.approved_at = None
                app_rec.skip_or_error_reason = ""
                record_application_transition(
                    db,
                    user_id=app_rec.user_id,
                    application_id=app_rec.id,
                    field_name="approval_state",
                    from_state=prev_ap,
                    to_state="ready_for_approval",
                    actor=f"user:{user.id}",
                    reason="Old queued entry reset on Live Sending unlock; requires fresh approval before sending",
                )

    inst.live_sending_enabled = target_enabled
    inst.updated_by_user_id = user.id
    inst.config_version = int(inst.config_version or 1) + 1
    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="installation.live_sending_toggled",
            target_type="installation",
            target_id="default",
            details_json={
                "live_sending_enabled": inst.live_sending_enabled,
                "cancelled_stale_outbox_ids": cancelled_stale_outbox_ids,
            },
        )
    )
    db.commit()
    db.refresh(inst)
    return inst


def complete_installation_setup(db: Session, *, user: User) -> InstallationConfig:
    """Finalize setup wizard state cleanly. Never enables live sending or starts any job batch."""
    inst = require_installation_manager(db, user)
    if inst.owner_user_id is None and user.is_operator:
        inst.owner_user_id = user.id
        inst.claimed_at = utcnow()
    inst.setup_state = "completed"
    inst.current_step = 7
    inst.completed_steps_json = _merge_step_list(inst.completed_steps_json, add_steps=[7])
    inst.updated_by_user_id = user.id
    inst.config_version = int(inst.config_version or 1) + 1
    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="installation.setup_completed",
            target_type="installation",
            target_id="default",
            details_json={
                "ai_provider": inst.ai_provider,
                "search_provider": inst.search_provider,
                "live_sending_enabled": inst.live_sending_enabled,
            },
        )
    )
    db.commit()
    db.refresh(inst)
    return inst


def _get_or_create_user_integration(db: Session, user_id: str) -> UserIntegrationConfig:
    cfg = db.scalar(select(UserIntegrationConfig).where(UserIntegrationConfig.user_id == user_id))
    if cfg is not None:
        return cfg
    cfg = UserIntegrationConfig(user_id=user_id, ai_provider="inherit", search_provider="inherit")
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def save_user_integrations(
    db: Session,
    *,
    user: User,
    ai_provider: str | None = None,
    ai_model: str | None = None,
    openai_api_key: str | None = None,
    gemini_api_key: str | None = None,
    remove_openai_key: bool = False,
    remove_gemini_key: bool = False,
    search_provider: str | None = None,
    tavily_api_key: str | None = None,
    brave_api_key: str | None = None,
    remove_tavily_key: bool = False,
    remove_brave_key: bool = False,
) -> UserIntegrationConfig:
    """Save strictly user-scoped personal AI and/or search credentials without affecting other tenants."""
    cfg = _get_or_create_user_integration(db, user.id)
    if ai_provider is not None:
        if ai_provider not in ("inherit", "openai", "gemini", "none"):
            raise SetupError("INVALID_PROVIDER", "User AI provider must be inherit, openai, gemini, or none.", status_code=400)
        cfg.ai_provider = ai_provider
    if ai_model is not None:
        cfg.ai_model = ai_model.strip()[:80]
    if search_provider is not None:
        if search_provider not in ("inherit", "tavily", "brave", "both", "none"):
            raise SetupError("INVALID_PROVIDER", "User search provider must be inherit, tavily, brave, both, or none.", status_code=400)
        cfg.search_provider = search_provider

    remove_keys: set[str] = set()
    if remove_openai_key:
        remove_keys.add("openai_api_key")
    if remove_gemini_key:
        remove_keys.add("gemini_api_key")
    if remove_tavily_key:
        remove_keys.add("tavily_api_key")
    if remove_brave_key:
        remove_keys.add("brave_api_key")

    updates = {
        "openai_api_key": openai_api_key,
        "gemini_api_key": gemini_api_key,
        "tavily_api_key": tavily_api_key,
        "brave_api_key": brave_api_key,
    }
    cfg.encrypted_secrets_json = update_encrypted_secrets(cfg.encrypted_secrets_json, updates, remove_keys=remove_keys)

    if ai_provider == "inherit":
        cfg.validation_statuses_json = _set_validation_entry(
            cfg.validation_statuses_json,
            "ai",
            {"status": "inherit", "message": "Using shared/installation default if permitted.", "checked_at": utcnow().isoformat()},
        )
    elif any(k in updates and updates[k] and not is_mask_placeholder(updates[k]) for k in ("openai_api_key", "gemini_api_key")):
        cfg.validation_statuses_json = _set_validation_entry(
            cfg.validation_statuses_json,
            "ai",
            {"status": "saved_not_tested", "message": "Personal AI key saved.", "checked_at": utcnow().isoformat()},
        )

    if search_provider == "inherit":
        cfg.validation_statuses_json = _set_validation_entry(
            cfg.validation_statuses_json,
            "search",
            {"status": "inherit", "message": "Using shared/installation default if permitted.", "checked_at": utcnow().isoformat()},
        )
    elif any(k in updates and updates[k] and not is_mask_placeholder(updates[k]) for k in ("tavily_api_key", "brave_api_key")):
        cfg.validation_statuses_json = _set_validation_entry(
            cfg.validation_statuses_json,
            "search",
            {"status": "saved_not_tested", "message": "Personal search key saved.", "checked_at": utcnow().isoformat()},
        )

    cfg.config_version = int(cfg.config_version or 1) + 1
    db.add(
        AuditEvent(
            user_id=user.id,
            actor_user_id=user.id,
            action="user.integrations_updated",
            target_type="user_integration_config",
            target_id=cfg.id,
            details_json={"ai_provider": cfg.ai_provider, "search_provider": cfg.search_provider},
        )
    )
    db.commit()
    db.refresh(cfg)
    return cfg


def _build_user_integrations_snapshot(db: Session, user: User) -> dict[str, Any]:
    u_cfg = db.scalar(select(UserIntegrationConfig).where(UserIntegrationConfig.user_id == user.id))
    u_secrets = load_encrypted_secrets(u_cfg.encrypted_secrets_json) if u_cfg else {}
    eff_ai = resolve_effective_ai_config(db, user_id=user.id)
    eff_search = resolve_effective_search_config(db, user_id=user.id)
    validations = u_cfg.validation_statuses_json if u_cfg else {}

    return {
        "ai": {
            "provider": u_cfg.ai_provider if u_cfg else "inherit",
            "model": u_cfg.ai_model if u_cfg else "",
            "has_openai_key": bool(u_secrets.get("openai_api_key")),
            "masked_openai_key": mask_secret(u_secrets.get("openai_api_key", "")),
            "has_gemini_key": bool(u_secrets.get("gemini_api_key")),
            "masked_gemini_key": mask_secret(u_secrets.get("gemini_api_key", "")),
            "effective_provider": eff_ai.provider,
            "effective_model": eff_ai.model,
            "effective_source": eff_ai.source,
            "locked_by_env": eff_ai.locked_by_env,
            "validation": validations.get("ai"),
        },
        "search": {
            "provider": u_cfg.search_provider if u_cfg else "inherit",
            "has_tavily_key": bool(u_secrets.get("tavily_api_key")),
            "masked_tavily_key": mask_secret(u_secrets.get("tavily_api_key", "")),
            "has_brave_key": bool(u_secrets.get("brave_api_key")),
            "masked_brave_key": mask_secret(u_secrets.get("brave_api_key", "")),
            "effective_provider": eff_search.provider,
            "effective_source": eff_search.source,
            "locked_by_env": eff_search.locked_by_env,
            "validation": validations.get("search"),
        },
    }


def _build_readiness_summary(
    db: Session,
    *,
    inst: InstallationConfig | None,
    user: User,
    skipped_steps: list[int],
) -> dict[str, Any]:
    runtime = check_worker_and_scheduler_status(db)
    eff_ai = resolve_effective_ai_config(db, user_id=user.id)
    eff_search = resolve_effective_search_config(db, user_id=user.id)
    eff_tx = resolve_effective_transactional_email(db)
    live_enabled, live_source = resolve_effective_live_sending(db)

    mailboxes = db.scalars(
        select(MailboxConnection).where(MailboxConnection.user_id == user.id, MailboxConnection.revoked_at.is_(None))
    ).all()
    connected_mailboxes = [m for m in mailboxes if m.status == "connected"]
    sheets_conn = db.scalar(select(SheetsConnection).where(SheetsConnection.user_id == user.id))

    inst_validations = inst.validation_statuses_json if inst else {}
    u_cfg = db.scalar(select(UserIntegrationConfig).where(UserIntegrationConfig.user_id == user.id))
    user_validations = u_cfg.validation_statuses_json if u_cfg else {}

    # Determine honest AI readiness status
    if eff_ai.provider == "none":
        ai_status = "skipped" if 2 in skipped_steps else "not_configured"
        ai_msg = "AI provider skipped/not configured. Grounded AI drafting and CV matching are paused; manual notes remain available."
    else:
        val_meta = (
            user_validations.get("ai")
            if eff_ai.source == "user"
            else inst_validations.get("ai")
        ) or {}
        val_state = val_meta.get("status")
        if val_state in ("verified", "needs_attention", "saved_not_tested"):
            ai_status = val_state
            ai_msg = str(val_meta.get("message") or f"{eff_ai.provider} active via {eff_ai.source}.")
        else:
            ai_status = "saved_not_tested"
            ai_msg = f"Using {eff_ai.provider} ({eff_ai.model}) via {eff_ai.source}; run Test Connection to verify."

    # Determine honest Search readiness status
    if eff_search.provider == "none":
        search_status = "skipped" if 3 in skipped_steps else "not_configured"
        search_msg = "Automated search provider is not configured. Manual URL pasting and historical import remain available."
    else:
        val_meta = (
            user_validations.get("search")
            if eff_search.source == "user"
            else inst_validations.get("search")
        ) or {}
        val_state = val_meta.get("status")
        if val_state in ("verified", "needs_attention", "saved_not_tested"):
            search_status = val_state
            search_msg = str(val_meta.get("message") or f"{eff_search.provider} active via {eff_search.source}.")
        else:
            search_status = "saved_not_tested"
            search_msg = f"Using {eff_search.provider} search via {eff_search.source}; run Test Connection to verify."

    # Mailbox & granted capabilities
    if connected_mailboxes:
        primary = next((m for m in connected_mailboxes if m.is_primary), connected_mailboxes[0])
        granted = primary.granted_scopes_json or []
        mailbox_status = "verified"
        mailbox_msg = f"Connected {primary.provider.upper()} mailbox ({primary.sender_email})."
    elif 4 in skipped_steps:
        granted = []
        mailbox_status = "skipped"
        mailbox_msg = "Personal mailbox connection skipped for now. Drafts stay safely in preview until a mailbox is connected."
    else:
        granted = []
        mailbox_status = "not_configured"
        mailbox_msg = "No sending mailbox connected yet."

    # Transactional email
    if eff_tx.provider == "smtp":
        tx_val = inst_validations.get("transactional_email") or {}
        tx_status = tx_val.get("status") or "saved_not_tested"
        tx_msg = str(tx_val.get("message") or f"SMTP configured on {eff_tx.smtp_host}:{eff_tx.smtp_port}.")
    else:
        tx_status = "verified" if get_settings().environment != "production" else "not_configured"
        tx_msg = (
            "Console log sink active (development/local verification links logged locally)."
            if get_settings().environment != "production"
            else "Transactional SMTP not configured for multi-user verification/reset emails."
        )

    # Google Sheets optional integration
    if sheets_conn and sheets_conn.status == "connected":
        sheets_status = "verified"
        sheets_msg = "Google Sheets mirror connected."
    elif 5 in skipped_steps:
        sheets_status = "skipped"
        sheets_msg = "Google Sheets sync skipped. Built-in tracker and CSV/XLSX exports remain ready."
    else:
        sheets_status = "not_configured"
        sheets_msg = "Google Sheets sync not connected (built-in tracker & CSV/XLSX export active)."

    return {
        "database": runtime["database"],
        "worker": runtime["worker"],
        "ai": {"status": ai_status, "provider": eff_ai.provider, "model": eff_ai.model, "source": eff_ai.source, "message": ai_msg},
        "search": {"status": search_status, "provider": eff_search.provider, "source": eff_search.source, "message": search_msg},
        "mailbox": {
            "status": mailbox_status,
            "connected_count": len(connected_mailboxes),
            "granted_scopes": granted,
            "message": mailbox_msg,
        },
        "transactional_email": {"status": tx_status, "provider": eff_tx.provider, "message": tx_msg},
        "optional_sheets": {"status": sheets_status, "message": sheets_msg},
        "live_sending": {
            "status": "verified" if live_enabled else "disabled",
            "enabled": live_enabled,
            "source": live_source,
            "message": (
                "Live sending is ENABLED (respecting daily caps and manual review rules)."
                if live_enabled
                else "Live sending is DISABLED (safe dry-run/draft mode until you explicitly enable it)."
            ),
        },
    }


def get_setup_status(db: Session, *, user: User | None) -> dict[str, Any]:
    """Return strictly non-secret setup wizard status, masked credential hints, and readiness matrix."""
    inst = get_installation_config(db)
    claimed = bool(inst and inst.owner_user_id is not None)
    setup_state = inst.setup_state if inst else "unclaimed"
    installation_kind = _detect_installation_kind(db, inst) if inst else "fresh"
    eff_google = resolve_effective_google_oauth(db)
    google_signin_enabled = bool(eff_google.client_id and eff_google.client_secret)

    # Unauthenticated callers get ONLY safe public bootstrap status (no secrets, no owner email, no hints)
    if user is None:
        return {
            "claimed": claimed,
            "requires_claim": not claimed,
            "setup_state": setup_state,
            "installation_kind": installation_kind,
            "can_manage_installation": False,
            "google_signin_enabled": google_signin_enabled,
        }

    can_manage = user_can_manage_installation(inst, user)
    eff_ms = resolve_effective_ms_graph_oauth(db)
    skipped_steps = list(inst.skipped_steps_json or []) if inst else []
    completed_steps = list(inst.completed_steps_json or []) if inst else []
    readiness = _build_readiness_summary(db, inst=inst, user=user, skipped_steps=skipped_steps)
    user_integrations = _build_user_integrations_snapshot(db, user)

    base_payload: dict[str, Any] = {
        "claimed": claimed,
        "requires_claim": not claimed,
        "setup_state": setup_state,
        "installation_kind": installation_kind,
        "can_manage_installation": can_manage,
        "is_owner": bool(inst and inst.owner_user_id == user.id),
        "google_signin_enabled": google_signin_enabled,
        "current_step": inst.current_step if inst else 1,
        "completed_steps": completed_steps,
        "skipped_steps": skipped_steps,
        "deployment_mode": inst.deployment_mode if inst else ("hosted" if get_settings().environment == "production" else "local"),
        "default_locale": inst.default_locale if inst else "en",
        "default_timezone": inst.default_timezone if inst else "UTC",
        "supported_models": SUPPORTED_AI_MODELS,
        "shared_capabilities": {
            "allow_shared_ai_for_users": bool(inst.allow_shared_ai_for_users) if inst else False,
            "allow_shared_search_for_users": bool(inst.allow_shared_search_for_users) if inst else False,
            "google_oauth_available": bool(eff_google.client_id and eff_google.client_secret),
            "ms_graph_available": bool(eff_ms.client_id and eff_ms.client_secret),
        },
        "user_integrations": user_integrations,
        "readiness": readiness,
    }

    if not can_manage:
        return base_payload

    # Installation manager gets full non-secret metadata + masked credential hints + callback URIs
    i_secrets = load_encrypted_secrets(inst.encrypted_secrets_json) if inst else {}
    eff_ai_inst = resolve_effective_ai_config(db, user_id=None)
    eff_search_inst = resolve_effective_search_config(db, user_id=None)
    eff_tx = resolve_effective_transactional_email(db)
    live_enabled, live_source = resolve_effective_live_sending(db)
    inst_val = inst.validation_statuses_json if inst else {}

    base_payload["installation"] = {
        "config_version": inst.config_version if inst else 0,
        "lock_env_config": get_settings().lock_env_config,
        "allow_shared_ai_for_users": bool(inst.allow_shared_ai_for_users) if inst else False,
        "allow_shared_search_for_users": bool(inst.allow_shared_search_for_users) if inst else False,
        "live_sending_enabled": live_enabled,
        "live_sending_source": live_source,
        "ai": {
            "provider": inst.ai_provider if (inst and inst.ai_provider != "none") else eff_ai_inst.provider,
            "model": (inst.ai_model if inst and inst.ai_provider != "none" else eff_ai_inst.model),
            "effective_provider": eff_ai_inst.provider,
            "effective_source": eff_ai_inst.source,
            "locked_by_env": eff_ai_inst.locked_by_env,
            "has_openai_key": bool(i_secrets.get("openai_api_key")),
            "masked_openai_key": mask_secret(i_secrets.get("openai_api_key", "")),
            "has_gemini_key": bool(i_secrets.get("gemini_api_key")),
            "masked_gemini_key": mask_secret(i_secrets.get("gemini_api_key", "")),
            "validation": inst_val.get("ai"),
        },
        "search": {
            "provider": inst.search_provider if (inst and inst.search_provider != "none") else eff_search_inst.provider,
            "effective_provider": eff_search_inst.provider,
            "effective_source": eff_search_inst.source,
            "locked_by_env": eff_search_inst.locked_by_env,
            "has_tavily_key": bool(i_secrets.get("tavily_api_key")),
            "masked_tavily_key": mask_secret(i_secrets.get("tavily_api_key", "")),
            "has_brave_key": bool(i_secrets.get("brave_api_key")),
            "masked_brave_key": mask_secret(i_secrets.get("brave_api_key", "")),
            "validation": inst_val.get("search"),
        },
        "google_oauth": {
            "client_id": eff_google.client_id,
            "has_client_secret": bool(eff_google.client_secret),
            "masked_client_secret": mask_secret(i_secrets.get("google_oauth_client_secret", "")) or (
                mask_secret(eff_google.client_secret) if eff_google.client_secret else ""
            ),
            "redirect_uri_auth": eff_google.redirect_uri_auth,
            "redirect_uri_gmail": eff_google.redirect_uri_gmail,
            "redirect_uri_sheets": eff_google.redirect_uri_sheets,
            "source": eff_google.source,
            "locked_by_env": eff_google.locked_by_env,
            "validation": inst_val.get("google_oauth"),
        },
        "ms_graph": {
            "client_id": eff_ms.client_id,
            "tenant_id": eff_ms.tenant_id,
            "has_client_secret": bool(eff_ms.client_secret),
            "masked_client_secret": mask_secret(i_secrets.get("ms_graph_client_secret", "")) or (
                mask_secret(eff_ms.client_secret) if eff_ms.client_secret else ""
            ),
            "redirect_uri": eff_ms.redirect_uri,
            "source": eff_ms.source,
            "locked_by_env": eff_ms.locked_by_env,
            "validation": inst_val.get("ms_graph"),
        },
        "transactional_email": {
            "provider": eff_tx.provider,
            "from_email": eff_tx.from_email,
            "smtp_host": eff_tx.smtp_host,
            "smtp_port": eff_tx.smtp_port,
            "smtp_username": eff_tx.smtp_username,
            "smtp_use_tls": eff_tx.smtp_use_tls,
            "has_smtp_password": bool(eff_tx.smtp_password),
            "masked_smtp_password": mask_secret(i_secrets.get("transactional_smtp_password", "")) or (
                mask_secret(eff_tx.smtp_password) if eff_tx.smtp_password else ""
            ),
            "source": eff_tx.source,
            "locked_by_env": eff_tx.locked_by_env,
            "validation": inst_val.get("transactional_email"),
        },
    }
    return base_payload
