"""Add durable admin chat tables.

Revision ID: 20260713_0002
Revises: 20260713_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("funpay_chat_id", sa.String(length=64), nullable=False),
        sa.Column("buyer_funpay_id", sa.String(length=64), nullable=True),
        sa.Column("funpay_order_id", sa.String(length=64), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("unread_count", sa.Integer(), nullable=False),
        sa.Column("last_message_text", sa.String(length=4000), nullable=True),
        sa.Column("last_message_direction", sa.String(length=16), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_chat_conversations_order_id_orders"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_conversations")),
    )
    op.create_index(
        "ix_chat_conversations_funpay_chat_id",
        "chat_conversations",
        ["funpay_chat_id"],
        unique=True,
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("funpay_message_id", sa.String(length=64), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("sender_funpay_id", sa.String(length=64), nullable=True),
        sa.Column("text", sa.String(length=4000), nullable=False),
        sa.Column("delivery_status", sa.String(length=16), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["chat_conversations.id"],
            name=op.f("fk_chat_messages_conversation_id_chat_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
        sa.UniqueConstraint(
            "conversation_id", "funpay_message_id", name="uq_chat_message_source"
        ),
    )
    op.create_index(
        "ix_chat_messages_conversation_created",
        "chat_messages",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_messages_conversation_created", table_name="chat_messages"
    )
    op.drop_table("chat_messages")
    op.drop_index(
        "ix_chat_conversations_funpay_chat_id", table_name="chat_conversations"
    )
    op.drop_table("chat_conversations")
