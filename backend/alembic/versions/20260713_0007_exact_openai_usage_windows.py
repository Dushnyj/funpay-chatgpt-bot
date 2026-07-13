"""Store exact observed OpenAI usage windows.

Revision ID: 20260713_0007
Revises: 20260713_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0007"
down_revision: str | None = "20260713_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "account_limits",
        sa.Column("codex_primary_remaining_pct", sa.Integer(), nullable=True),
    )
    op.add_column(
        "account_limits",
        sa.Column("codex_primary_window_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "account_limits",
        sa.Column(
            "codex_primary_resets_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "account_limits",
        sa.Column("codex_secondary_remaining_pct", sa.Integer(), nullable=True),
    )
    op.add_column(
        "account_limits",
        sa.Column("codex_secondary_window_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "account_limits",
        sa.Column(
            "codex_secondary_resets_at", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("account_limits") as batch_op:
        batch_op.drop_column("codex_secondary_resets_at")
        batch_op.drop_column("codex_secondary_window_seconds")
        batch_op.drop_column("codex_secondary_remaining_pct")
        batch_op.drop_column("codex_primary_resets_at")
        batch_op.drop_column("codex_primary_window_seconds")
        batch_op.drop_column("codex_primary_remaining_pct")
