"""Add canonical subscription catalog and plan detection evidence.

Revision ID: 20260713_0005
Revises: 20260713_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0005"
down_revision: str | None = "20260713_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SYSTEM_PLANS: tuple[tuple[str, str, str, int, float | None], ...] = (
    ("free", "Free", "ChatGPT Free", 10, None),
    ("go", "Go", "ChatGPT Go", 20, None),
    ("plus", "Plus", "ChatGPT Plus", 30, 1.0),
    (
        "pro_5x",
        "Pro 5x",
        "ChatGPT Pro с профилем лимитов 5x (raw: prolite)",
        40,
        5.0,
    ),
    (
        "pro_20x",
        "Pro 20x",
        "ChatGPT Pro с профилем лимитов 20x (raw: pro)",
        50,
        20.0,
    ),
    (
        "business",
        "Business / usage-based",
        "ChatGPT Business, включая прежнее raw-имя team",
        60,
        None,
    ),
    (
        "enterprise",
        "Enterprise / usage-based",
        "ChatGPT Enterprise с usage-based конфигурацией",
        70,
        None,
    ),
    ("edu", "Edu", "ChatGPT Edu", 80, None),
    ("teachers", "Teachers", "ChatGPT for Teachers", 90, None),
    ("healthcare", "Healthcare", "ChatGPT for Healthcare", 100, None),
    ("clinicians", "Clinicians", "ChatGPT for Clinicians", 110, None),
    ("gov", "Gov", "ChatGPT Gov", 120, None),
)


def upgrade() -> None:
    op.add_column(
        "subscription_tiers", sa.Column("code", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "subscription_tiers",
        sa.Column("system_managed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "subscription_tiers",
        sa.Column("is_sellable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "subscription_tiers",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "subscription_tiers",
        sa.Column("usage_multiplier", sa.Float(), nullable=True),
    )

    tiers = sa.table(
        "subscription_tiers",
        sa.column("id", sa.Integer()),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("system_managed", sa.Boolean()),
        sa.column("is_sellable", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
        sa.column("usage_multiplier", sa.Float()),
    )
    connection = op.get_bind()
    existing = {
        row.name.casefold(): row
        for row in connection.execute(
            sa.select(tiers.c.id, tiers.c.name, tiers.c.description)
        )
    }
    for code, name, description, sort_order, multiplier in _SYSTEM_PLANS:
        row = existing.get(name.casefold())
        if row is None and code == "pro_20x":
            row = existing.get("pro")
        values = {
            "code": code,
            "name": name,
            "system_managed": True,
            "is_sellable": True,
            "sort_order": sort_order,
            "usage_multiplier": multiplier,
        }
        if row is None:
            connection.execute(
                tiers.insert().values(
                    description=description,
                    is_active=True,
                    **values,
                )
            )
        else:
            connection.execute(
                tiers.update().where(tiers.c.id == row.id).values(**values)
            )

    with op.batch_alter_table("subscription_tiers") as batch_op:
        batch_op.create_unique_constraint("uq_subscription_tiers_code", ["code"])

    op.add_column(
        "accounts", sa.Column("plan_raw_type", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "accounts", sa.Column("plan_source", sa.String(length=128), nullable=True)
    )
    op.add_column("accounts", sa.Column("plan_confidence", sa.Float(), nullable=True))
    op.add_column(
        "accounts", sa.Column("plan_detected_at", sa.DateTime(timezone=True), nullable=True)
    )
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.alter_column(
            "tier_id", existing_type=sa.Integer(), nullable=True
        )
    # A pending/failed account has never produced trustworthy OpenAI plan
    # evidence.  Do not carry its old operator-selected tier into the new
    # automatic catalog, otherwise it could become sellable under a false plan.
    connection.execute(
        sa.text(
            "UPDATE accounts SET tier_id = NULL "
            "WHERE status IN ('pending_validation', 'validation_failed')"
        )
    )


def downgrade() -> None:
    # A downgrade restores the old mandatory tier contract.  Use Free for rows
    # that have not been classified so the FK remains valid.
    connection = op.get_bind()
    free_id = connection.execute(
        sa.text("SELECT id FROM subscription_tiers WHERE code = 'free'")
    ).scalar_one_or_none()
    if free_id is not None:
        connection.execute(
            sa.text("UPDATE accounts SET tier_id = :tier_id WHERE tier_id IS NULL"),
            {"tier_id": free_id},
        )

    with op.batch_alter_table("accounts") as batch_op:
        batch_op.alter_column(
            "tier_id", existing_type=sa.Integer(), nullable=False
        )
        batch_op.drop_column("plan_detected_at")
        batch_op.drop_column("plan_confidence")
        batch_op.drop_column("plan_source")
        batch_op.drop_column("plan_raw_type")

    with op.batch_alter_table("subscription_tiers") as batch_op:
        batch_op.drop_constraint("uq_subscription_tiers_code", type_="unique")
        batch_op.drop_column("usage_multiplier")
        batch_op.drop_column("sort_order")
        batch_op.drop_column("is_sellable")
        batch_op.drop_column("system_managed")
        batch_op.drop_column("code")
