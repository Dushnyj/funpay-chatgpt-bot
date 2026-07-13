"""Make lot publication templates editable and addressable.

Revision ID: 20260713_0012
Revises: 20260713_0011
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "20260713_0012"
down_revision: str | None = "20260713_0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("lot_templates") as batch:
        batch.add_column(sa.Column("key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("name", sa.String(length=120), nullable=True))
        batch.add_column(
            sa.Column(
                "is_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "system_managed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # The old model existed but was never read by lot publication. Preserve
    # those rows as disabled recoverable drafts instead of unexpectedly making
    # them live during the upgrade.
    table = sa.table(
        "lot_templates",
        sa.column("id", sa.Integer()),
        sa.column("key", sa.String(length=64)),
        sa.column("name", sa.String(length=120)),
    )
    bind = op.get_bind()
    for (row_id,) in bind.execute(sa.select(table.c.id)):
        bind.execute(
            table.update()
            .where(table.c.id == row_id)
            .values(
                key=f"legacy-{row_id}",
                name=f"Legacy template {row_id}",
            )
        )

    with op.batch_alter_table("lot_templates") as batch:
        batch.alter_column(
            "key", existing_type=sa.String(length=64), nullable=False
        )
        batch.alter_column(
            "name", existing_type=sa.String(length=120), nullable=False
        )
        batch.create_unique_constraint("uq_lot_templates_key", ["key"])

    op.create_index(
        "uq_lot_templates_enabled_custom_target",
        "lot_templates",
        [sa.text("coalesce(tier_id, 0)"), sa.text("coalesce(limit_scope_id, 0)")],
        unique=True,
        postgresql_where=sa.text("system_managed = false AND is_enabled = true"),
        sqlite_where=sa.text("system_managed = 0 AND is_enabled = 1"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_lot_templates_enabled_custom_target",
        table_name="lot_templates",
    )
    with op.batch_alter_table("lot_templates") as batch:
        batch.drop_constraint("uq_lot_templates_key", type_="unique")
        batch.drop_column("system_managed")
        batch.drop_column("is_enabled")
        batch.drop_column("name")
        batch.drop_column("key")
