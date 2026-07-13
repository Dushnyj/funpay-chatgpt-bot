"""Persist operator suspension intent and fair credential-delivery retries.

Revision ID: 20260713_0011
Revises: 20260713_0010
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


revision: str = "20260713_0011"
down_revision: str | None = "20260713_0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(
            sa.Column("operator_status_override", sa.String(length=16), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "validation_rerun_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    accounts = sa.table(
        "accounts",
        sa.column("status", sa.String(length=32)),
        sa.column("operator_status_override", sa.String(length=16)),
    )
    op.execute(
        accounts.update()
        # The old schema could not distinguish automatic from operator-set
        # maintenance. Preserve both suspension states and fail closed; an
        # operator can explicitly re-enable a false-positive after upgrade.
        .where(accounts.c.status.in_(["disabled", "maintenance"]))
        .values(operator_status_override=accounts.c.status)
    )

    with op.batch_alter_table("account_limits") as batch:
        batch.add_column(
            sa.Column(
                "plan_window_status",
                sa.String(length=24),
                nullable=False,
                server_default="unknown",
            )
        )
        batch.add_column(
            sa.Column("expected_long_window_seconds", sa.Integer(), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "low_limit_warning_fingerprint",
                sa.String(length=160),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "low_limit_warned_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

    # Existing observations predate the plan/window contract. Force an
    # immediate safe re-measure instead of silently treating stale rows as
    # sellable or waiting for an old multi-day interval.
    account_limits = sa.table(
        "account_limits",
        sa.column("measured_at", sa.DateTime(timezone=True)),
    )
    op.execute(account_limits.update().values(measured_at=None))
    seller_settings = sa.table(
        "seller_settings",
        sa.column("limits_check_interval_minutes", sa.Integer()),
    )
    op.execute(
        seller_settings.update()
        .where(seller_settings.c.limits_check_interval_minutes > 55)
        .values(limits_check_interval_minutes=55)
    )

    # Free has only the observed 30-day Codex window. A legacy 5-hour ceiling
    # on Free+ANY cannot be evaluated and the current UI intentionally hides
    # that control, so normalize stale rows instead of leaving a hidden filter
    # active in generated lot configuration.
    price_matrix = sa.table(
        "price_matrix",
        sa.column("tier_id", sa.Integer()),
        sa.column("limit_scope_id", sa.Integer()),
        sa.column("max_5h_pct", sa.Integer()),
    )
    subscription_tiers = sa.table(
        "subscription_tiers",
        sa.column("id", sa.Integer()),
        sa.column("code", sa.String()),
    )
    limit_scopes = sa.table(
        "limit_scopes",
        sa.column("id", sa.Integer()),
        sa.column("code", sa.String()),
    )
    free_tier_id = (
        sa.select(subscription_tiers.c.id)
        .where(subscription_tiers.c.code == "free")
        .scalar_subquery()
    )
    any_scope_id = (
        sa.select(limit_scopes.c.id)
        .where(limit_scopes.c.code == "any")
        .scalar_subquery()
    )
    op.execute(
        price_matrix.update()
        .where(
            price_matrix.c.tier_id == free_tier_id,
            price_matrix.c.limit_scope_id == any_scope_id,
            price_matrix.c.max_5h_pct.is_not(None),
        )
        .values(max_5h_pct=None)
    )
    with op.batch_alter_table("seller_settings") as batch:
        batch.add_column(
            sa.Column(
                "admin_login_failure_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "admin_login_window_started_at",
                sa.DateTime(timezone=True),
            )
        )
        batch.add_column(
            sa.Column(
                "admin_login_blocked_until",
                sa.DateTime(timezone=True),
            )
        )

    with op.batch_alter_table("orders") as batch:
        batch.add_column(
            sa.Column(
                "fulfillment_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column("fulfillment_next_attempt_at", sa.DateTime(timezone=True))
        )
        batch.add_column(
            sa.Column("fulfillment_last_error", sa.String(length=128))
        )
        batch.create_index(
            "ix_orders_fulfillment_next_attempt_at",
            ["fulfillment_next_attempt_at"],
        )

    with op.batch_alter_table("rentals") as batch:
        batch.add_column(
            sa.Column(
                "credentials_delivery_next_attempt_at",
                sa.DateTime(timezone=True),
            )
        )
        batch.add_column(sa.Column("issued_codex_primary_pct", sa.Integer()))
        batch.add_column(
            sa.Column("issued_codex_primary_window_seconds", sa.Integer())
        )
        batch.add_column(
            sa.Column(
                "issued_codex_primary_resets_at",
                sa.DateTime(timezone=True),
            )
        )
        batch.add_column(sa.Column("issued_codex_secondary_pct", sa.Integer()))
        batch.add_column(
            sa.Column("issued_codex_secondary_window_seconds", sa.Integer())
        )
        batch.add_column(
            sa.Column(
                "issued_codex_secondary_resets_at",
                sa.DateTime(timezone=True),
            )
        )
        batch.add_column(
            sa.Column("issued_plan_window_status", sa.String(length=24))
        )
        batch.add_column(
            sa.Column("issued_expected_long_window_seconds", sa.Integer())
        )
        batch.add_column(
            sa.Column("issued_limits_measured_at", sa.DateTime(timezone=True))
        )
        batch.create_index(
            "ix_rentals_credentials_delivery_retry",
            ["credentials_delivery_status", "credentials_delivery_next_attempt_at"],
        )

    with op.batch_alter_table("chat_conversations") as batch:
        batch.alter_column(
            "last_message_text",
            existing_type=sa.String(length=4000),
            type_=sa.Text(),
            existing_nullable=True,
        )
    with op.batch_alter_table("chat_messages") as batch:
        batch.alter_column(
            "text",
            existing_type=sa.String(length=4000),
            type_=sa.Text(),
            existing_nullable=False,
        )
    _encrypt_legacy_chat_column("chat_conversations", "id", "last_message_text")
    _encrypt_legacy_chat_column("chat_messages", "id", "text")


def downgrade() -> None:
    # Never materialize credential-bearing chat history as plaintext. Revision
    # 0010 may display ciphertext after rollback, but confidentiality must fail
    # closed and a later 0011 upgrade can reuse the preserved Fernet values.
    with op.batch_alter_table("chat_conversations") as batch:
        batch.alter_column(
            "last_message_text",
            existing_type=sa.Text(),
            type_=sa.String(length=4000),
            existing_nullable=True,
        )
    with op.batch_alter_table("chat_messages") as batch:
        batch.alter_column(
            "text",
            existing_type=sa.Text(),
            type_=sa.String(length=4000),
            existing_nullable=False,
        )

    with op.batch_alter_table("rentals") as batch:
        batch.drop_index("ix_rentals_credentials_delivery_retry")
        batch.drop_column("issued_limits_measured_at")
        batch.drop_column("issued_expected_long_window_seconds")
        batch.drop_column("issued_plan_window_status")
        batch.drop_column("issued_codex_secondary_resets_at")
        batch.drop_column("issued_codex_secondary_window_seconds")
        batch.drop_column("issued_codex_secondary_pct")
        batch.drop_column("issued_codex_primary_resets_at")
        batch.drop_column("issued_codex_primary_window_seconds")
        batch.drop_column("issued_codex_primary_pct")
        batch.drop_column("credentials_delivery_next_attempt_at")

    with op.batch_alter_table("seller_settings") as batch:
        batch.drop_column("admin_login_blocked_until")
        batch.drop_column("admin_login_window_started_at")
        batch.drop_column("admin_login_failure_count")

    with op.batch_alter_table("orders") as batch:
        batch.drop_index("ix_orders_fulfillment_next_attempt_at")
        batch.drop_column("fulfillment_last_error")
        batch.drop_column("fulfillment_next_attempt_at")
        batch.drop_column("fulfillment_attempts")

    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("validation_rerun_requested")
        batch.drop_column("operator_status_override")
    with op.batch_alter_table("account_limits") as batch:
        batch.drop_column("low_limit_warned_at")
        batch.drop_column("low_limit_warning_fingerprint")
        batch.drop_column("expected_long_window_seconds")
        batch.drop_column("plan_window_status")


def _encrypt_legacy_chat_column(
    table_name: str,
    id_column: str,
    value_column: str,
) -> None:
    bind = op.get_bind()
    table = sa.table(
        table_name,
        sa.column(id_column, sa.Integer()),
        sa.column(value_column, sa.Text()),
    )
    fernet = Fernet(get_settings().encryption_key.encode())
    for row in bind.execute(
        sa.select(table.c[id_column], table.c[value_column])
    ).mappings():
        value = row[value_column]
        if value is None:
            continue
        encoded = str(value).encode()
        try:
            fernet.decrypt(encoded)
            encrypted = str(value)
        except InvalidToken:
            if str(value).startswith("gAAAA"):
                raise RuntimeError(
                    f"Cannot decrypt {table_name}.{value_column} row "
                    f"{row[id_column]}; verify ENCRYPTION_KEY"
                )
            encrypted = fernet.encrypt(encoded).decode()
        if encrypted != value:
            bind.execute(
                table.update()
                .where(table.c[id_column] == row[id_column])
                .values({value_column: encrypted})
            )
