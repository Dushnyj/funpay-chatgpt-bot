"""Add safe availability controls for system limit scopes.

Revision ID: 20260713_0013
Revises: 20260713_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0013"
down_revision: str | None = "20260713_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "limit_scopes",
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "limit_scopes",
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE limit_scopes SET "
            "is_enabled = CASE "
            "WHEN code IN ('any', 'codex') THEN true ELSE false END, "
            "sort_order = CASE code "
            "WHEN 'any' THEN 10 WHEN 'chat' THEN 20 WHEN 'codex' THEN 30 "
            "ELSE 100 END"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("limit_scopes") as batch_op:
        batch_op.drop_column("sort_order")
        batch_op.drop_column("is_enabled")
