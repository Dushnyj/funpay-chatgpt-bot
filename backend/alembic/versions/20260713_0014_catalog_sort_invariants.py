"""Normalize catalog sort mirrors to their canonical values.

Revision ID: 20260713_0014
Revises: 20260713_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0014"
down_revision: str | None = "20260713_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE durations SET sort_order = days"))
    connection.execute(
        sa.text(
            "UPDATE limit_scopes SET sort_order = CASE code "
            "WHEN 'any' THEN 10 WHEN 'chat' THEN 20 WHEN 'codex' THEN 30 "
            "ELSE 100 END"
        )
    )


def downgrade() -> None:
    # Previous operator-defined ordering cannot be reconstructed. The columns
    # intentionally remain in place for backwards compatibility.
    pass
