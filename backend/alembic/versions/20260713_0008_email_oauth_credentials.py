"""Add encrypted delegated email OAuth credentials.

Revision ID: 20260713_0008
Revises: 20260713_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0008"
down_revision: str | None = "20260713_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_oauth_credentials",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("external_subject", sa.String(length=255), nullable=True),
        sa.Column("refresh_token_encrypted", sa.String(), nullable=False),
        sa.Column("scopes", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "connected_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_email_oauth_credentials_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "account_id", name=op.f("pk_email_oauth_credentials")
        ),
    )


def downgrade() -> None:
    op.drop_table("email_oauth_credentials")
