"""Add browser-only proxy routes and one-time home relay enrollment.

Revision ID: 20260716_0022
Revises: 20260715_0021
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260716_0022"
down_revision: str | None = "20260715_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proxy_routes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("proxy_type", sa.String(length=12), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username_encrypted", sa.String(), nullable=True),
        sa.Column("password_encrypted", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "config_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("egress_ip", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('home_relay', 'custom_proxy')",
            name=op.f("ck_proxy_routes_mode_supported"),
        ),
        sa.CheckConstraint(
            "proxy_type IN ('http', 'https', 'socks5')",
            name=op.f("ck_proxy_routes_proxy_type_supported"),
        ),
        sa.CheckConstraint(
            "port >= 1 AND port <= 65535",
            name=op.f("ck_proxy_routes_port_range"),
        ),
        sa.CheckConstraint(
            "status IN ('unchecked', 'online', 'offline')",
            name=op.f("ck_proxy_routes_status_supported"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proxy_routes")),
        sa.UniqueConstraint("name", name=op.f("uq_proxy_routes_name")),
    )
    op.create_index(
        "uq_proxy_routes_single_home_relay",
        "proxy_routes",
        ["mode"],
        unique=True,
        postgresql_where=sa.text("mode = 'home_relay'"),
        sqlite_where=sa.text("mode = 'home_relay'"),
    )
    op.create_table(
        "home_relay_setups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("route_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("machine_name", sa.String(length=128), nullable=True),
        sa.Column("public_key_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["proxy_routes.id"],
            name=op.f("fk_home_relay_setups_route_id_proxy_routes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_home_relay_setups")),
        sa.UniqueConstraint(
            "token_hash", name=op.f("uq_home_relay_setups_token_hash")
        ),
    )
    op.create_index(
        op.f("ix_home_relay_setups_route_id"),
        "home_relay_setups",
        ["route_id"],
        unique=False,
    )
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(
            sa.Column("proxy_route_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            op.f("fk_accounts_proxy_route_id_proxy_routes"),
            "proxy_routes",
            ["proxy_route_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("seller_settings") as batch_op:
        batch_op.add_column(
            sa.Column("default_proxy_route_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            op.f("fk_seller_settings_default_proxy_route_id_proxy_routes"),
            "proxy_routes",
            ["default_proxy_route_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("seller_settings") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_seller_settings_default_proxy_route_id_proxy_routes"),
            type_="foreignkey",
        )
        batch_op.drop_column("default_proxy_route_id")
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_accounts_proxy_route_id_proxy_routes"),
            type_="foreignkey",
        )
        batch_op.drop_column("proxy_route_id")
    op.drop_index(
        op.f("ix_home_relay_setups_route_id"),
        table_name="home_relay_setups",
    )
    op.drop_table("home_relay_setups")
    op.drop_table("proxy_routes")
