"""Separate verified FunPay sales from untrusted chat traffic.

Revision ID: 20260713_0016
Revises: 20260713_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0016"
down_revision: str | None = "20260713_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "funpay_sales",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("funpay_order_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("funpay_chat_id", sa.String(length=64), nullable=True),
        sa.Column("buyer_funpay_id", sa.String(length=64), nullable=False),
        sa.Column("buyer_username", sa.String(length=128), nullable=True),
        sa.Column("buyer_avatar_url", sa.String(length=2048), nullable=True),
        sa.Column("buyer_is_online", sa.Boolean(), nullable=True),
        sa.Column("buyer_status_text", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("profile_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "detail_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "detail_next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_funpay_sales_order_id_orders"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_funpay_sales")),
        sa.UniqueConstraint(
            "funpay_order_id", name=op.f("uq_funpay_sales_funpay_order_id")
        ),
        sa.UniqueConstraint("order_id", name=op.f("uq_funpay_sales_order_id")),
    )
    op.create_index(
        "ix_funpay_sales_funpay_order_id",
        "funpay_sales",
        ["funpay_order_id"],
        unique=True,
    )
    op.create_index(
        "ix_funpay_sales_chat_buyer",
        "funpay_sales",
        ["funpay_chat_id", "buyer_funpay_id"],
        unique=False,
    )
    op.create_index(
        "ix_funpay_sales_buyer",
        "funpay_sales",
        ["buyer_funpay_id"],
        unique=False,
    )

    with op.batch_alter_table("chat_conversations") as batch_op:
        batch_op.add_column(
            sa.Column("buyer_username", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column("buyer_avatar_url", sa.String(length=2048), nullable=True)
        )
        batch_op.add_column(
            sa.Column("buyer_is_online", sa.Boolean(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("buyer_status_text", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column("profile_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "profile_attempts",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "profile_next_attempt_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "verified_sale",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.create_index(
        "ix_chat_conversations_verified_sale",
        "chat_conversations",
        ["verified_sale"],
        unique=False,
    )

    connection = op.get_bind()
    # Existing Order rows were historically created only by NewSaleEvent. They
    # are therefore safe bootstrap provenance, even when profile details were
    # not retained by the old schema.
    connection.execute(
        sa.text(
            "INSERT INTO funpay_sales "
            "(funpay_order_id, order_id, funpay_chat_id, buyer_funpay_id, "
            "status, created_at, updated_at) "
            "SELECT funpay_order_id, id, funpay_chat_id, buyer_funpay_id, "
            "CASE "
            "WHEN status = 'pending' THEN 'paid' "
            "WHEN status = 'completed' THEN 'completed' "
            "WHEN status IN ('refunded', 'refund_pending') THEN 'refunded' "
            "ELSE 'unknown' END, "
            "created_at, CURRENT_TIMESTAMP FROM orders"
        )
    )
    # Do not bless every historical chat. Only an exact local order plus the
    # same FunPay chat/buyer tuple proves that the peer was our buyer.
    connection.execute(
        sa.text(
            "UPDATE chat_conversations SET "
            "verified_sale = true, "
            "buyer_funpay_id = ("
            "SELECT s.buyer_funpay_id FROM funpay_sales s "
            "WHERE (s.order_id = chat_conversations.order_id OR "
            "s.funpay_order_id = chat_conversations.funpay_order_id) "
            "AND s.funpay_chat_id = chat_conversations.funpay_chat_id "
            "LIMIT 1) "
            "WHERE EXISTS ("
            "SELECT 1 FROM funpay_sales s "
            "WHERE (s.order_id = chat_conversations.order_id OR "
            "s.funpay_order_id = chat_conversations.funpay_order_id) "
            "AND s.funpay_chat_id = chat_conversations.funpay_chat_id "
            "AND (chat_conversations.buyer_funpay_id IS NULL OR "
            "chat_conversations.buyer_funpay_id = s.buyer_funpay_id))"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_conversations_verified_sale", table_name="chat_conversations"
    )
    with op.batch_alter_table("chat_conversations") as batch_op:
        batch_op.drop_column("verified_sale")
        batch_op.drop_column("profile_next_attempt_at")
        batch_op.drop_column("profile_attempts")
        batch_op.drop_column("profile_checked_at")
        batch_op.drop_column("buyer_status_text")
        batch_op.drop_column("buyer_is_online")
        batch_op.drop_column("buyer_avatar_url")
        batch_op.drop_column("buyer_username")

    op.drop_index("ix_funpay_sales_buyer", table_name="funpay_sales")
    op.drop_index("ix_funpay_sales_chat_buyer", table_name="funpay_sales")
    op.drop_index("ix_funpay_sales_funpay_order_id", table_name="funpay_sales")
    op.drop_table("funpay_sales")
