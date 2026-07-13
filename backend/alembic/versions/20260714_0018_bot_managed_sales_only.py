"""Restrict FunPay sale provenance to orders of bot-managed lots.

Revision ID: 20260714_0018
Revises: 20260714_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260714_0018"
down_revision: str | None = "20260714_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quarantine_unmanaged_sales(connection: sa.engine.Connection) -> None:
    """Keep only sales with an immutable binding snapshot to their local lot."""

    connection.execute(
        sa.text(
            "DELETE FROM funpay_sales "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM orders "
            "JOIN lots ON lots.id = orders.lot_id "
            "WHERE orders.id = funpay_sales.order_id "
            "AND orders.funpay_order_id = funpay_sales.funpay_order_id "
            "AND orders.buyer_funpay_id = funpay_sales.buyer_funpay_id "
            "AND orders.funpay_chat_id = funpay_sales.funpay_chat_id "
            "AND (("
            "orders.lot_binding_method = 'offer_id' "
            "AND orders.funpay_offer_id IS NOT NULL "
            "AND lots.funpay_id IS NOT NULL "
            "AND orders.funpay_offer_id = lots.funpay_id"
            ") OR ("
            "orders.lot_binding_method = 'provenance_token' "
            "AND orders.lot_provenance_token IS NOT NULL "
            "AND lots.provenance_token IS NOT NULL "
            "AND lots.provenance_marker_synced = true "
            "AND orders.lot_provenance_token = lots.provenance_token"
            ")))"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE chat_conversations SET verified_sale = false "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM funpay_sales "
            "JOIN orders ON orders.id = funpay_sales.order_id "
            "JOIN lots ON lots.id = orders.lot_id "
            "WHERE funpay_sales.funpay_chat_id = "
            "chat_conversations.funpay_chat_id "
            "AND funpay_sales.buyer_funpay_id = "
            "chat_conversations.buyer_funpay_id "
            "AND orders.funpay_order_id = funpay_sales.funpay_order_id "
            "AND orders.buyer_funpay_id = funpay_sales.buyer_funpay_id "
            "AND orders.funpay_chat_id = funpay_sales.funpay_chat_id "
            "AND (("
            "orders.lot_binding_method = 'offer_id' "
            "AND orders.funpay_offer_id IS NOT NULL "
            "AND lots.funpay_id IS NOT NULL "
            "AND orders.funpay_offer_id = lots.funpay_id"
            ") OR ("
            "orders.lot_binding_method = 'provenance_token' "
            "AND orders.lot_provenance_token IS NOT NULL "
            "AND lots.provenance_token IS NOT NULL "
            "AND lots.provenance_marker_synced = true "
            "AND orders.lot_provenance_token = lots.provenance_token"
            ")))"
        )
    )
    # Empty shells came only from broad history enrichment. Conversations with
    # messages stay encrypted for audit, but cannot be exposed by a stale flag.
    connection.execute(
        sa.text(
            "DELETE FROM chat_conversations "
            "WHERE verified_sale = false "
            "AND NOT EXISTS ("
            "SELECT 1 FROM chat_messages "
            "WHERE chat_messages.conversation_id = chat_conversations.id)"
        )
    )


def upgrade() -> None:
    with op.batch_alter_table("lots") as batch_op:
        batch_op.add_column(
            sa.Column("provenance_token", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "provenance_marker_synced",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "UPDATE lots SET provenance_token = "
                "md5(random()::text || clock_timestamp()::text || id::text) "
                "WHERE provenance_token IS NULL"
            )
        )
    elif connection.dialect.name == "sqlite":
        connection.execute(
            sa.text(
                "UPDATE lots SET provenance_token = lower(hex(randomblob(16))) "
                "WHERE provenance_token IS NULL"
            )
        )
    else:
        raise RuntimeError(
            "0018 provenance token backfill supports PostgreSQL and SQLite only"
        )
    with op.batch_alter_table("lots") as batch_op:
        batch_op.alter_column(
            "provenance_token",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_lots_provenance_token",
            ["provenance_token"],
        )

    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(
            sa.Column("lot_binding_method", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("funpay_offer_id", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("lot_provenance_token", sa.String(length=32), nullable=True)
        )
        batch_op.create_check_constraint(
            "bot_lot_binding_shape",
            "(lot_binding_method IS NULL AND funpay_offer_id IS NULL "
            "AND lot_provenance_token IS NULL) OR "
            "(lot_binding_method = 'offer_id' AND lot_id IS NOT NULL "
            "AND funpay_offer_id IS NOT NULL "
            "AND lot_provenance_token IS NULL) OR "
            "(lot_binding_method = 'provenance_token' AND lot_id IS NOT NULL "
            "AND funpay_offer_id IS NULL "
            "AND lot_provenance_token IS NOT NULL)",
        )

    # Existing orders deliberately remain unbound. A local lot_id alone is not
    # proof that the order came from an offer published by this bot.
    _quarantine_unmanaged_sales(op.get_bind())
    op.execute(
        sa.text(
            "UPDATE funpay_sale_sync_state SET "
            "backfill_cursor = NULL, backfill_complete = true"
        )
    )

    with op.batch_alter_table("funpay_sales") as batch_op:
        batch_op.drop_constraint(
            "fk_funpay_sales_order_id_orders",
            type_="foreignkey",
        )
        batch_op.alter_column(
            "order_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_funpay_sales_order_id_orders",
            "orders",
            ["order_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("funpay_sales") as batch_op:
        batch_op.drop_constraint(
            "fk_funpay_sales_order_id_orders",
            type_="foreignkey",
        )
        batch_op.alter_column(
            "order_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.create_foreign_key(
            "fk_funpay_sales_order_id_orders",
            "orders",
            ["order_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 0017 periodically scans seller-wide history.  Re-enable that cursor on
    # rollback so its older runtime is not left permanently marked complete.
    op.execute(
        sa.text(
            "UPDATE funpay_sale_sync_state SET "
            "backfill_cursor = NULL, backfill_complete = false"
        )
    )

    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_constraint(
            "bot_lot_binding_shape",
            type_="check",
        )
        batch_op.drop_column("lot_provenance_token")
        batch_op.drop_column("funpay_offer_id")
        batch_op.drop_column("lot_binding_method")

    with op.batch_alter_table("lots") as batch_op:
        batch_op.drop_constraint(
            "uq_lots_provenance_token",
            type_="unique",
        )
        batch_op.drop_column("provenance_marker_synced")
        batch_op.drop_column("provenance_token")
