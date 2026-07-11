from datetime import datetime

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
