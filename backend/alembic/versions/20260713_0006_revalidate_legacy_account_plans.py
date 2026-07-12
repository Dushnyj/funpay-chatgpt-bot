"""Revalidate legacy accounts without trustworthy plan evidence.

Revision ID: 20260713_0006
Revises: 20260713_0005
"""

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0006"
down_revision: str | None = "20260713_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Integer()),
        sa.column("tier_id", sa.Integer()),
        sa.column("status", sa.String()),
        sa.column("plan_source", sa.String()),
        sa.column("plan_confidence", sa.Float()),
        sa.column("plan_detected_at", sa.DateTime(timezone=True)),
    )
    tiers = sa.table(
        "subscription_tiers",
        sa.column("id", sa.Integer()),
        sa.column("code", sa.String()),
        sa.column("system_managed", sa.Boolean()),
    )
    jobs = sa.table(
        "account_check_jobs",
        sa.column("account_id", sa.Integer()),
        sa.column("priority", sa.String()),
        sa.column("job_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    canonical_tier_exists = sa.exists(
        sa.select(tiers.c.id).where(
            tiers.c.id == accounts.c.tier_id,
            tiers.c.code.is_not(None),
            tiers.c.system_managed.is_(True),
        )
    )
    lacks_trustworthy_plan = sa.or_(
        accounts.c.plan_detected_at.is_(None),
        accounts.c.plan_source.is_(None),
        accounts.c.plan_confidence.is_(None),
        accounts.c.plan_confidence <= 0,
        ~canonical_tier_exists,
    )

    # Previously active/manual accounts must leave the sellable pool until a
    # new validation resolves their plan. Explicit disabled/maintenance states
    # remain operator-controlled and are not reactivated by the migration.
    connection.execute(
        accounts.update()
        .where(
            lacks_trustworthy_plan,
            accounts.c.status.in_(
                ["active", "pending_validation", "validation_failed"]
            ),
        )
        .values(tier_id=None, status="pending_validation")
    )
    connection.execute(
        accounts.update()
        .where(lacks_trustworthy_plan)
        .values(tier_id=None)
    )

    active_full_validation_exists = sa.exists(
        sa.select(jobs.c.account_id).where(
            jobs.c.account_id == accounts.c.id,
            jobs.c.job_type == "full_validation",
            jobs.c.status.in_(["pending", "running"]),
        )
    )
    connection.execute(
        jobs.insert().from_select(
            ["account_id", "priority", "job_type", "status", "created_at"],
            sa.select(
                accounts.c.id,
                sa.literal("new"),
                sa.literal("full_validation"),
                sa.literal("pending"),
                sa.literal(datetime.now(timezone.utc)),
            ).where(
                accounts.c.status == "pending_validation",
                lacks_trustworthy_plan,
                ~active_full_validation_exists,
            ),
        )
    )


def downgrade() -> None:
    # The previous operator-selected tier cannot be reconstructed safely.
    # Revalidation jobs and discovered plan evidence remain valid after a
    # schema downgrade to 0005, so this data migration is intentionally kept.
    pass
