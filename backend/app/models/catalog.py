from sqlalchemy import Boolean, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SubscriptionTier(Base):
    __tablename__ = "subscription_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None] = mapped_column(default=None)
    is_active: Mapped[bool] = mapped_column(default=True)
    # ``code`` is nullable only for backwards compatibility with operator-created
    # legacy rows.  Every system tier always has a stable canonical code.
    code: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)
    system_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sellable: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    usage_multiplier: Mapped[float | None] = mapped_column(Float, default=None)


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
    # Codes and names are stable system identifiers. Operators may only
    # control whether a scope participates in new offers and how it is shown.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
