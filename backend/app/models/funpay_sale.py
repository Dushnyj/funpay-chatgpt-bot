from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FunPaySale(Base):
    """A FunPay sale proven to belong to a lot managed by this bot.

    A seller-wide FunPay sales preview is not sufficient provenance.  Every
    row must point at the local ``Order`` created only after an exact,
    fail-closed lot match.  This database invariant keeps unrelated sales from
    authorising admin chat history or buyer commands.
    """

    __tablename__ = "funpay_sales"
    __table_args__ = (
        UniqueConstraint(
            "funpay_order_id",
            name="uq_funpay_sales_funpay_order_id",
        ),
        UniqueConstraint("order_id", name="uq_funpay_sales_order_id"),
        Index(
            "ix_funpay_sales_funpay_order_id",
            "funpay_order_id",
            unique=True,
        ),
        Index(
            "ix_funpay_sales_chat_buyer",
            "funpay_chat_id",
            "buyer_funpay_id",
        ),
        Index("ix_funpay_sales_buyer", "buyer_funpay_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    funpay_order_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
    )
    # Legacy rows may lack a chat node until the order page is enriched, but
    # they are still backed by a managed local Order.
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
