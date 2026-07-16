from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.types.encrypted import FernetEncrypted


class ProxyRoute(Base):
    """A browser-only network route used for account authentication.

    Credentials are decrypted only when a Playwright browser is launched.  No
    request made by the rest of the application inherits this route.
    """

    __tablename__ = "proxy_routes"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('home_relay', 'custom_proxy')",
            name="mode_supported",
        ),
        CheckConstraint(
            "proxy_type IN ('http', 'https', 'socks5')",
            name="proxy_type_supported",
        ),
        CheckConstraint("port >= 1 AND port <= 65535", name="port_range"),
        CheckConstraint(
            "status IN ('unchecked', 'online', 'offline')",
            name="status_supported",
        ),
        Index(
            "uq_proxy_routes_single_home_relay",
            "mode",
            unique=True,
            postgresql_where=text("mode = 'home_relay'"),
            sqlite_where=text("mode = 'home_relay'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    mode: Mapped[str] = mapped_column(String(20))
    proxy_type: Mapped[str] = mapped_column(String(12))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    username_encrypted: Mapped[str | None] = mapped_column(
        FernetEncrypted, default=None
    )
    password_encrypted: Mapped[str | None] = mapped_column(
        FernetEncrypted, default=None
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Every network-affecting mutation invalidates in-flight probes.  The
    # test endpoint publishes a result only for the exact revision it tested.
    config_revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="unchecked")
    egress_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Deliberately stores only a stable, secret-free machine code.
    last_error: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class HomeRelaySetup(Base):
    """One-time proof allowing a Windows client to enroll one SSH key."""

    __tablename__ = "home_relay_setups"

    id: Mapped[int] = mapped_column(primary_key=True)
    route_id: Mapped[int] = mapped_column(
        ForeignKey("proxy_routes.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    machine_name: Mapped[str | None] = mapped_column(String(128), default=None)
    public_key_fingerprint: Mapped[str | None] = mapped_column(
        String(64), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
