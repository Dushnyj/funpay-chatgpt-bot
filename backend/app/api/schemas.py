from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    password: str


class StatusResponse(BaseModel):
    status: str


# --- Catalog ---

class TierOut(_Base):
    id: int
    name: str
    code: str | None = None
    description: str | None = None
    is_active: bool
    system_managed: bool = True
    is_sellable: bool = False
    # Read-only technical capability of FunPay's current ChatGPT offer form.
    # Canonical plans can still be recognized when this capability is false.
    funpay_supported: bool = False
    sort_order: int = 0
    usage_multiplier: float | None = None


class TierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool = True


class TierUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    is_sellable: bool | None = None

    @field_validator("is_active", "is_sellable", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class DurationOut(_Base):
    id: int
    minutes: int
    is_enabled: bool
    sort_order: int


class DurationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minutes: int = Field(strict=True, ge=30, le=43_200, multiple_of=30)
    is_enabled: bool = True


class DurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    is_enabled: bool | None = None

    @field_validator("is_enabled", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not (self.model_fields_set - {"id"}):
            raise ValueError("at least one editable field must be provided")
        return self


class DurationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_enabled: bool | None = None

    @field_validator("is_enabled", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class LimitScopeOut(_Base):
    id: int
    code: str
    name: str
    is_enabled: bool = True
    sort_order: int = 0


class LimitScopeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_enabled: bool | None = None

    @field_validator("is_enabled", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


# --- Accounts ---

class ValidationJobOut(BaseModel):
    id: int
    status: str
    job_type: str
    priority: str
    stage: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AccountOut(_Base):
    id: int
    login: str
    tier_id: int | None = None
    email: str | None = None
    subscription_expires_at: datetime | None = None
    subscription_expiry_source: str | None = None
    max_active_rentals: int | None = None
    active_rentals_count: int = 0
    replacement_reserved: bool = False
    status: str
    operator_status_override: str | None = None
    notes: str | None = None
    plan_raw_type: str | None = None
    plan_source: str | None = None
    plan_confidence: float | None = None
    plan_detected_at: datetime | None = None
    email_oauth_connected: bool = False
    email_oauth_provider: str | None = None
    email_oauth_status: str | None = None
    validation_job: ValidationJobOut | None = None


class AccountCreate(BaseModel):
    login: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=4096)
    totp_secret: str = Field(default="", max_length=256)
    email: str | None = Field(default=None, max_length=320)
    email_password: str | None = Field(default=None, max_length=4096)
    max_active_rentals: int | None = Field(default=None, ge=1, le=1)
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="before")
    @classmethod
    def reject_manual_subscription_expiry(cls, value):
        if isinstance(value, dict) and "subscription_expires_at" in value:
            raise ValueError(
                "subscription expiry is measured automatically by OpenAI"
            )
        return value


class AccountUpdate(BaseModel):
    max_active_rentals: int | None = Field(default=None, ge=1, le=1)
    # A human operator may suspend an account, but cannot certify credentials.
    # Returning to active always goes through the recheck endpoint.
    status: Literal["maintenance", "disabled"] | None = None
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="before")
    @classmethod
    def reject_manual_subscription_expiry(cls, value):
        if isinstance(value, dict) and "subscription_expires_at" in value:
            raise ValueError(
                "subscription expiry is measured automatically by OpenAI"
            )
        return value

    @field_validator("status", mode="before")
    @classmethod
    def reject_null_status(cls, value):
        if value is None:
            raise ValueError("status cannot be null")
        return value


class AccountCredentialsUpdate(BaseModel):
    """Write-only credential repair payload.

    Omitted fields stay unchanged. Nullable optional credentials use an
    explicit ``null`` as the only clear operation, so an accidentally blank
    form never destroys a stored secret.
    """

    login: str | None = Field(default=None, min_length=1, max_length=320)
    password: str | None = None
    totp_secret: str | None = None
    email: str | None = Field(default=None, min_length=1, max_length=320)
    email_password: str | None = None

    @field_validator("login", "email", mode="before")
    @classmethod
    def normalize_identity(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("at least one credential field is required")
        return self


class AccountLimitsOut(_Base):
    account_id: int
    codex_5h_remaining_pct: int | None = None
    codex_weekly_remaining_pct: int | None = None
    codex_primary_remaining_pct: int | None = None
    codex_primary_window_seconds: int | None = None
    codex_primary_resets_at: datetime | None = None
    codex_secondary_remaining_pct: int | None = None
    codex_secondary_window_seconds: int | None = None
    codex_secondary_resets_at: datetime | None = None
    refresh_status: str
    measured_at: datetime | None = None
    plan_type: str | None = None
    plan_window_status: str = "unknown"
    expected_long_window_seconds: int | None = None

    @field_validator(
        "codex_primary_resets_at",
        "codex_secondary_resets_at",
        mode="before",
    )
    @classmethod
    def normalize_observed_reset_timezone(
        cls, value: datetime | None
    ) -> datetime | None:
        # SQLite drops timezone metadata in tests and local installs. The API
        # contract is UTC, matching the OpenAI Unix/ISO timestamps.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class AccountWithLimits(AccountOut):
    limits: AccountLimitsOut | None = None


class DeviceAuthStartOut(BaseModel):
    session_id: str
    verification_url: str
    user_code: str
    expires_at: datetime
    interval_seconds: int


class DeviceAuthStatusOut(BaseModel):
    status: Literal["pending", "completed", "failed", "expired"]
    error_code: str | None = None
    error_detail: str | None = None
    account: AccountOut | None = None


# --- Price Matrix ---

class PriceMatrixItem(BaseModel):
    tier_id: int = Field(gt=0)
    duration_id: int = Field(gt=0)
    limit_scope_id: int = Field(gt=0)
    min_limit_pct: int | None = Field(default=None, ge=0, le=100)
    max_5h_pct: int | None = Field(default=None, ge=0, le=100)
    max_weekly_pct: int | None = Field(default=None, ge=0, le=100)
    price: int = Field(gt=0, le=100_000_000)


class PriceMatrixUpdate(BaseModel):
    items: list[PriceMatrixItem] = Field(max_length=10_000)


# --- Templates ---

class TemplateOut(_Base):
    key: str
    lang: str
    content: str
    allowed_fields: list[str] = Field(default_factory=list)
    default_content: str | None = None
    is_custom: bool = False


class TemplateItem(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    lang: Literal["ru", "en"]
    content: str = Field(min_length=1, max_length=4_000)


class TemplateUpdate(BaseModel):
    items: list[TemplateItem] = Field(min_length=1, max_length=500)


class LotTemplateOut(BaseModel):
    id: int
    key: str
    name: str
    tier_id: int | None = None
    limit_scope_id: int | None = None
    title_ru: str
    title_en: str
    description_ru: str
    description_en: str
    enabled: bool
    system_managed: bool
    is_custom: bool
    default_title_ru: str | None = None
    default_title_en: str | None = None
    default_description_ru: str | None = None
    default_description_en: str | None = None
    allowed_fields: list[str] = Field(default_factory=list)


class LotTemplateCreate(BaseModel):
    key: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    tier_id: int | None = Field(default=None, gt=0)
    limit_scope_id: int | None = Field(default=None, gt=0)
    title_ru: str = Field(min_length=1, max_length=255)
    title_en: str = Field(min_length=1, max_length=255)
    description_ru: str = Field(default="", max_length=4_000)
    description_en: str = Field(default="", max_length=4_000)
    enabled: bool = True

    @field_validator("key", "name", mode="before")
    @classmethod
    def strip_lot_template_identity(cls, value):
        # Normalize before Field length checks so whitespace-only identities
        # cannot be persisted as an empty template name or key.
        return value.strip() if isinstance(value, str) else value


class LotTemplateUpdate(BaseModel):
    title_ru: str = Field(min_length=1, max_length=255)
    title_en: str = Field(min_length=1, max_length=255)
    description_ru: str = Field(default="", max_length=4_000)
    description_en: str = Field(default="", max_length=4_000)
    enabled: bool


# --- Lots ---

class LotOut(_Base):
    id: int
    funpay_id: str | None = None
    funpay_node_id: int | None = None
    tier_id: int
    duration_id: int
    limit_scope_id: int
    min_limit_pct: int | None = None
    max_5h_pct: int | None = None
    max_weekly_pct: int | None = None
    price: int
    title_ru: str
    title_en: str
    status: str
    auto_created: bool


class LotCreate(BaseModel):
    funpay_node_id: int | None = Field(default=None, gt=0)
    tier_id: int = Field(gt=0)
    duration_id: int = Field(gt=0)
    limit_scope_id: int = Field(gt=0)
    min_limit_pct: int | None = Field(default=None, ge=0, le=100)
    max_5h_pct: int | None = Field(default=None, ge=0, le=100)
    max_weekly_pct: int | None = Field(default=None, ge=0, le=100)
    price: int = Field(gt=0, le=100_000_000)
    title_ru: str = Field(min_length=1, max_length=255)
    title_en: str = Field(min_length=1, max_length=255)
    description_ru: str = Field(default="", max_length=4000)
    description_en: str = Field(default="", max_length=4000)


# --- Orders / Rentals ---

class OrderOut(_Base):
    id: int
    funpay_order_id: str
    funpay_chat_id: str
    buyer_funpay_id: str
    buyer_locale: str
    lot_id: int | None = None
    tier_id: int | None = None
    duration_id: int | None = None
    limit_scope_id: int | None = None
    min_limit_pct: int | None = None
    max_5h_pct: int | None = None
    max_weekly_pct: int | None = None
    price: int
    status: str
    fulfillment_attempts: int
    fulfillment_next_attempt_at: datetime | None = None
    fulfillment_last_error: str | None = None
    confirmation_delivery_status: str
    confirmation_delivery_attempts: int
    confirmation_delivery_next_attempt_at: datetime | None = None
    confirmation_delivery_last_error: str | None = None
    created_at: datetime


class RentalOut(_Base):
    id: int
    order_id: int
    account_id: int
    buyer_funpay_id: str
    buyer_funpay_chat_id: str
    tier_id: int
    duration_id: int
    limit_scope_id: int
    min_limit_pct: int | None = None
    max_5h_pct: int | None = None
    max_weekly_pct: int | None = None
    lang: str
    started_at: datetime
    expires_at: datetime
    status: str
    replacement_count: int
    credentials_delivery_status: str
    credentials_delivery_template: str
    credentials_delivery_started_at: datetime | None = None
    credentials_delivery_next_attempt_at: datetime | None = None
    credentials_delivered_at: datetime | None = None
    credentials_delivery_attempts: int
    credentials_delivery_last_error: str | None = None
    issued_codex_primary_pct: int | None = None
    issued_codex_primary_window_seconds: int | None = None
    issued_codex_primary_resets_at: datetime | None = None
    issued_codex_secondary_pct: int | None = None
    issued_codex_secondary_window_seconds: int | None = None
    issued_codex_secondary_resets_at: datetime | None = None
    issued_plan_window_status: str | None = None
    issued_expected_long_window_seconds: int | None = None
    issued_limits_measured_at: datetime | None = None


class RentalPatch(BaseModel):
    status: Literal["active", "expired", "refunded", "revoked"] | None = None

    @field_validator("status", mode="before")
    @classmethod
    def reject_null_status(cls, value):
        if value is None:
            raise ValueError("status cannot be null")
        return value


# --- Settings ---

class SettingsOut(_Base):
    funpay_node_id: int | None = None
    graph_configured: bool = False
    auto_bump_enabled: bool
    bump_interval_hours: int
    default_max_active_rentals: int
    funpay_commission_percent: int
    check_interval_minutes: int
    limits_check_interval_minutes: int
    refresh_recover_concurrency: int
    refresh_max_attempts: int
    refresh_retry_delay_minutes: int
    check_delay_seconds: int
    limits_warn_threshold_pct: int


class SettingsUpdate(BaseModel):
    funpay_node_id: int | None = Field(default=None, gt=0)
    auto_bump_enabled: bool | None = None
    bump_interval_hours: int | None = Field(default=None, ge=1, le=168)
    default_max_active_rentals: int | None = Field(default=None, ge=1, le=1)
    funpay_commission_percent: int | None = Field(default=None, ge=0, le=100)
    check_interval_minutes: int | None = Field(default=None, ge=1, le=10_080)
    limits_check_interval_minutes: int | None = Field(default=None, ge=1, le=55)
    refresh_recover_concurrency: int | None = Field(default=None, ge=1, le=20)
    refresh_max_attempts: int | None = Field(default=None, ge=1, le=20)
    refresh_retry_delay_minutes: int | None = Field(default=None, ge=1, le=1_440)
    check_delay_seconds: int | None = Field(default=None, ge=30, le=3_600)
    limits_warn_threshold_pct: int | None = Field(default=None, ge=0, le=100)

    @field_validator(
        "auto_bump_enabled",
        "bump_interval_hours",
        "default_max_active_rentals",
        "funpay_commission_percent",
        "check_interval_minutes",
        "limits_check_interval_minutes",
        "refresh_recover_concurrency",
        "refresh_max_attempts",
        "refresh_retry_delay_minutes",
        "check_delay_seconds",
        "limits_warn_threshold_pct",
        mode="before",
    )
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("field cannot be null")
        return value


class FunPayKeyUpdate(BaseModel):
    key: str = Field(min_length=16, max_length=4096)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 16:
            raise ValueError("FunPay key must contain at least 16 characters")
        return value


class FunPayKeyStatus(BaseModel):
    configured: bool
    connected: bool = False
    last4: str | None = None


class TelegramConfigUpdate(BaseModel):
    token: str | None = Field(default=None, min_length=8, max_length=4096)
    seller_chat_id: str | None = Field(default=None, max_length=128)


class TelegramConfigStatus(BaseModel):
    configured: bool
    token_last4: str | None = None
    seller_chat_id: str | None = None


# --- Metrics ---

class MetricsOut(BaseModel):
    active_rentals: int
    available_accounts: int
    orders_today: int
    revenue_brutto: int
    revenue_netto: int
    bot_status: str
