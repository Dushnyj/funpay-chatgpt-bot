"""Run expensive full browser validation daily by default.

Revision ID: 20260713_0010
Revises: 20260713_0009
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20260713_0010"
down_revision: str | None = "20260713_0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    seller_settings = sa.table(
        "seller_settings",
        sa.column("check_interval_minutes", sa.Integer()),
    )
    op.execute(
        seller_settings.update()
        .where(seller_settings.c.check_interval_minutes == 10)
        .values(check_interval_minutes=1440)
    )


def downgrade() -> None:
    seller_settings = sa.table(
        "seller_settings",
        sa.column("check_interval_minutes", sa.Integer()),
    )
    op.execute(
        seller_settings.update()
        .where(seller_settings.c.check_interval_minutes == 1440)
        .values(check_interval_minutes=10)
    )
