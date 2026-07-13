"""Store rental duration in minutes and retire the Chat limit model.

Revision ID: 20260713_0015
Revises: 20260713_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0015"
down_revision: str | None = "20260713_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CHAT_REFERENCE_TABLES = (
    "price_matrix",
    "lots",
    "orders",
    "rentals",
    "lot_templates",
)


def upgrade() -> None:
    connection = op.get_bind()
    shared_account = connection.execute(
        sa.text(
            "SELECT account_id FROM rentals WHERE status IN "
            "('active', 'expiry_pending') GROUP BY account_id "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if shared_account is not None:
        raise RuntimeError(
            "Cannot enforce one-account/one-renter safety: account "
            f"{shared_account} has multiple live rentals. Resolve them first."
        )
    chat_id = connection.execute(
        sa.text("SELECT id FROM limit_scopes WHERE code = 'chat'")
    ).scalar_one_or_none()
    if chat_id is not None:
        live_references = {
            "rentals": int(
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM rentals WHERE limit_scope_id = "
                        ":chat_id AND status IN ('active', 'expiry_pending')"
                    ),
                    {"chat_id": chat_id},
                ).scalar_one()
            ),
            "orders": int(
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM orders o LEFT JOIN rentals r ON "
                        "r.order_id = o.id WHERE o.limit_scope_id = :chat_id "
                        "AND o.status IN ('pending', 'completed') AND r.id IS NULL"
                    ),
                    {"chat_id": chat_id},
                ).scalar_one()
            ),
            "lots": int(
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM lots WHERE limit_scope_id = "
                        ":chat_id AND status = 'active'"
                    ),
                    {"chat_id": chat_id},
                ).scalar_one()
            ),
        }
        if any(live_references.values()):
            detail = ", ".join(
                f"{name}={count}"
                for name, count in live_references.items()
                if count
            )
            raise RuntimeError(
                "Cannot retire the Chat limit scope while live buyer state "
                f"exists ({detail}). Finish, refund, or migrate it first."
            )

    # OpenAI session revocation is account-wide. Clamp legacy overrides before
    # enforcing the one-account/one-renter safety invariant.
    connection.execute(
        sa.text(
            "UPDATE accounts SET max_active_rentals = 1 "
            "WHERE max_active_rentals IS NOT NULL AND max_active_rentals != 1"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE seller_settings SET default_max_active_rentals = 1 "
            "WHERE default_max_active_rentals != 1"
        )
    )
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_accounts_single_active_rental"),
            "max_active_rentals IS NULL OR max_active_rentals = 1",
        )
    with op.batch_alter_table("seller_settings") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_seller_settings_single_active_rental"),
            "default_max_active_rentals = 1",
        )

    # A rename plus an in-place scale preserves duration IDs and therefore all
    # price/lot/order/rental foreign keys and stable lot config signatures.
    with op.batch_alter_table("durations") as batch_op:
        batch_op.drop_constraint(
            op.f("uq_durations_days"),
            type_="unique",
        )
    op.alter_column(
        "durations",
        "days",
        new_column_name="minutes",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    connection.execute(
        sa.text(
            "UPDATE durations SET minutes = minutes * 1440, "
            "sort_order = minutes * 1440"
        )
    )
    with op.batch_alter_table("durations") as batch_op:
        batch_op.create_unique_constraint(
            op.f("uq_durations_minutes"),
            ["minutes"],
        )
        batch_op.create_check_constraint(
            op.f("ck_durations_duration_minutes_range_step"),
            "minutes >= 30 AND minutes <= 43200 AND minutes % 30 = 0",
        )

    # Legacy Chat offers are retained as disabled tombstone references. Active
    # API queries and reconciliation exclude them, while an operator can still
    # recover historical configuration directly from the database.
    connection.execute(
        sa.text(
            "UPDATE lot_templates SET is_enabled = false WHERE "
            "limit_scope_id IN (SELECT id FROM limit_scopes WHERE code = 'chat')"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE limit_scopes SET is_enabled = false, sort_order = 100 "
            "WHERE code = 'chat'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE limit_scopes SET name = 'Codex', sort_order = 20 "
            "WHERE code = 'codex'"
        )
    )

    # Production installations without historical references can remove the
    # obsolete scope completely. Other databases keep a disabled tombstone so
    # immutable order/rental history and old lots retain valid foreign keys.
    chat_id = connection.execute(
        sa.text("SELECT id FROM limit_scopes WHERE code = 'chat'")
    ).scalar_one_or_none()
    if chat_id is not None:
        reference_count = sum(
            int(
                connection.execute(
                    sa.text(
                        f"SELECT COUNT(*) FROM {table} "  # noqa: S608
                        "WHERE limit_scope_id = :chat_id"
                    ),
                    {"chat_id": chat_id},
                ).scalar_one()
            )
            for table in _CHAT_REFERENCE_TABLES
        )
        if reference_count == 0:
            connection.execute(
                sa.text("DELETE FROM limit_scopes WHERE id = :chat_id"),
                {"chat_id": chat_id},
            )

    # These nullable columns contained no trustworthy OpenAI measurement. The
    # exact primary/secondary agentic windows remain untouched.
    with op.batch_alter_table("account_limits") as batch_op:
        batch_op.drop_column("chat_5h_remaining_pct")
        batch_op.drop_column("chat_weekly_remaining_pct")
    with op.batch_alter_table("rentals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "expiry_revoke_started_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "expiry_notified_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "replacement_target_account_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            op.f("fk_rentals_replacement_target_account_id_accounts"),
            "accounts",
            ["replacement_target_account_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            op.f("uq_rentals_replacement_target_account_id"),
            ["replacement_target_account_id"],
        )
        batch_op.drop_column("issued_chat_5h_pct")
        batch_op.drop_column("issued_chat_weekly_pct")
    # Historical terminal rentals predate the durable notification outbox and
    # must not send old expiry messages immediately after this deployment.
    connection.execute(
        sa.text(
            "UPDATE rentals SET expiry_notified_at = CURRENT_TIMESTAMP "
            "WHERE status NOT IN ('active', 'expiry_pending')"
        )
    )
    op.create_index(
        "uq_rentals_one_occupying_account",
        "rentals",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('active', 'expiry_pending')"
        ),
        sqlite_where=sa.text(
            "status IN ('active', 'expiry_pending')"
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    non_day_duration = connection.execute(
        sa.text("SELECT id FROM durations WHERE minutes % 1440 != 0 LIMIT 1")
    ).scalar_one_or_none()
    if non_day_duration is not None:
        raise RuntimeError(
            "Cannot downgrade durations: sub-day values cannot be represented "
            "by the previous whole-day schema"
        )

    op.drop_index(
        "uq_rentals_one_occupying_account",
        table_name="rentals",
    )

    with op.batch_alter_table("seller_settings") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_seller_settings_single_active_rental"),
            type_="check",
        )
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_accounts_single_active_rental"),
            type_="check",
        )

    with op.batch_alter_table("account_limits") as batch_op:
        batch_op.add_column(
            sa.Column("chat_5h_remaining_pct", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("chat_weekly_remaining_pct", sa.Integer(), nullable=True)
        )
    with op.batch_alter_table("rentals") as batch_op:
        batch_op.drop_constraint(
            op.f("uq_rentals_replacement_target_account_id"),
            type_="unique",
        )
        batch_op.drop_constraint(
            op.f("fk_rentals_replacement_target_account_id_accounts"),
            type_="foreignkey",
        )
        batch_op.drop_column("replacement_target_account_id")
        batch_op.drop_column("expiry_notified_at")
        batch_op.drop_column("expiry_revoke_started_at")
        batch_op.add_column(
            sa.Column("issued_chat_5h_pct", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("issued_chat_weekly_pct", sa.Integer(), nullable=True)
        )

    # Revision 0013 shipped the Chat row disabled. Recreate it only when the
    # upgrade safely removed an unreferenced row.
    if connection.execute(
        sa.text("SELECT id FROM limit_scopes WHERE code = 'chat'")
    ).scalar_one_or_none() is None:
        connection.execute(
            sa.text(
                "INSERT INTO limit_scopes "
                "(code, name, is_enabled, sort_order) "
                "VALUES ('chat', 'Chat', false, 20)"
            )
        )
    connection.execute(
        sa.text(
            "UPDATE limit_scopes SET sort_order = 30 WHERE code = 'codex'"
        )
    )

    with op.batch_alter_table("durations") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_durations_duration_minutes_range_step"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("uq_durations_minutes"),
            type_="unique",
        )
    connection.execute(
        sa.text(
            "UPDATE durations SET minutes = minutes / 1440, "
            "sort_order = minutes / 1440"
        )
    )
    op.alter_column(
        "durations",
        "minutes",
        new_column_name="days",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    with op.batch_alter_table("durations") as batch_op:
        batch_op.create_unique_constraint(
            op.f("uq_durations_days"),
            ["days"],
        )
