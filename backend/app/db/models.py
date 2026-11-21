"""Normalized SQLAlchemy 2.0 ORM models for ApplyFlow Global."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, new_uuid, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    google_oidc_sub: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    ui_locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    timezone: Mapped[str] = mapped_column(String(80), nullable=False, default="UTC")
    is_operator: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    automation_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=365)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    profile: Mapped["CandidateProfile | None"] = relationship(back_populates="user", cascade="all, delete-orphan")
    preference: Mapped["SearchPreference | None"] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    user_agent: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    ip_prefix: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email_normalized: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    headline: Mapped[str] = mapped_column(String(250), nullable=False, default="")
    contact_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    residence_country_code: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    residence_city: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    residence_geo_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability_note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    work_authorizations_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    nationalities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    residency_permits_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confirmed_languages_json: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False, default=list)
    confirmed_certifications_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    requires_sponsorship: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    willing_to_relocate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    relocation_targets_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    preferred_roles_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    salary_expectation_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_expectation_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    salary_period: Mapped[str] = mapped_column(String(24), nullable=False, default="monthly")
    salary_basis: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    signature_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    portfolio_links_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    consistency_warnings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="profile")


class CvDocument(Base):
    __tablename__ = "cv_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_label: Mapped[str] = mapped_column(String(80), nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    versions: Mapped[list["CvVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="CvVersion.version_number.desc()"
    )


class CvVersion(Base):
    __tablename__ = "cv_versions"
    __table_args__ = (UniqueConstraint("cv_document_id", "version_number", name="uq_cv_doc_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cv_document_id: Mapped[str] = mapped_column(ForeignKey("cv_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    ocr_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_needed")
    parse_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detected_language: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    total_experience_months: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    derived_pdf_storage_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    derived_pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    pdf_conversion_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_needed")
    pdf_conversion_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pdf_page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    document: Mapped["CvDocument"] = relationship(back_populates="versions")
    facts: Mapped[list["CvExtractedFact"]] = relationship(back_populates="cv_version", cascade="all, delete-orphan")


class CvExtractedFact(Base):
    __tablename__ = "cv_extracted_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cv_version_id: Mapped[str] = mapped_column(ForeignKey("cv_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(250), nullable=False, default="")
    organization: Mapped[str] = mapped_column(String(250), nullable=False, default="")
    value: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    start_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proficiency_level: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    confidence: Mapped[str] = mapped_column(String(24), nullable=False, default="evidenced")
    source_page: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    cv_version: Mapped["CvVersion"] = relationship(back_populates="facts")


class SupportingFile(Base):
    __tablename__ = "supporting_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    file_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="portfolio")
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    attach_by_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled_for_matching: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keywords_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class SearchPreference(Base):
    __tablename__ = "search_preferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    target_titles_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    role_families_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    industries_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    seniority_levels_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    excluded_recipients_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    excluded_keywords_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    excluded_companies_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    target_locations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    allowed_remote_modes_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: ["onsite", "hybrid", "remote_country", "remote_region", "remote_worldwide"]
    )
    allow_relocation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    require_sponsorship_friendly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    employment_types_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=lambda: ["full_time"])
    freshness_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    min_match_score: Mapped[int] = mapped_column(Integer, nullable=False, default=65)
    max_experience_gap_years: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    min_salary_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_salary_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    min_salary_period: Mapped[str] = mapped_column(String(24), nullable=False, default="monthly")
    max_weekly_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unknown_salary_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="allow_neutral")
    unknown_dates_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="allow_neutral")
    unknown_location_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="skip")
    unknown_eligibility_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="skip")
    search_languages_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=lambda: ["en"])
    application_language_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="employer_language_if_supported")
    explicit_application_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    email_tone: Mapped[str] = mapped_column(String(32), nullable=False, default="concise_professional")
    email_length_words_min: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    email_length_words_max: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    daily_recipient_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    send_window_start_hhmm: Mapped[str] = mapped_column(String(8), nullable=False, default="08:00")
    send_window_end_hhmm: Mapped[str] = mapped_column(String(8), nullable=False, default="20:00")
    quiet_hours_start_hhmm: Mapped[str] = mapped_column(String(8), nullable=False, default="22:00")
    quiet_hours_end_hhmm: Mapped[str] = mapped_column(String(8), nullable=False, default="07:00")
    timezone: Mapped[str] = mapped_column(String(80), nullable=False, default="UTC")
    max_batch_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    max_monthly_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    max_search_queries_per_batch: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    followup_interval_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    followup_max_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    include_standing_career_invitations: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    campaign_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="dry_run")
    live_consent_granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    live_consent_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    live_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    save_drafts_in_gmail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_goal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    budget_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    require_official_or_authorized_send: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="preference")


class MailboxConnection(Base):
    __tablename__ = "mailbox_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    sender_email: Mapped[str] = mapped_column(String(254), nullable=False)
    authorized_aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    granted_scopes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False, default="")
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    imap_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, default=993)
    imap_use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="connected")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    inbox_poll_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    inbox_last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    inbox_cursor: Mapped[str] = mapped_column(Text, nullable=False, default="")
    inbox_health_status: Mapped[str] = mapped_column(String(32), nullable=False, default="disabled")
    daily_limit_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    save_drafts_in_gmail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_sync_checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    sent_sync_cursor: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    sent_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_earliest_covered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_sync_status: Mapped[str] = mapped_column(String(32), nullable=False, default="disabled")
    sent_sync_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sent_records_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_batch_user_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    schedule_occurrence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mode: Mapped[str] = mapped_column(String(24), nullable=False, default="dry_run")
    run_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="dry_run")
    pipeline_version: Mapped[str] = mapped_column(String(24), nullable=False, default="vNext")
    target_goal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    goal_shortfall_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stage_status_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    budget_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    budget_usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    discovery_stage: Mapped[str | None] = mapped_column(String(40))
    checkpoint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    strategy_used: Mapped[str] = mapped_column(String(64), nullable=False, default="email_application_discovery")
    import_origin: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    source_archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    source_archive_storage_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    query_rotation_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources_searched_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_errors_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    coverage_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates_removed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category_counts_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    skip_reason_counts_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    metrics_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    drafted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uncertain_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pause_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class JobPosting(Base):
    __tablename__ = "job_postings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True)
    canonical_url_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    search_query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    publisher: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    purported_employer: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    validated_employer: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    employer_confidence: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    role_extracted: Mapped[str] = mapped_column(String(250), nullable=False, default="")
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="job_post")
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    company_domain: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    job_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    detected_language: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    location_raw: Mapped[str] = mapped_column(String(250), nullable=False, default="")
    location_confidence: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    country_code: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    remote_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    location_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    employment_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    salary_period: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    salary_basis: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    weekly_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    date_source: Mapped[str] = mapped_column(String(48), nullable=False, default="unknown")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    source_timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    application_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    cc_emails_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    subject_instruction: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    attachment_requirements_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_standing_invitation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discovery_category: Mapped[str] = mapped_column(String(40), nullable=False, default="incomplete_evidence", index=True)
    source_priority_tier: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    access_status: Mapped[str] = mapped_column(String(48), nullable=False, default="unknown")
    parsing_version: Mapped[str] = mapped_column(String(24), nullable=False, default="v3.0")
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    dns_check_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unchecked")
    primary_skip_code: Mapped[str] = mapped_column(String(48), nullable=False, default="", index=True)
    skip_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    secondary_skip_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    skip_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    extraction_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    extraction_evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    eligibility_requirements_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    pipeline_version: Mapped[str] = mapped_column(String(24), nullable=False, default="vNext")
    source_verification_tier: Mapped[str] = mapped_column(
        String(40), nullable=False, default="tier_5_unrelated_or_prohibited", index=True
    )
    corroboration_status: Mapped[str] = mapped_column(String(32), nullable=False, default="uncorroborated")
    corroboration_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    corroboration_evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    requisition_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    normalized_vacancy_key: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    vacancy_confidence: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    research_status: Mapped[str] = mapped_column(String(32), nullable=False, default="evaluated")
    raw_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class JobMatch(Base):
    __tablename__ = "job_matches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True)
    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False, index=True)
    cv_version_id: Mapped[str | None] = mapped_column(ForeignKey("cv_versions.id", ondelete="SET NULL"), nullable=True)
    hard_filter_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hard_filter_failures_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    failure_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    eligibility_check_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    fit_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    score_components_json: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)
    supporting_facts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    missing_requirements_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    decision_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    why_this_cv: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verified_skills_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_gaps_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    attachment_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_selected_cv: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True)
    job_posting_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False, index=True)
    job_match_id: Mapped[str | None] = mapped_column(ForeignKey("job_matches.id", ondelete="SET NULL"), nullable=True)
    mailbox_connection_id: Mapped[str | None] = mapped_column(ForeignKey("mailbox_connections.id", ondelete="SET NULL"), nullable=True)
    selected_cv_version_id: Mapped[str | None] = mapped_column(ForeignKey("cv_versions.id", ondelete="SET NULL"), nullable=True)
    frozen_cv_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    frozen_cv_filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    supporting_file_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    role_title: Mapped[str] = mapped_column(String(250), nullable=False, default="")
    country_code: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    recipient_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    recipient_normalized: Mapped[str] = mapped_column(String(254), nullable=False, default="", index=True)
    cc_emails_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    subject: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    evidence_fact_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    llm_provider_used: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    llm_model_used: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    transport_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    pipeline_version: Mapped[str] = mapped_column(String(24), nullable=False, default="vNext")
    source_verification_tier: Mapped[str] = mapped_column(
        String(40), nullable=False, default="tier_5_unrelated_or_prohibited"
    )
    approval_state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    review_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    review_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    draft_content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    approved_content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attachment_pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    import_origin: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    import_item_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    gmail_draft_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    response_status: Mapped[str] = mapped_column(String(32), nullable=False, default="none", index=True)
    outcome_status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    outcome_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False, default="", index=True)
    provider_thread_id: Mapped[str] = mapped_column(String(255), nullable=False, default="", index=True)
    rfc_message_id: Mapped[str] = mapped_column(String(255), nullable=False, default="", index=True)
    follow_up_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovery_category: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    source_domain: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    primary_skip_code: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    skip_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    secondary_skip_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    skip_or_error_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class DiscoverySourceSighting(Base):
    """Raw search hits, repeated batch sightings, and source-level provenance separated from Applications."""

    __tablename__ = "discovery_source_sightings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True)
    job_posting_id: Mapped[str | None] = mapped_column(ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True, index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    search_query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    canonical_url_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_domain: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    publisher: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    title_raw: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    is_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    dedupe_reason: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    access_status: Mapped[str] = mapped_column(String(48), nullable=False, default="unknown")
    disposition: Mapped[str] = mapped_column(String(48), nullable=False, default="discovered")
    primary_skip_code: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    secondary_skip_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    research_stage: Mapped[str] = mapped_column(String(40), nullable=False, default="discovery_search")
    corroborated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class RecipientHistory(Base):
    __tablename__ = "recipient_histories"
    __table_args__ = (UniqueConstraint("user_id", "normalized_email", name="uq_recipient_user_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    normalized_email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    display_email: Mapped[str] = mapped_column(String(254), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    first_application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"), nullable=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class OutboxEntry(Base):
    __tablename__ = "outbox_entries"
    __table_args__ = (Index("ix_outbox_status_next_attempt", "status", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    mailbox_connection_id: Mapped[str] = mapped_column(ForeignKey("mailbox_connections.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="initial")
    action_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, default=new_uuid)
    rfc_message_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    handoff_outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class BudgetConsumption(Base):
    __tablename__ = "budget_consumptions"
    __table_args__ = (Index("ix_budget_user_consumed", "user_id", "consumed_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    mailbox_connection_id: Mapped[str | None] = mapped_column(ForeignKey("mailbox_connections.id", ondelete="SET NULL"), nullable=True, index=True)
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"), nullable=True)
    outbox_entry_id: Mapped[str | None] = mapped_column(ForeignKey("outbox_entries.id", ondelete="SET NULL"), nullable=True)
    recipient_normalized: Mapped[str] = mapped_column(String(254), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="initial_to")
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MessageEvent(Base):
    __tablename__ = "message_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="internal")
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    rfc_message_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    in_reply_to: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    sender_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    subject: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    classification_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False, default="dry_run")
    days_of_week_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=lambda: [0, 1, 2, 3, 4])
    local_time_hhmm: Mapped[str] = mapped_column(String(8), nullable=False, default="09:00")
    timezone: Mapped[str] = mapped_column(String(80), nullable=False, default="UTC")
    catch_up_window_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ScheduleOccurrence(Base):
    __tablename__ = "schedule_occurrences"
    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_for_utc", name="uq_schedule_occurrence_utc"),
        UniqueConstraint("schedule_id", "local_date_str", name="uq_schedule_occurrence_local_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scheduled_for_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_date_str: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="dispatched")
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("batches.id", ondelete="SET NULL"), nullable=True)
    dst_note: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class UsageLedger(Base):
    __tablename__ = "usage_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("batches.id", ondelete="SET NULL"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class SheetsConnection(Base):
    __tablename__ = "sheets_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    spreadsheet_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    spreadsheet_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sheet_name: Mapped[str] = mapped_column(String(80), nullable=False, default="Applications")
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False, default="")
    granted_scopes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="disconnected")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class SheetsRowMapping(Base):
    __tablename__ = "sheets_row_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, unique=True)
    spreadsheet_id: Mapped[str] = mapped_column(String(200), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    last_synced_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class InstallationConfig(Base):
    """Singleton installation-wide configuration, encrypted provider credentials, and wizard progress."""

    __tablename__ = "installation_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    setup_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unclaimed")
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    completed_steps_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    skipped_steps_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    deployment_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="local")
    default_locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    default_timezone: Mapped[str] = mapped_column(String(80), nullable=False, default="UTC")

    # Explicit admin policy controlling whether ordinary users may consume owner-configured shared AI/search
    allow_shared_ai_for_users: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_shared_search_for_users: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # AI & Search selections (secrets stored encrypted inside encrypted_secrets_json)
    ai_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    ai_model: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    search_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="none")

    # Installation OAuth apps & transactional email settings
    google_oauth_client_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    ms_graph_client_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    ms_graph_tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, default="common")
    transactional_email_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="console")
    transactional_from_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    transactional_smtp_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    transactional_smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    transactional_smtp_username: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    transactional_smtp_use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Explicit installation-wide live sending toggle (off by default even after setup completes)
    live_sending_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Fernet-encrypted JSON for installation secrets; NEVER serialized to API responses or logs
    encrypted_secrets_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Safe per-integration validation metadata (status, last_checked_at, message)
    validation_statuses_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class SetupClaimToken(Base):
    """One-time expiring setup claim code generated by local bootstrap (stored hashed only)."""

    __tablename__ = "setup_claim_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="initial_claim")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class UserIntegrationConfig(Base):
    """Per-user isolated AI & search provider overrides (never shared across tenants)."""

    __tablename__ = "user_integration_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    ai_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="inherit")
    ai_model: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    search_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="inherit")
    encrypted_secrets_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    validation_statuses_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ApplicationTransition(Base):
    """Immutable audit trail of application approval/transport state transitions."""

    __tablename__ = "application_transitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(32), nullable=False, default="approval_state")
    from_state: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    to_state: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class GmailSentRecord(Base):
    """Synced Sent-folder history from user's connected Gmail mailbox for duplicate protection."""

    __tablename__ = "gmail_sent_records"
    __table_args__ = (UniqueConstraint("mailbox_connection_id", "provider_message_id", name="uq_gmail_sent_mbox_msg"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    mailbox_connection_id: Mapped[str] = mapped_column(ForeignKey("mailbox_connections.id", ondelete="CASCADE"), nullable=False, index=True)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rfc_message_id: Mapped[str] = mapped_column(String(255), nullable=False, default="", index=True)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    recipient_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    recipient_normalized: Mapped[str] = mapped_column(String(254), nullable=False, default="", index=True)
    contact_keys_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    recipient_domain: Mapped[str] = mapped_column(String(200), nullable=False, default="", index=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    classification: Mapped[str] = mapped_column(String(32), nullable=False, default="ambiguous")
    confidence_reason: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    is_suppressed_recipient: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
