"""Trust only OpenAI-attested paid subscription deadlines.

Revision ID: 20260714_0020
Revises: 20260714_0019
"""

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision: str = "20260714_0020"
down_revision: str | None = "20260714_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUPERSEDED_REVALIDATION_JOB_TYPES = (
    "full_validation",
    "refresh_recover",
    "limit_check",
)


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "subscription_expiry_source",
            sa.String(length=32),
            nullable=True,
        ),
    )
    op.add_column(
        "account_limits",
        sa.Column(
            "subscription_expiry_source",
            sa.String(length=32),
            nullable=True,
        ),
    )

    connection = op.get_bind()
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Integer()),
        sa.column("tier_id", sa.Integer()),
        sa.column("subscription_expires_at", sa.DateTime(timezone=True)),
        sa.column("subscription_expiry_source", sa.String()),
        sa.column("status", sa.String()),
        sa.column("operator_status_override", sa.String()),
        sa.column("validation_rerun_requested", sa.Boolean()),
    )
    limits = sa.table(
        "account_limits",
        sa.column("account_id", sa.Integer()),
        sa.column("subscription_expires_at", sa.DateTime(timezone=True)),
        sa.column("subscription_expiry_source", sa.String()),
    )
    tiers = sa.table(
        "subscription_tiers",
        sa.column("id", sa.Integer()),
        sa.column("code", sa.String()),
    )
    jobs = sa.table(
        "account_check_jobs",
        sa.column("account_id", sa.Integer()),
        sa.column("priority", sa.String()),
        sa.column("job_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("finished_at", sa.DateTime(timezone=True)),
        sa.column("result", sa.String()),
    )

    # No pre-0020 row can prove where its editable deadline came from. Clear
    # both denormalized copies first; Free remains termless and paid accounts
    # cannot be allocated until a new OpenAI observation supplies provenance.
    connection.execute(
        accounts.update().values(
            subscription_expires_at=None,
            subscription_expiry_source=None,
        )
    )
    connection.execute(
        limits.update().values(
            subscription_expires_at=None,
            subscription_expiry_source=None,
        )
    )

    with op.batch_alter_table("accounts") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_accounts_subscription_expiry_source_trusted"),
            "subscription_expiry_source IS NULL OR "
            "subscription_expiry_source IN ('accounts_check', 'id_token')",
        )
    with op.batch_alter_table("account_limits") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_account_limits_subscription_expiry_source_trusted"),
            "subscription_expiry_source IS NULL OR "
            "subscription_expiry_source IN ('accounts_check', 'id_token')",
        )

    paid_tier_exists = sa.exists(
        sa.select(tiers.c.id).where(
            tiers.c.id == accounts.c.tier_id,
            tiers.c.code.is_not(None),
            tiers.c.code != "free",
        )
    )
    needs_revalidation = sa.and_(
        paid_tier_exists,
        accounts.c.operator_status_override.is_(None),
        accounts.c.status.in_(
            ["active", "pending_validation", "validation_failed"]
        ),
    )
    worker_handoff_exists = sa.exists(
        sa.select(jobs.c.account_id).where(
            jobs.c.account_id == accounts.c.id,
            sa.or_(
                jobs.c.status == "running",
                sa.and_(
                    jobs.c.status == "pending",
                    jobs.c.job_type == "device_auth",
                ),
            ),
        )
    )

    # A running worker owns the account mutation lease. A pending device-auth
    # job is also exclusive and must survive the migration so its in-flight
    # browser flow can finish (or startup recovery can fail it explicitly).
    # Both paths use the durable rerun handoff; accounts without an exclusive
    # worker get a provenance-aware full validation immediately.
    connection.execute(
        accounts.update()
        .where(needs_revalidation, worker_handoff_exists)
        .values(
            status="pending_validation",
            validation_rerun_requested=True,
        )
    )
    connection.execute(
        accounts.update()
        .where(needs_revalidation, ~worker_handoff_exists)
        .values(
            status="pending_validation",
            validation_rerun_requested=False,
        )
    )

    revalidation_account_exists = sa.exists(
        sa.select(accounts.c.id).where(
            accounts.c.id == jobs.c.account_id,
            paid_tier_exists,
            accounts.c.operator_status_override.is_(None),
            accounts.c.status == "pending_validation",
        )
    )
    now = datetime.now(timezone.utc)
    # Supersede only jobs that the new full validation replaces. Device-auth
    # has separate external state and must never be silently marked done.
    connection.execute(
        jobs.update()
        .where(
            jobs.c.status == "pending",
            jobs.c.job_type.in_(_SUPERSEDED_REVALIDATION_JOB_TYPES),
            revalidation_account_exists,
        )
        .values(
            status="done",
            result="superseded:expiry_provenance_migration",
            finished_at=now,
        )
    )

    active_job_exists = sa.exists(
        sa.select(jobs.c.account_id).where(
            jobs.c.account_id == accounts.c.id,
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
                sa.literal(now),
            ).where(
                paid_tier_exists,
                accounts.c.operator_status_override.is_(None),
                accounts.c.status == "pending_validation",
                accounts.c.validation_rerun_requested.is_(False),
                ~active_job_exists,
            ),
        )
    )


def downgrade() -> None:
    # Cleared operator-editable deadlines cannot be reconstructed. The queued
    # validation is still safe and useful on 0019, so only the schema additions
    # are removed.
    with op.batch_alter_table("account_limits") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_account_limits_subscription_expiry_source_trusted"),
            type_="check",
        )
        batch_op.drop_column("subscription_expiry_source")
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_accounts_subscription_expiry_source_trusted"),
            type_="check",
        )
        batch_op.drop_column("subscription_expiry_source")
