from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
    description: str | None = None
    is_active: bool


class TierCreate(BaseModel):
    name: str
    description: str | None = None
    is_active: bool = True


class TierUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None


class DurationOut(_Base):
    id: int
    days: int
    is_enabled: bool
    sort_order: int


class DurationUpdate(BaseModel):
    id: int
    is_enabled: bool | None = None
    sort_order: int | None = None


class LimitScopeOut(_Base):
    id: int
    code: str
    name: str


# --- Accounts ---

class AccountOut(_Base):
    id: int
    login: str
    tier_id: int
    email: str | None = None
    subscription_expires_at: datetime | None = None
    max_active_rentals: int | None = None
    status: str
    notes: str | None = None


class AccountCreate(BaseModel):
    login: str
    password: str
    totp_secret: str = ""
    email: str | None = None
    email_password: str | None = None
    tier_id: int
    subscription_expires_at: datetime | None = None
    max_active_rentals: int | None = None
    notes: str | None = None


class AccountUpdate(BaseModel):
    subscription_expires_at: datetime | None = None
    max_active_rentals: int | None = None
    status: str | None = None
    notes: str | None = None


class AccountLimitsOut(_Base):
    account_id: int
    chat_5h_remaining_pct: int | None = None
    chat_weekly_remaining_pct: int | None = None
    codex_5h_remaining_pct: int | None = None
    codex_weekly_remaining_pct: int | None = None
    refresh_status: str
    measured_at: datetime | None = None


class AccountWithLimits(AccountOut):
    limits: AccountLimitsOut | None = None


# --- Price Matrix ---

class PriceMatrixItem(BaseModel):
    tier_id: int
    duration_id: int
    limit_scope_id: int
    min_limit_pct: int | None = None
    max_5h_pct: int | None = None
    max_weekly_pct: int | None = None
    price: int


class PriceMatrixUpdate(BaseModel):
    items: list[PriceMatrixItem]


# --- Templates ---

class TemplateOut(_Base):
    key: str
    lang: str
    content: str


class TemplateItem(BaseModel):
    key: str
    lang: str
    content: str


class TemplateUpdate(BaseModel):
    items: list[TemplateItem]


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
    description_ru: str = ""
    description_en: str = ""


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
    status: str | None = None


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
    funpay_node_id: int | None = None
    auto_bump_enabled: bool | None = None
    bump_interval_hours: int | None = None
    default_max_active_rentals: int | None = None
    funpay_commission_percent: int | None = None
    check_interval_minutes: int | None = None
    limits_check_interval_minutes: int | None = None
    limits_warn_threshold_pct: int | None = None


# --- Metrics ---

class MetricsOut(BaseModel):
    active_rentals: int
    available_accounts: int
    orders_today: int
    revenue_brutto: int
    revenue_netto: int
    bot_status: str
