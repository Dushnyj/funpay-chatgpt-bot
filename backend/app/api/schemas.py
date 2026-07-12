from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    sort_order: int = 0
    usage_multiplier: float | None = None


class TierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool = True


class TierUpdate(BaseModel):
    is_active: bool | None = None
    is_sellable: bool | None = None


class DurationOut(_Base):
    id: int
    days: int
    is_enabled: bool
    sort_order: int


class DurationUpdate(BaseModel):
    id: int = Field(gt=0)
    is_enabled: bool | None = None
    sort_order: int | None = None


class LimitScopeOut(_Base):
    id: int
    code: str
    name: str


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
    max_active_rentals: int | None = None
    status: str
    notes: str | None = None
    plan_raw_type: str | None = None
    plan_source: str | None = None
    plan_confidence: float | None = None
    plan_detected_at: datetime | None = None
    validation_job: ValidationJobOut | None = None


class AccountCreate(BaseModel):
    login: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=4096)
    totp_secret: str = Field(default="", max_length=256)
    email: str | None = Field(default=None, max_length=320)
    email_password: str | None = Field(default=None, max_length=4096)
    subscription_expires_at: datetime | None = None
    max_active_rentals: int | None = Field(default=None, ge=1, le=100)
    notes: str | None = Field(default=None, max_length=4000)


class AccountUpdate(BaseModel):
    subscription_expires_at: datetime | None = None
    max_active_rentals: int | None = Field(default=None, ge=1, le=100)
    status: Literal[
        "pending_validation", "validation_failed", "active", "maintenance", "disabled"
    ] | None = None
    notes: str | None = Field(default=None, max_length=4000)


class AccountLimitsOut(_Base):
    account_id: int
    chat_5h_remaining_pct: int | None = None
    chat_weekly_remaining_pct: int | None = None
    codex_5h_remaining_pct: int | None = None
    codex_weekly_remaining_pct: int | None = None
    refresh_status: str
    measured_at: datetime | None = None
    plan_type: str | None = None


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
    items: list[PriceMatrixItem] = Field(min_length=1, max_length=10_000)


# --- Templates ---

class TemplateOut(_Base):
    key: str
    lang: str
    content: str


class TemplateItem(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    lang: Literal["ru", "en"]
    content: str = Field(min_length=1, max_length=20_000)


class TemplateUpdate(BaseModel):
    items: list[TemplateItem] = Field(min_length=1, max_length=500)


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
    price: int
    status: str
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
    lang: str
    started_at: datetime
    expires_at: datetime
    status: str
    replacement_count: int


class RentalPatch(BaseModel):
    status: Literal["active", "expired", "refunded", "replaced"] | None = None


# --- Settings ---

class SettingsOut(_Base):
    funpay_node_id: int | None = None
    auto_bump_enabled: bool
    bump_interval_hours: int
    default_max_active_rentals: int
    funpay_commission_percent: int
    check_interval_minutes: int
    limits_check_interval_minutes: int
    limits_warn_threshold_pct: int


class SettingsUpdate(BaseModel):
    funpay_node_id: int | None = Field(default=None, gt=0)
    auto_bump_enabled: bool | None = None
    bump_interval_hours: int | None = Field(default=None, ge=1, le=168)
    default_max_active_rentals: int | None = Field(default=None, ge=1, le=100)
    funpay_commission_percent: int | None = Field(default=None, ge=0, le=100)
    check_interval_minutes: int | None = Field(default=None, ge=1, le=10_080)
    limits_check_interval_minutes: int | None = Field(default=None, ge=1, le=10_080)
    limits_warn_threshold_pct: int | None = Field(default=None, ge=0, le=100)

    @field_validator(
        "auto_bump_enabled",
        "bump_interval_hours",
        "default_max_active_rentals",
        "funpay_commission_percent",
        "check_interval_minutes",
        "limits_check_interval_minutes",
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
