"""Pydantic v2 request and response schemas for ApplyFlow Global API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(default="", max_length=200)
    ui_locale: Literal["en", "ar"] = "en"
    timezone: str = Field(default="UTC", max_length=80)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenVerifyRequest(BaseModel):
    token: str = Field(min_length=10, max_length=256)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=256)
    new_password: str = Field(min_length=10, max_length=128)


class AccountSettingsPatch(BaseModel):
    ui_locale: Literal["en", "ar"] | None = None
    timezone: str | None = Field(default=None, max_length=80)
    retention_days: int | None = Field(default=None, ge=30, le=3650)
    full_name: str | None = Field(default=None, max_length=200)


class PortfolioLinkItem(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=4, max_length=500)
    enabled: bool = True


class ConfirmedLanguageItem(BaseModel):
    language: str = Field(min_length=1, max_length=40)
    level: str = Field(default="fluent", max_length=32)


class CandidateProfileUpdate(BaseModel):
    full_name: str = Field(default="", max_length=200)
    headline: str = Field(default="", max_length=250)
    contact_email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=64)
    residence_country_code: str = Field(default="", max_length=8)
    residence_city: str = Field(default="", max_length=120)
    residence_geo_id: str = Field(default="", max_length=64)
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    availability_note: str = Field(default="", max_length=300)
    work_authorizations: list[str] = Field(default_factory=list)
    nationalities: list[str] = Field(default_factory=list)
    residency_permits: list[str] = Field(default_factory=list)
    confirmed_languages: list[ConfirmedLanguageItem] = Field(default_factory=list)
    confirmed_certifications: list[str] = Field(default_factory=list)
    requires_sponsorship: bool | None = None
    willing_to_relocate: bool = False
    relocation_targets: list[str] = Field(default_factory=list)
    preferred_roles: list[str] = Field(default_factory=list)
    salary_expectation_min: float | None = Field(default=None, ge=0)
    salary_expectation_max: float | None = Field(default=None, ge=0)
    salary_currency: str = Field(default="", max_length=8)
    salary_period: Literal["hourly", "daily", "weekly", "monthly", "annual"] = "monthly"
    salary_basis: Literal["gross", "net", "unknown"] = "unknown"
    signature_text: str = Field(default="", max_length=1500)
    portfolio_links: list[PortfolioLinkItem] = Field(default_factory=list)
    confirm_profile: bool = True


class TargetLocationItem(BaseModel):
    geo_id: str = ""
    country_code: str = ""
    country_name: str = ""
    city: str = ""
    label: str = ""


class SearchPreferenceUpdate(BaseModel):
    target_titles: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    seniority_levels: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    excluded_recipients: list[str] | None = Field(default=None, max_length=1000)
    target_locations: list[TargetLocationItem] = Field(default_factory=list)
    allowed_remote_modes: list[
        Literal["onsite", "hybrid", "remote_country", "remote_region", "remote_worldwide"]
    ] = Field(default_factory=lambda: ["onsite", "hybrid", "remote_country", "remote_region", "remote_worldwide"])
    allow_relocation: bool = False
    require_sponsorship_friendly: bool = False
    employment_types: list[str] = Field(default_factory=lambda: ["full_time"])
    freshness_days: int = Field(default=14, ge=1, le=90)
    min_match_score: int = Field(default=65, ge=0, le=100)
    max_experience_gap_years: int = Field(default=2, ge=0, le=15)
    min_salary_amount: float | None = Field(default=None, ge=0)
    min_salary_currency: str = Field(default="", max_length=8)
    min_salary_period: Literal["hourly", "daily", "weekly", "monthly", "annual"] = "monthly"
    max_weekly_hours: int | None = Field(default=None, ge=1, le=80)
    unknown_salary_policy: Literal["allow_neutral", "skip", "require_review"] = "allow_neutral"
    unknown_dates_policy: Literal["allow_neutral", "skip"] = "allow_neutral"
    unknown_location_policy: Literal["skip", "allow_if_remote_worldwide", "require_review"] = "skip"
    unknown_eligibility_policy: Literal["skip", "allow_neutral", "require_review"] = "skip"
    search_languages: list[str] = Field(default_factory=lambda: ["en"])
    application_language_mode: Literal["explicit", "employer_language_if_supported"] = "employer_language_if_supported"
    explicit_application_language: str = Field(default="en", max_length=16)
    email_tone: Literal["concise_professional", "warm_collaborative", "executive"] = "concise_professional"
    email_length_words_min: int = Field(default=120, ge=60, le=300)
    email_length_words_max: int = Field(default=180, ge=80, le=500)
    batch_size: int = Field(default=10, ge=1, le=50)
    daily_recipient_limit: int = Field(default=20, ge=1, le=50)
    send_window_start_hhmm: str = Field(default="08:00", pattern=r"^\d{2}:\d{2}$")
    send_window_end_hhmm: str = Field(default="20:00", pattern=r"^\d{2}:\d{2}$")
    quiet_hours_start_hhmm: str = Field(default="22:00", pattern=r"^\d{2}:\d{2}$")
    quiet_hours_end_hhmm: str = Field(default="07:00", pattern=r"^\d{2}:\d{2}$")
    timezone: str = Field(default="UTC", max_length=80)
    max_batch_cost_usd: float = Field(default=1.0, ge=0.05, le=20.0)
    max_monthly_cost_usd: float = Field(default=10.0, ge=0.1, le=200.0)
    max_search_queries_per_batch: int = Field(default=6, ge=1, le=20)
    followup_interval_days: int = Field(default=7, ge=2, le=30)
    followup_max_count: int = Field(default=1, ge=0, le=2)
    include_standing_career_invitations: bool = False
    campaign_mode: Literal["dry_run", "approval_required", "autopilot"] = "dry_run"
    live_consent_granted: bool = False
    live_consent_version: str = Field(default="vNext-1", max_length=32)
    save_drafts_in_gmail: bool = False
    target_goal_count: int = Field(default=10, ge=1, le=50)
    budget_config: dict[str, Any] = Field(default_factory=dict)
    require_official_or_authorized_send: bool = True


class SmtpMailboxConnectRequest(BaseModel):
    label: str = Field(default="SMTP Mailbox", max_length=120)
    sender_email: EmailStr
    username: str = Field(default="", max_length=254)
    password: str = Field(min_length=1, max_length=512)
    smtp_host: str = Field(min_length=3, max_length=255)
    smtp_port: int = Field(default=587)
    smtp_use_tls: bool = True
    imap_host: str = Field(default="", max_length=255)
    imap_port: int = Field(default=993)
    imap_use_tls: bool = True
    inbox_poll_enabled: bool = False
    authorized_aliases: list[str] = Field(default_factory=list)


class PastedPostingInput(BaseModel):
    title: str = Field(min_length=2, max_length=240)
    company_name: str = Field(default="", max_length=200)
    source_url: str = Field(default="", max_length=1000)
    location_raw: str = Field(default="", max_length=250)
    raw_text: str = Field(min_length=20, max_length=25000)


class StartBatchRequest(BaseModel):
    mode: Literal["dry_run", "live", "demo_fixture", "approval_required", "autopilot"] = "dry_run"
    run_mode: Literal["dry_run", "approval_required", "autopilot", "demo_fixture"] | None = None
    target_goal_count: int | None = Field(default=None, ge=1, le=50)
    max_search_queries: int | None = Field(default=None, ge=1, le=30)
    include_standing_invitations: bool | None = None
    budget_config: dict[str, Any] | None = None
    pasted_posting: PastedPostingInput | None = None
    run_immediately: bool = True


class DraftReviseRequest(BaseModel):
    subject: str | None = Field(default=None, min_length=2, max_length=300)
    body_text: str | None = Field(default=None, min_length=20, max_length=15000)
    recipient_email: str | None = Field(default=None, max_length=254)
    cc_emails: list[str] | None = None
    selected_cv_version_id: str | None = Field(default=None, max_length=36)
    review_notes: str | None = Field(default=None, max_length=4000)


class DraftApproveRequest(BaseModel):
    expected_hashes: dict[str, str] = Field(default_factory=dict, max_length=100)
    application_ids: list[str] = Field(min_length=1, max_length=100)
    review_notes: str = Field(default="", max_length=4000)
    allow_third_party_override: bool = False


class DraftSendApprovedRequest(BaseModel):
    expected_mailbox_id: str = Field(default="", max_length=64)
    acknowledge_incomplete_history: bool = False
    application_ids: list[str] = Field(min_length=1, max_length=100)
    expected_hashes: dict[str, str] = Field(default_factory=dict, max_length=100)
    dispatch_immediately: bool = Field(default=False, deprecated=True, description="Legacy input. Confirmed sends are queued for the worker.")


class RetrySendRequest(BaseModel):
    bypass_send_window: bool = False


class CancelOutboxRequest(BaseModel):
    reason: str = Field(default="Cancelled by user from dashboard", max_length=500)


class ReconcileOutboxRequest(BaseModel):
    mark_failed_if_not_found: bool = False


class MailboxSelfTestRequest(BaseModel):
    explicit_confirm: bool = Field(default=False)
    recipient_email: str | None = Field(default=None, max_length=254)
    cv_version_id: str | None = Field(default=None, max_length=36)
    subject: str = Field(default="ApplyFlow Connected Mailbox Self-Test", min_length=3, max_length=200)
    note: str = Field(default="", max_length=1000)


class ScheduleCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    mode: Literal["dry_run", "live"] = "dry_run"
    days_of_week: list[int] = Field(min_length=1, max_length=7)
    local_time_hhmm: str = Field(pattern=r"^\d{2}:\d{2}$")
    timezone: str = Field(default="UTC", max_length=80)
    catch_up_window_minutes: int = Field(default=120, ge=5, le=720)
    is_active: bool = True


class SchedulePatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    mode: Literal["dry_run", "live"] | None = None
    days_of_week: list[int] | None = None
    local_time_hhmm: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    timezone: str | None = Field(default=None, max_length=80)
    is_active: bool | None = None


class ApplicationPatchRequest(BaseModel):
    outcome_status: Literal["open", "interview", "rejected", "withdrawn", "archived"] | None = None
    notes: str | None = Field(default=None, max_length=4000)


class SupportingFilePatchRequest(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    attach_by_default: bool | None = None
    enabled_for_matching: bool | None = None
    keywords: list[str] | None = None


class OkResponse(BaseModel):
    ok: bool = True
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
