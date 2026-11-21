"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime configuration for ApplyFlow Global API and background workers."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "ApplyFlow Global"
    app_url: str = "http://localhost:5173"
    api_public_url: str = "http://localhost:8000"
    environment: Literal["development", "test", "production"] = "development"
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    cookie_secure: bool = False
    session_ttl_hours: int = 168
    data_retention_days_default: int = 365

    # Cryptographic keys
    app_secret_key: str = ""
    app_secret_key_file: str = ""
    mailbox_encryption_key: str = ""
    mailbox_encryption_key_file: str = ""
    postgres_password_file: str = ""

    # Database and Celery/Redis
    database_url: str = "sqlite:///./data/applyflow_dev.db"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    celery_task_always_eager: bool = False

    # Private storage
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_path: str = "./storage/private"
    s3_bucket: str = ""
    s3_region: str = ""
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # Safety & Operator Limits
    live_sending_enabled: bool = False
    lock_env_config: bool = False
    operator_admin_emails: str = ""
    max_daily_recipients_default: int = 20
    max_daily_recipients_hard_cap: int = 50
    max_user_concurrent_batches: int = 2
    model_timeout_seconds: float = Field(default=25.0, ge=1, le=90)
    search_timeout_seconds: float = Field(default=20.0, ge=1, le=60)
    max_batch_search_cost_usd: float = 1.50
    max_monthly_user_cost_usd: float = 15.00
    inbox_stale_minutes: int = 180
    login_rate_limit_per_15m: int = 15
    cv_max_upload_bytes: int = 10 * 1024 * 1024  # 10 MB
    cv_max_pages: int = 15
    cv_max_decompressed_bytes: int = 25 * 1024 * 1024

    # AI Providers
    llm_provider: Literal["openai", "gemini", "none"] = "none"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"

    # Search Providers
    search_provider: Literal["tavily", "brave", "both", "none"] = "none"
    tavily_api_key: str = ""
    brave_api_key: str = ""

    # Configurable cost rates (labeled estimates)
    cost_rate_openai_input_per_1k: float = 0.0004
    cost_rate_openai_output_per_1k: float = 0.0016
    cost_rate_gemini_input_per_1k: float = 0.0003
    cost_rate_gemini_output_per_1k: float = 0.0012
    cost_rate_tavily_per_call: float = 0.008
    cost_rate_brave_per_call: float = 0.005

    # Google OAuth 2.0 (empty defaults derive dynamically from api_public_url)
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri_auth: str = ""
    google_oauth_redirect_uri_gmail: str = ""
    google_oauth_redirect_uri_sheets: str = ""

    # Microsoft Graph OAuth 2.0
    ms_graph_client_id: str = ""
    ms_graph_client_secret: str = ""
    ms_graph_tenant_id: str = "common"
    ms_graph_redirect_uri: str = ""

    # Transactional email
    transactional_email_provider: Literal["console", "smtp"] = "console"
    transactional_from_email: str = "no-reply@example.invalid"
    transactional_smtp_host: str = ""
    transactional_smtp_port: int = 587
    transactional_smtp_username: str = ""
    transactional_smtp_password: str = ""
    transactional_smtp_use_tls: bool = True

    # OCR & FX
    ocr_provider: Literal["none", "tesseract"] = "none"
    tesseract_cmd: str = "tesseract"
    fx_provider: Literal["none", "frankfurter", "static"] = "none"
    fx_api_url: str = "https://api.frankfurter.app"
    fx_static_rates_json: str = ""

    # Reverse proxy / domain
    https_domain: str = "localhost"
    acme_email: str = "ops@example.invalid"

    # Outbox / worker tuning
    outbox_lease_seconds: int = 120
    send_pace_seconds: float = 2.0

    @field_validator("database_url")
    @classmethod
    def normalize_sqlite_dir(cls, value: str) -> str:
        if value.startswith("sqlite:///"):
            raw_path = value.removeprefix("sqlite:///")
            if raw_path and raw_path != ":memory:":
                Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
        return value

    @model_validator(mode="after")
    def load_secret_files(self) -> "Settings":
        if self.app_secret_key_file and Path(self.app_secret_key_file).is_file():
            raw = Path(self.app_secret_key_file).read_text(encoding="utf-8").strip()
            if raw:
                self.app_secret_key = raw
        if self.mailbox_encryption_key_file and Path(self.mailbox_encryption_key_file).is_file():
            raw = Path(self.mailbox_encryption_key_file).read_text(encoding="utf-8").strip()
            if raw:
                self.mailbox_encryption_key = raw
        if self.postgres_password_file and Path(self.postgres_password_file).is_file():
            pg_pw = Path(self.postgres_password_file).read_text(encoding="utf-8").strip()
            if pg_pw and "__POSTGRES_PASSWORD__" in self.database_url:
                self.database_url = self.database_url.replace("__POSTGRES_PASSWORD__", quote_plus(pg_pw))
        if not self.app_secret_key:
            raise ValueError("Set APP_SECRET_KEY or mount APP_SECRET_KEY_FILE before starting ApplyFlow")
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def operator_emails_set(self) -> set[str]:
        return {e.strip().lower() for e in self.operator_admin_emails.split(",") if e.strip()}

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
