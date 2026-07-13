from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.types.encrypted import FernetEncryptedText


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatConversation(Base):
    """Durable admin-console view of a FunPay conversation."""

    __tablename__ = "chat_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    funpay_chat_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    buyer_funpay_id: Mapped[str | None] = mapped_column(String(64), default=None)
    funpay_order_id: Mapped[str | None] = mapped_column(String(64), default=None)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), default=None)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_text: Mapped[str | None] = mapped_column(
        FernetEncryptedText(allow_legacy_plaintext=True), default=None
    )
    last_message_direction: Mapped[str | None] = mapped_column(String(16), default=None)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ChatMessage(Base):
    """One incoming or outgoing message in a durable FunPay conversation."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "funpay_message_id", name="uq_chat_message_source"),
        Index("ix_chat_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"),
    )
    funpay_message_id: Mapped[str | None] = mapped_column(String(64), default=None)
    direction: Mapped[str] = mapped_column(String(16))  # incoming | outgoing
    sender_funpay_id: Mapped[str | None] = mapped_column(String(64), default=None)
    text: Mapped[str] = mapped_column(
        FernetEncryptedText(allow_legacy_plaintext=True)
    )
    delivery_status: Mapped[str] = mapped_column(String(16))  # received | pending | sent | failed
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
