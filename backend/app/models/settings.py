from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.types.encrypted import FernetEncrypted


class SellerSettings(Base):
    __tablename__ = "seller_settings"

    # Singleton-строка: единственный ряд настроек продавца
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    funpay_session_key: Mapped[str | None] = mapped_column(
        FernetEncrypted(allow_legacy_plaintext=True), default=None
    )
    funpay_session_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    funpay_node_id: Mapped[int | None] = mapped_column(default=None)
    telegram_bot_token: Mapped[str | None] = mapped_column(
        FernetEncrypted(allow_legacy_plaintext=True), default=None
    )
    telegram_seller_chat_id: Mapped[str | None] = mapped_column(default=None)
    # A full browser/OAuth validation is intentionally infrequent. Lightweight
    # usage-window measurements have their own five-minute scheduler below.
    check_interval_minutes: Mapped[int] = mapped_column(default=1440)
    limits_check_interval_minutes: Mapped[int] = mapped_column(default=5)
    refresh_recover_concurrency: Mapped[int] = mapped_column(default=3)
    refresh_max_attempts: Mapped[int] = mapped_column(default=3)
    refresh_retry_delay_minutes: Mapped[int] = mapped_column(default=5)
    check_delay_seconds: Mapped[int] = mapped_column(default=45)
    bump_interval_hours: Mapped[int] = mapped_column(default=4)
    auto_bump_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_max_active_rentals: Mapped[int] = mapped_column(default=1)
    funpay_commission_percent: Mapped[int] = mapped_column(default=15)
    limits_warn_threshold_pct: Mapped[int] = mapped_column(default=20)
    admin_password_hash: Mapped[str | None] = mapped_column(default=None)
    admin_session_version: Mapped[int] = mapped_column(Integer, default=0)
    admin_login_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    admin_login_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    admin_login_blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
