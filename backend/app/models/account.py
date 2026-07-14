from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.types.encrypted import FernetEncrypted


# Only OpenAI-owned responses may attest a paid subscription deadline. Keep
# this allow-list next to the persisted fields so every allocator uses the
# same trust contract.
TRUSTED_SUBSCRIPTION_EXPIRY_SOURCES = ("accounts_check", "id_token")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "max_active_rentals IS NULL OR max_active_rentals = 1",
            name="single_active_rental",
        ),
        CheckConstraint(
            "subscription_expiry_source IS NULL OR "
            "subscription_expiry_source IN ('accounts_check', 'id_token')",
            name="subscription_expiry_source_trusted",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(unique=True)
    password_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    totp_secret_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    email: Mapped[str | None] = mapped_column(default=None)
    email_password_encrypted: Mapped[str | None] = mapped_column(FernetEncrypted, default=None)
    # The tier is unknown until the first successful OpenAI metadata check.
    tier_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscription_tiers.id"), default=None
    )
    plan_raw_type: Mapped[str | None] = mapped_column(String(255), default=None)
    plan_source: Mapped[str | None] = mapped_column(String(128), default=None)
    plan_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    plan_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    subscription_expiry_source: Mapped[str | None] = mapped_column(
        String(32), default=None
    )
    max_active_rentals: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(32), default="pending_validation")
    # Durable operator intent is separate from validation status. Browser jobs
    # may finish after an operator pauses an account; allocation always checks
    # this override so a late success cannot make it sellable again.
    operator_status_override: Mapped[str | None] = mapped_column(
        String(16), default=None
    )
    # When OAuth data arrives while another validation owns the account, that
    # worker must enqueue one follow-up pass after releasing its lease.
    validation_rerun_requested: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    chatgpt_last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    notes: Mapped[str | None] = mapped_column(default=None)


class AccountLimits(Base):
    __tablename__ = "account_limits"
    __table_args__ = (
        CheckConstraint(
            "subscription_expiry_source IS NULL OR "
            "subscription_expiry_source IN ('accounts_check', 'id_token')",
            name="subscription_expiry_source_trusted",
        ),
    )

    # Один к одному с Account: PK он же FK, что гарантирует уникальность
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    refresh_token_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    access_token_encrypted: Mapped[str | None] = mapped_column(FernetEncrypted, default=None)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    account_id_openai: Mapped[str | None] = mapped_column(default=None)
    codex_5h_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    codex_weekly_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    # Exact window observations from /wham/usage. The legacy 5h/weekly columns
    # remain for API/database compatibility and are populated only when the
    # observed duration really is 5 hours / 7 days.
    codex_primary_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    codex_primary_window_seconds: Mapped[int | None] = mapped_column(default=None)
    codex_primary_resets_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    codex_secondary_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    codex_secondary_window_seconds: Mapped[int | None] = mapped_column(default=None)
    codex_secondary_resets_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    plan_type: Mapped[str | None] = mapped_column(default=None)
    plan_window_status: Mapped[str] = mapped_column(String(24), default="unknown")
    expected_long_window_seconds: Mapped[int | None] = mapped_column(default=None)
    low_limit_warning_fingerprint: Mapped[str | None] = mapped_column(
        String(160), default=None
    )
    low_limit_warned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    subscription_expiry_source: Mapped[str | None] = mapped_column(
        String(32), default=None
    )
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    refresh_status: Mapped[str] = mapped_column(String(16), default="ok")
    refresh_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    refresh_recover_attempts: Mapped[int] = mapped_column(default=0)
    refresh_last_recover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class EmailOAuthCredential(Base):
    """Delegated mailbox OAuth credential owned by exactly one account."""

    __tablename__ = "email_oauth_credentials"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="microsoft_graph")
    email: Mapped[str] = mapped_column(String(320))
    external_subject: Mapped[str | None] = mapped_column(String(255), default=None)
    refresh_token_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    scopes: Mapped[str] = mapped_column(String(1024), default="")
    status: Mapped[str] = mapped_column(String(32), default="connected")
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AccountCheckJob(Base):
    __tablename__ = "account_check_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    priority: Mapped[str] = mapped_column(String(20), default="scheduled")  # new | refresh_recover | manual | scheduled | limit_check
    job_type: Mapped[str] = mapped_column(String(20))  # full_validation | refresh_recover | limit_check
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | running | done | failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    result: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)
