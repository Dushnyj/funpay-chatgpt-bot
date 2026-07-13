from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FunPaySale(Base):
    """Authoritative provenance that a FunPay peer bought from this seller.

    Rows are created only from ``NewSaleEvent`` or ``Bot.get_sales()``.  Local
    ``Order`` rows are fulfillment state and may be absent when a sale cannot
    be matched to a configured lot.
    """

    __tablename__ = "funpay_sales"
    __table_args__ = (
        Index(
            "ix_funpay_sales_chat_buyer",
            "funpay_chat_id",
            "buyer_funpay_id",
        ),
        Index("ix_funpay_sales_buyer", "buyer_funpay_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    funpay_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"),
        unique=True,
        default=None,
    )
    # ``get_sales`` does not expose a chat node. It is filled lazily from the
    # order page in a rate-limited enrichment pass or from a verified message.
    funpay_chat_id: Mapped[str | None] = mapped_column(String(64), default=None)
    buyer_funpay_id: Mapped[str] = mapped_column(String(64))
    buyer_username: Mapped[str | None] = mapped_column(String(128), default=None)
    buyer_avatar_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    buyer_is_online: Mapped[bool | None] = mapped_column(Boolean, default=None)
    buyer_status_text: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    profile_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    detail_attempts: Mapped[int] = mapped_column(default=0)
    detail_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class FunPaySaleSyncState(Base):
    """Durable cursor for bounded historical-sale backfill."""

    __tablename__ = "funpay_sale_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    backfill_cursor: Mapped[str | None] = mapped_column(String(64), default=None)
    backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    head_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    page_backoff_attempts: Mapped[int] = mapped_column(Integer, default=0)
    page_backoff_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
