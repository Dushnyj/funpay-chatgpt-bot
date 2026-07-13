from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# ``expiry_pending`` still owns the account until the account-wide logout is
# confirmed. Every pool/capacity view must use this shared definition so an
# account cannot be issued or deleted while revocation is in progress.
OCCUPYING_RENTAL_STATUSES: tuple[str, str] = ("active", "expiry_pending")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "(lot_binding_method IS NULL AND funpay_offer_id IS NULL "
            "AND lot_provenance_token IS NULL) OR "
            "(lot_binding_method = 'offer_id' AND lot_id IS NOT NULL "
            "AND funpay_offer_id IS NOT NULL "
            "AND lot_provenance_token IS NULL) OR "
            "(lot_binding_method = 'provenance_token' AND lot_id IS NOT NULL "
            "AND funpay_offer_id IS NULL "
            "AND lot_provenance_token IS NOT NULL)",
            name="bot_lot_binding_shape",
        ),
        Index(
            "ix_orders_fulfillment_next_attempt_at",
            "fulfillment_next_attempt_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Идемпотентность обработки заказов FunPay: дубль события не создаст дубль-строку
    funpay_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    funpay_chat_id: Mapped[str] = mapped_column(String(64))
    buyer_funpay_id: Mapped[str] = mapped_column(String(64))
    buyer_locale: Mapped[str] = mapped_column(String(8), default="ru")
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), default=None)
    # Immutable proof captured when the remote sale is first bound. Legacy
    # rows remain NULL and must never become trusted merely from lot_id.
    lot_binding_method: Mapped[str | None] = mapped_column(
        String(32), default=None
    )
    funpay_offer_id: Mapped[str | None] = mapped_column(
        String(64), default=None
    )
    lot_provenance_token: Mapped[str | None] = mapped_column(
        String(32), default=None
    )
    tier_id: Mapped[int | None] = mapped_column(ForeignKey("subscription_tiers.id"), default=None)
    duration_id: Mapped[int | None] = mapped_column(ForeignKey("durations.id"), default=None)
    limit_scope_id: Mapped[int | None] = mapped_column(ForeignKey("limit_scopes.id"), default=None)
    min_limit_pct: Mapped[int | None] = mapped_column(default=None)
    max_5h_pct: Mapped[int | None] = mapped_column(default=None)
    max_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    price: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    fulfillment_attempts: Mapped[int] = mapped_column(Integer, default=0)
    fulfillment_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    fulfillment_last_error: Mapped[str | None] = mapped_column(
        String(128), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Rental(Base):
    __tablename__ = "rentals"
    # Одна аренда на заказ: повторная выдача по тому же заказу не создаёт дубль
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_rental_order"),
        UniqueConstraint(
            "replacement_target_account_id",
            name="uq_rentals_replacement_target_account_id",
        ),
        Index(
            "uq_rentals_one_occupying_account",
            "account_id",
            unique=True,
            postgresql_where=text(
                "status IN ('active', 'expiry_pending')"
            ),
            sqlite_where=text(
                "status IN ('active', 'expiry_pending')"
            ),
        ),
        Index(
            "ix_rentals_credentials_delivery_retry",
            "credentials_delivery_status",
            "credentials_delivery_next_attempt_at",
        ),
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
    expiry_revoke_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Durable reservation made before the old account is logged out. The
    # unique constraint prevents one candidate from being promised to two
    # concurrent replacements; AccountPool also excludes all such targets.
    replacement_target_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), default=None
    )
    expiry_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    replaced_by_rental_id: Mapped[int | None] = mapped_column(default=None)
    replacement_count: Mapped[int] = mapped_column(default=0)
    last_code_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    issued_codex_5h_pct: Mapped[int | None] = mapped_column(default=None)
    issued_codex_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    issued_codex_primary_pct: Mapped[int | None] = mapped_column(default=None)
    issued_codex_primary_window_seconds: Mapped[int | None] = mapped_column(default=None)
    issued_codex_primary_resets_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    issued_codex_secondary_pct: Mapped[int | None] = mapped_column(default=None)
    issued_codex_secondary_window_seconds: Mapped[int | None] = mapped_column(default=None)
    issued_codex_secondary_resets_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    issued_plan_window_status: Mapped[str | None] = mapped_column(
        String(24), default=None
    )
    issued_expected_long_window_seconds: Mapped[int | None] = mapped_column(default=None)
    issued_limits_measured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    credentials_delivery_status: Mapped[str] = mapped_column(
        String(16), default="sending"
    )
    credentials_delivery_template: Mapped[str] = mapped_column(
        String(32), default="welcome"
    )
    credentials_delivery_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    credentials_delivery_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    credentials_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    credentials_delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    credentials_delivery_last_error: Mapped[str | None] = mapped_column(
        String(128), default=None
    )
