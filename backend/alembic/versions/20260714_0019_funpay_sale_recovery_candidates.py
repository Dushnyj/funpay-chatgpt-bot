"""Persist bounded exact recovery candidates for missed FunPay sales.

Revision ID: 20260714_0019
Revises: 20260714_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260714_0019"
down_revision: str | None = "20260714_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "confirmation_delivery_status",
            sa.String(length=16),
            nullable=False,
            server_default="idle",
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "confirmation_delivery_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "confirmation_delivery_next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "confirmation_delivery_last_error",
            sa.String(length=128),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_orders_confirmation_delivery_retry",
        "orders",
        [
            "confirmation_delivery_status",
            "confirmation_delivery_next_attempt_at",
            "created_at",
        ],
        unique=False,
    )
    op.create_table(
        "funpay_sale_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("funpay_order_id", sa.String(length=64), nullable=False),
        sa.Column("buyer_funpay_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("observed_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recovery_state",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=128), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_funpay_sale_candidates")
        ),
        sa.UniqueConstraint(
            "funpay_order_id",
            name="uq_funpay_sale_candidates_funpay_order_id",
        ),
    )
    op.create_index(
        "ix_funpay_sale_candidates_recovery_due",
        "funpay_sale_candidates",
        [
            "recovery_state",
            "next_attempt_at",
            "observed_created_at",
        ],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_event_order",
        "audit_logs",
        ["event_type", "order_id"],
        unique=False,
    )
    # 0018 deliberately stopped seller-wide backfill because it had nowhere
    # safe to persist unverified rows.  The isolated candidate queue now makes
    # a full bounded rescan safe and lets deployments recover sales missed
    # while the listener was offline before this migration.
    op.execute(
        sa.text(
            "UPDATE funpay_sale_sync_state SET "
            "backfill_cursor = NULL, backfill_complete = false"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE funpay_sale_sync_state SET "
            "backfill_cursor = NULL, backfill_complete = true"
        )
    )
    op.drop_index(
        "ix_funpay_sale_candidates_recovery_due",
        table_name="funpay_sale_candidates",
    )
    op.drop_index(
        "ix_audit_logs_event_order",
        table_name="audit_logs",
    )
    op.drop_table("funpay_sale_candidates")
    op.drop_index(
        "ix_orders_confirmation_delivery_retry",
        table_name="orders",
    )
    op.drop_column("orders", "confirmation_delivery_last_error")
    op.drop_column("orders", "confirmation_delivery_next_attempt_at")
    op.drop_column("orders", "confirmation_delivery_attempts")
    op.drop_column("orders", "confirmation_delivery_status")
