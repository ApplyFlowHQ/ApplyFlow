"""Shared effective-configuration service with explicit precedence across User, Installation, Environment, and Defaults."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.encryption import decrypt_json, encrypt_json
from app.db.models import InstallationConfig, User, UserIntegrationConfig

SUPPORTED_AI_MODELS: dict[str, list[dict[str, str]]] = {
    "openai": [
        {"id": "gpt-4.1-mini", "label": "GPT-4.1 Mini (Fast, Structured Outputs)"},
        {"id": "gpt-4o-mini", "label": "GPT-4o Mini"},
        {"id": "gpt-4o", "label": "GPT-4o"},
    ],
    "gemini": [
        {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash (Recommended)"},
        {"id": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite (Low Latency)"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
    ],
}

MASK_MARKERS = ("••••", "****", "[CONFIGURED]", "[MASKED]")


def mask_secret(secret: str) -> str:
    val = (secret or "").strip()
    if not val:
        return ""
    if len(val) <= 8:
        return "••••"
    return f"{val[:4]}••••{val[-4:]}"


def is_mask_placeholder(value: str | None) -> bool:
    if value is None:
        return False
    stripped = value.strip()
    if not stripped:
        return False
    return any(m in stripped for m in MASK_MARKERS)


def load_encrypted_secrets(ciphertext: str) -> dict[str, str]:
    if not ciphertext:
        return {}
    raw = decrypt_json(ciphertext)
    return {str(k): str(v) for k, v in raw.items() if v}


def update_encrypted_secrets(
    existing_ciphertext: str,
    updates: dict[str, str | None],
    *,
    remove_keys: set[str] | None = None,
) -> str:
    """
    Atomically update Fernet-encrypted secrets dictionary:
    - None or empty string keeps existing value (unless key is in `remove_keys`).
    - Masking placeholders are NEVER saved as secrets.
    """
    current = load_encrypted_secrets(existing_ciphertext)
    for k in remove_keys or set():
        current.pop(k, None)
    for k, val in updates.items():
        if val is None:
            continue
        cleaned = val.strip()
        if not cleaned or is_mask_placeholder(cleaned):
            continue
        current[k] = cleaned
    if not current:
        return ""
    return encrypt_json(current)


def get_installation_config(db: Session) -> InstallationConfig | None:
    return db.scalar(select(InstallationConfig).where(InstallationConfig.id == "default"))


def get_or_create_installation_config(db: Session) -> InstallationConfig:
    inst = get_installation_config(db)
    if inst is not None:
        return inst
    settings = get_settings()
    inst = InstallationConfig(
        id="default",
        setup_state="unclaimed",
        current_step=1,
        deployment_mode="hosted" if settings.environment == "production" else "local",
        live_sending_enabled=False,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


@dataclass
class EffectiveAiConfig:
    provider: str  # openai | gemini | none
    model: str
    api_key: str
    source: str  # user | installation | environment | env_locked | not_configured
    config_version: int = 0
    locked_by_env: bool = False


@dataclass
class EffectiveSearchConfig:
    provider: str  # tavily | brave | both | none
    tavily_api_key: str
    brave_api_key: str
    source: str  # user | installation | environment | env_locked | not_configured
    config_version: int = 0
    locked_by_env: bool = False


@dataclass
class EffectiveGoogleOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri_auth: str
    redirect_uri_gmail: str
    redirect_uri_sheets: str
    source: str  # installation | environment | env_locked | not_configured
    locked_by_env: bool = False


@dataclass
class EffectiveMsGraphOAuthConfig:
    client_id: str
    client_secret: str
    tenant_id: str
    redirect_uri: str
    source: str  # installation | environment | env_locked | not_configured
    locked_by_env: bool = False


@dataclass
class EffectiveTransactionalEmailConfig:
    provider: str  # console | smtp
    from_email: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_tls: bool
    source: str  # installation | environment | env_locked | default
    locked_by_env: bool = False


def _user_can_use_shared_installation(db: Session, inst: InstallationConfig, user_id: str | None, *, kind: str) -> bool:
    if user_id is None:
        return True
    if inst.owner_user_id == user_id:
        return True
    user = db.scalar(select(User).where(User.id == user_id))
    if user and user.is_operator:
        return True
    if kind == "ai":
        return bool(inst.allow_shared_ai_for_users)
    return bool(inst.allow_shared_search_for_users)


def resolve_effective_ai_config(db: Session, *, user_id: str | None = None) -> EffectiveAiConfig:
    settings = get_settings()
    env_key = settings.openai_api_key if settings.llm_provider == "openai" else settings.gemini_api_key
    env_model = settings.openai_model if settings.llm_provider == "openai" else settings.gemini_model

    if settings.lock_env_config and settings.llm_provider != "none" and env_key:
        return EffectiveAiConfig(
            provider=settings.llm_provider,
            model=env_model,
            api_key=env_key,
            source="env_locked",
            locked_by_env=True,
        )

    # 1. Per-user personal override
    if user_id:
        u_cfg = db.scalar(select(UserIntegrationConfig).where(UserIntegrationConfig.user_id == user_id))
        if u_cfg and u_cfg.ai_provider != "inherit":
            if u_cfg.ai_provider == "none":
                return EffectiveAiConfig(provider="none", model="", api_key="", source="user", config_version=u_cfg.config_version)
            u_secrets = load_encrypted_secrets(u_cfg.encrypted_secrets_json)
            key_name = "openai_api_key" if u_cfg.ai_provider == "openai" else "gemini_api_key"
            key_val = u_secrets.get(key_name, "")
            if key_val:
                default_m = settings.openai_model if u_cfg.ai_provider == "openai" else settings.gemini_model
                return EffectiveAiConfig(
                    provider=u_cfg.ai_provider,
                    model=u_cfg.ai_model or default_m,
                    api_key=key_val,
                    source="user",
                    config_version=u_cfg.config_version,
                )

    # 2. Installation-owned configuration (respects admin sharing policy)
    inst = get_installation_config(db)
    if inst and inst.ai_provider in ("openai", "gemini"):
        if _user_can_use_shared_installation(db, inst, user_id, kind="ai"):
            i_secrets = load_encrypted_secrets(inst.encrypted_secrets_json)
            key_name = "openai_api_key" if inst.ai_provider == "openai" else "gemini_api_key"
            key_val = i_secrets.get(key_name, "")
            if key_val:
                default_m = settings.openai_model if inst.ai_provider == "openai" else settings.gemini_model
                return EffectiveAiConfig(
                    provider=inst.ai_provider,
                    model=inst.ai_model or default_m,
                    api_key=key_val,
                    source="installation",
                    config_version=inst.config_version,
                )

    # 3. Environment fallback (.env compatibility)
    if settings.llm_provider in ("openai", "gemini") and env_key:
        if inst is None or _user_can_use_shared_installation(db, inst, user_id, kind="ai") or inst.owner_user_id is None:
            return EffectiveAiConfig(
                provider=settings.llm_provider,
                model=env_model,
                api_key=env_key,
                source="environment",
            )

    return EffectiveAiConfig(provider="none", model="", api_key="", source="not_configured")


def resolve_effective_search_config(db: Session, *, user_id: str | None = None) -> EffectiveSearchConfig:
    settings = get_settings()
    if settings.lock_env_config and settings.search_provider != "none":
        return EffectiveSearchConfig(
            provider=settings.search_provider,
            tavily_api_key=settings.tavily_api_key,
            brave_api_key=settings.brave_api_key,
            source="env_locked",
            locked_by_env=True,
        )

    # 1. Per-user override
    if user_id:
        u_cfg = db.scalar(select(UserIntegrationConfig).where(UserIntegrationConfig.user_id == user_id))
        if u_cfg and u_cfg.search_provider != "inherit":
            if u_cfg.search_provider == "none":
                return EffectiveSearchConfig(
                    provider="none", tavily_api_key="", brave_api_key="", source="user", config_version=u_cfg.config_version
                )
            u_secrets = load_encrypted_secrets(u_cfg.encrypted_secrets_json)
            tav = u_secrets.get("tavily_api_key", "")
            brv = u_secrets.get("brave_api_key", "")
            if (u_cfg.search_provider == "tavily" and tav) or (u_cfg.search_provider == "brave" and brv) or (
                u_cfg.search_provider == "both" and (tav or brv)
            ):
                return EffectiveSearchConfig(
                    provider=u_cfg.search_provider,
                    tavily_api_key=tav,
                    brave_api_key=brv,
                    source="user",
                    config_version=u_cfg.config_version,
                )

    # 2. Installation-owned configuration
    inst = get_installation_config(db)
    if inst and inst.search_provider in ("tavily", "brave", "both"):
        if _user_can_use_shared_installation(db, inst, user_id, kind="search"):
            i_secrets = load_encrypted_secrets(inst.encrypted_secrets_json)
            tav = i_secrets.get("tavily_api_key", "")
            brv = i_secrets.get("brave_api_key", "")
            if (inst.search_provider == "tavily" and tav) or (inst.search_provider == "brave" and brv) or (
                inst.search_provider == "both" and (tav or brv)
            ):
                return EffectiveSearchConfig(
                    provider=inst.search_provider,
                    tavily_api_key=tav,
                    brave_api_key=brv,
                    source="installation",
                    config_version=inst.config_version,
                )

    # 3. Environment fallback
    if settings.search_provider in ("tavily", "brave", "both") and (settings.tavily_api_key or settings.brave_api_key):
        if inst is None or _user_can_use_shared_installation(db, inst, user_id, kind="search") or inst.owner_user_id is None:
            return EffectiveSearchConfig(
                provider=settings.search_provider,
                tavily_api_key=settings.tavily_api_key,
                brave_api_key=settings.brave_api_key,
                source="environment",
            )

    return EffectiveSearchConfig(provider="none", tavily_api_key="", brave_api_key="", source="not_configured")


def _resolve_oauth_redirect_uri(configured_uri: str, base_api: str, callback_path: str) -> str:
    """
    Resolve the externally reachable OAuth redirect URI.
    If no explicit override is set, or if a legacy `http://localhost:8000` default is present while
    `API_PUBLIC_URL` points to the reverse-proxied origin (e.g. `http://localhost`), derive from `base_api`.
    """
    raw = (configured_uri or "").strip()
    expected = f"{base_api}{callback_path}"
    if not raw:
        return expected
    if raw.startswith("http://localhost:8000/") and not base_api.startswith("http://localhost:8000"):
        return expected
    return raw


def resolve_effective_google_oauth(db: Session) -> EffectiveGoogleOAuthConfig:
    settings = get_settings()
    base_api = settings.api_public_url.rstrip("/")
    red_auth = _resolve_oauth_redirect_uri(settings.google_oauth_redirect_uri_auth, base_api, "/api/v1/auth/google/callback")
    red_gmail = _resolve_oauth_redirect_uri(settings.google_oauth_redirect_uri_gmail, base_api, "/api/v1/mailboxes/gmail/callback")
    red_sheets = _resolve_oauth_redirect_uri(settings.google_oauth_redirect_uri_sheets, base_api, "/api/v1/sheets/callback")

    if settings.lock_env_config and settings.google_oauth_client_id:
        return EffectiveGoogleOAuthConfig(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            redirect_uri_auth=red_auth,
            redirect_uri_gmail=red_gmail,
            redirect_uri_sheets=red_sheets,
            source="env_locked",
            locked_by_env=True,
        )

    inst = get_installation_config(db)
    if inst and inst.google_oauth_client_id:
        i_secrets = load_encrypted_secrets(inst.encrypted_secrets_json)
        secret = i_secrets.get("google_oauth_client_secret", "") or settings.google_oauth_client_secret
        return EffectiveGoogleOAuthConfig(
            client_id=inst.google_oauth_client_id,
            client_secret=secret,
            redirect_uri_auth=red_auth,
            redirect_uri_gmail=red_gmail,
            redirect_uri_sheets=red_sheets,
            source="installation",
        )

    if settings.google_oauth_client_id:
        return EffectiveGoogleOAuthConfig(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            redirect_uri_auth=red_auth,
            redirect_uri_gmail=red_gmail,
            redirect_uri_sheets=red_sheets,
            source="environment",
        )

    return EffectiveGoogleOAuthConfig(
        client_id="",
        client_secret="",
        redirect_uri_auth=red_auth,
        redirect_uri_gmail=red_gmail,
        redirect_uri_sheets=red_sheets,
        source="not_configured",
    )


def resolve_effective_ms_graph_oauth(db: Session) -> EffectiveMsGraphOAuthConfig:
    settings = get_settings()
    base_api = settings.api_public_url.rstrip("/")
    redirect_uri = _resolve_oauth_redirect_uri(settings.ms_graph_redirect_uri, base_api, "/api/v1/mailboxes/ms-graph/callback")

    if settings.lock_env_config and settings.ms_graph_client_id:
        return EffectiveMsGraphOAuthConfig(
            client_id=settings.ms_graph_client_id,
            client_secret=settings.ms_graph_client_secret,
            tenant_id=settings.ms_graph_tenant_id or "common",
            redirect_uri=redirect_uri,
            source="env_locked",
            locked_by_env=True,
        )

    inst = get_installation_config(db)
    if inst and inst.ms_graph_client_id:
        i_secrets = load_encrypted_secrets(inst.encrypted_secrets_json)
        secret = i_secrets.get("ms_graph_client_secret", "") or settings.ms_graph_client_secret
        return EffectiveMsGraphOAuthConfig(
            client_id=inst.ms_graph_client_id,
            client_secret=secret,
            tenant_id=inst.ms_graph_tenant_id or settings.ms_graph_tenant_id or "common",
            redirect_uri=redirect_uri,
            source="installation",
        )

    if settings.ms_graph_client_id:
        return EffectiveMsGraphOAuthConfig(
            client_id=settings.ms_graph_client_id,
            client_secret=settings.ms_graph_client_secret,
            tenant_id=settings.ms_graph_tenant_id or "common",
            redirect_uri=redirect_uri,
            source="environment",
        )

    return EffectiveMsGraphOAuthConfig(
        client_id="",
        client_secret="",
        tenant_id="common",
        redirect_uri=redirect_uri,
        source="not_configured",
    )


def resolve_effective_transactional_email(db: Session) -> EffectiveTransactionalEmailConfig:
    settings = get_settings()
    if settings.lock_env_config and settings.transactional_email_provider == "smtp" and settings.transactional_smtp_host:
        return EffectiveTransactionalEmailConfig(
            provider="smtp",
            from_email=settings.transactional_from_email,
            smtp_host=settings.transactional_smtp_host,
            smtp_port=settings.transactional_smtp_port,
            smtp_username=settings.transactional_smtp_username,
            smtp_password=settings.transactional_smtp_password,
            smtp_use_tls=settings.transactional_smtp_use_tls,
            source="env_locked",
            locked_by_env=True,
        )

    inst = get_installation_config(db)
    if inst and inst.transactional_email_provider == "smtp" and inst.transactional_smtp_host:
        i_secrets = load_encrypted_secrets(inst.encrypted_secrets_json)
        return EffectiveTransactionalEmailConfig(
            provider="smtp",
            from_email=inst.transactional_from_email or settings.transactional_from_email,
            smtp_host=inst.transactional_smtp_host,
            smtp_port=inst.transactional_smtp_port,
            smtp_username=inst.transactional_smtp_username,
            smtp_password=i_secrets.get("transactional_smtp_password", ""),
            smtp_use_tls=inst.transactional_smtp_use_tls,
            source="installation",
        )

    if settings.transactional_email_provider == "smtp" and settings.transactional_smtp_host:
        return EffectiveTransactionalEmailConfig(
            provider="smtp",
            from_email=settings.transactional_from_email,
            smtp_host=settings.transactional_smtp_host,
            smtp_port=settings.transactional_smtp_port,
            smtp_username=settings.transactional_smtp_username,
            smtp_password=settings.transactional_smtp_password,
            smtp_use_tls=settings.transactional_smtp_use_tls,
            source="environment",
        )

    return EffectiveTransactionalEmailConfig(
        provider="console",
        from_email=settings.transactional_from_email,
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_use_tls=True,
        source="default",
    )


def resolve_effective_live_sending(db: Session) -> tuple[bool, str]:
    """
    Resolve effective installation-wide live sending toggle.
    On a claimed installation, `InstallationConfig.live_sending_enabled` controls live sending
    unless `LOCK_ENV_CONFIG=true`. Defaults to False.
    """
    settings = get_settings()
    if settings.lock_env_config:
        return bool(settings.live_sending_enabled), "env_locked"
    inst = get_installation_config(db)
    if inst and inst.owner_user_id is not None:
        return bool(inst.live_sending_enabled), "installation"
    if settings.live_sending_enabled:
        return True, "environment"
    # Never allow an unclaimed installation DB row to implicitly enable live sending
    return False, "default"
