"""Persist bounded FunPay sale-history backfill progress.

Revision ID: 20260714_0017
Revises: 20260713_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260714_0017"
down_revision: str | None = "20260713_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "funpay_sale_sync_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("backfill_cursor", sa.String(length=64), nullable=True),
        sa.Column(
            "backfill_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("head_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "page_backoff_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "page_backoff_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_funpay_sale_sync_state")),
    )
    op.execute(
        sa.text(
            "INSERT INTO funpay_sale_sync_state "
            "(id, backfill_complete, page_backoff_attempts, updated_at) "
            "VALUES (1, false, 0, CURRENT_TIMESTAMP)"
        )
    )


def downgrade() -> None:
    op.drop_table("funpay_sale_sync_state")
