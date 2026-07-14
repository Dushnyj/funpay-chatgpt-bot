from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_event_order", "event_type", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    event_type: Mapped[str] = mapped_column(String(48))
    account_id: Mapped[int | None] = mapped_column(default=None)
    order_id: Mapped[int | None] = mapped_column(default=None)
    rental_id: Mapped[int | None] = mapped_column(default=None)
    chat_id: Mapped[str | None] = mapped_column(default=None)
    message_text: Mapped[str | None] = mapped_column(default=None)
    # Атрибут `metadata_` маппится на колонку `metadata`, т.к. имя `metadata`
    # зарезервировано SQLAlchemy (это MetaData таблицы у DeclarativeBase).
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, default=None)
