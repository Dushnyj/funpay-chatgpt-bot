from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.types.encrypted import FernetEncrypted


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(unique=True)
    password_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    totp_secret_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    tier_id: Mapped[int] = mapped_column(ForeignKey("subscription_tiers.id"))
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    max_active_rentals: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(32), default="pending_validation")
    chatgpt_last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    notes: Mapped[str | None] = mapped_column(default=None)


class AccountLimits(Base):
    __tablename__ = "account_limits"

    # Один к одному с Account: PK он же FK, что гарантирует уникальность
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    refresh_token_encrypted: Mapped[str] = mapped_column(FernetEncrypted)
    access_token_encrypted: Mapped[str | None] = mapped_column(FernetEncrypted, default=None)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    account_id_openai: Mapped[str | None] = mapped_column(default=None)
    chat_5h_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    chat_weekly_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    codex_5h_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    codex_weekly_remaining_pct: Mapped[int | None] = mapped_column(default=None)
    plan_type: Mapped[str | None] = mapped_column(default=None)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    refresh_status: Mapped[str] = mapped_column(String(16), default="ok")
    refresh_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    refresh_recover_attempts: Mapped[int] = mapped_column(default=0)
    refresh_last_recover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


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
