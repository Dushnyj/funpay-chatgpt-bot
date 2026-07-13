"""Add durable rental credential delivery state.

Revision ID: 20260713_0009
Revises: 20260713_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op


revision: str = "20260713_0009"
down_revision: str | None = "20260713_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLLBACK_BACKUP_TABLE = "rental_delivery_state_rollback_backup"


def _has_rollback_backup() -> bool:
    return inspect(op.get_bind()).has_table(_ROLLBACK_BACKUP_TABLE)


def upgrade() -> None:
    op.add_column(
        "rentals",
        sa.Column(
            "credentials_delivery_status",
            sa.String(length=16),
            nullable=False,
            server_default="sending",
        ),
    )
    op.add_column(
        "rentals",
        sa.Column(
            "credentials_delivery_template",
            sa.String(length=32),
            nullable=False,
            server_default="welcome",
        ),
    )
    op.add_column(
        "rentals",
        sa.Column(
            "credentials_delivery_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "rentals",
        sa.Column(
            "credentials_delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "rentals",
        sa.Column(
            "credentials_delivery_attempts",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "rentals",
        sa.Column(
            "credentials_delivery_last_error",
            sa.String(length=128),
            nullable=True,
        ),
    )
    # Rows created before this migration were committed only after the welcome
    # message returned successfully, so they are known delivered rentals.
    op.execute(
        sa.text(
            "UPDATE rentals SET credentials_delivery_status = 'sent', "
            "credentials_delivery_started_at = started_at, "
            "credentials_delivered_at = started_at"
        )
    )
    if _has_rollback_backup():
        # A deliberate 0009 -> 0008 rollback cannot expose these columns to
        # the older application, but it must not destroy retry truth. Restore
        # the exact state when the operator upgrades again.
        for column in (
            "credentials_delivery_status",
            "credentials_delivery_template",
            "credentials_delivery_started_at",
            "credentials_delivered_at",
            "credentials_delivery_attempts",
            "credentials_delivery_last_error",
        ):
            op.execute(sa.text(
                f"UPDATE rentals SET {column} = ("
                f"SELECT {column} FROM {_ROLLBACK_BACKUP_TABLE} backup "
                "WHERE backup.rental_id = rentals.id) "
                f"WHERE EXISTS (SELECT 1 FROM {_ROLLBACK_BACKUP_TABLE} backup "
                "WHERE backup.rental_id = rentals.id)"
            ))
        op.drop_table(_ROLLBACK_BACKUP_TABLE)


def downgrade() -> None:
    if _has_rollback_backup():
        op.drop_table(_ROLLBACK_BACKUP_TABLE)
    op.create_table(
        _ROLLBACK_BACKUP_TABLE,
        sa.Column("rental_id", sa.Integer(), primary_key=True),
        sa.Column("credentials_delivery_status", sa.String(length=16), nullable=False),
        sa.Column("credentials_delivery_template", sa.String(length=32), nullable=False),
        sa.Column("credentials_delivery_started_at", sa.DateTime(timezone=True)),
        sa.Column("credentials_delivered_at", sa.DateTime(timezone=True)),
        sa.Column("credentials_delivery_attempts", sa.Integer(), nullable=False),
        sa.Column("credentials_delivery_last_error", sa.String(length=128)),
    )
    op.execute(sa.text(
        f"INSERT INTO {_ROLLBACK_BACKUP_TABLE} ("
        "rental_id, credentials_delivery_status, credentials_delivery_template, "
        "credentials_delivery_started_at, credentials_delivered_at, "
        "credentials_delivery_attempts, credentials_delivery_last_error) "
        "SELECT id, credentials_delivery_status, credentials_delivery_template, "
        "credentials_delivery_started_at, credentials_delivered_at, "
        "credentials_delivery_attempts, credentials_delivery_last_error FROM rentals"
    ))
    with op.batch_alter_table("rentals") as batch_op:
        batch_op.drop_column("credentials_delivery_last_error")
        batch_op.drop_column("credentials_delivery_attempts")
        batch_op.drop_column("credentials_delivered_at")
        batch_op.drop_column("credentials_delivery_started_at")
        batch_op.drop_column("credentials_delivery_template")
        batch_op.drop_column("credentials_delivery_status")
