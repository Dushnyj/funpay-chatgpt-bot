from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Идемпотентность обработки заказов FunPay: дубль события не создаст дубль-строку
    funpay_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    funpay_chat_id: Mapped[str] = mapped_column(String(64))
    buyer_funpay_id: Mapped[str] = mapped_column(String(64))
    buyer_locale: Mapped[str] = mapped_column(String(8), default="ru")
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), default=None)
    tier_id: Mapped[int | None] = mapped_column(ForeignKey("subscription_tiers.id"), default=None)
    duration_id: Mapped[int | None] = mapped_column(ForeignKey("durations.id"), default=None)
    limit_scope_id: Mapped[int | None] = mapped_column(ForeignKey("limit_scopes.id"), default=None)
    min_limit_pct: Mapped[int | None] = mapped_column(default=None)
    max_5h_pct: Mapped[int | None] = mapped_column(default=None)
    max_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    price: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Rental(Base):
    __tablename__ = "rentals"
    # Одна аренда на заказ: повторная выдача по тому же заказу не создаёт дубль
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_rental_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    buyer_funpay_id: Mapped[str] = mapped_column(String(64))
    buyer_funpay_chat_id: Mapped[str] = mapped_column(String(64))
    tier_id: Mapped[int] = mapped_column(ForeignKey("subscription_tiers.id"))
    duration_id: Mapped[int] = mapped_column(ForeignKey("durations.id"))
    limit_scope_id: Mapped[int] = mapped_column(ForeignKey("limit_scopes.id"))
    min_limit_pct: Mapped[int | None] = mapped_column(default=None)
    max_5h_pct: Mapped[int | None] = mapped_column(default=None)
    max_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    lang: Mapped[str] = mapped_column(String(8), default="ru")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")
    replaced_by_rental_id: Mapped[int | None] = mapped_column(default=None)
    replacement_count: Mapped[int] = mapped_column(default=0)
    last_code_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    issued_chat_5h_pct: Mapped[int | None] = mapped_column(default=None)
    issued_chat_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    issued_codex_5h_pct: Mapped[int | None] = mapped_column(default=None)
    issued_codex_weekly_pct: Mapped[int | None] = mapped_column(default=None)
