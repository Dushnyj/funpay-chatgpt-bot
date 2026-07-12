"""Add admin session revocation version.

Revision ID: 20260713_0004
Revises: 20260713_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0004"
down_revision: str | None = "20260713_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("seller_settings")
    }
    if "admin_session_version" in columns:
        return
    op.add_column(
        "seller_settings",
        sa.Column(
            "admin_session_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("seller_settings")
    }
    if "admin_session_version" in columns:
        op.drop_column("seller_settings", "admin_session_version")
