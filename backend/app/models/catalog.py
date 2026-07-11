from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SubscriptionTier(Base):
    __tablename__ = "subscription_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None] = mapped_column(default=None)
    is_active: Mapped[bool] = mapped_column(default=True)


class Duration(Base):
    __tablename__ = "durations"

    id: Mapped[int] = mapped_column(primary_key=True)
    days: Mapped[int] = mapped_column(unique=True)
    is_enabled: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(default=0)


class LimitScope(Base):
    __tablename__ = "limit_scopes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)  # any | chat | codex
    name: Mapped[str] = mapped_column(unique=True)
