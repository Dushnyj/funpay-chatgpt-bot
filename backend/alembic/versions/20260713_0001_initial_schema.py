"""Initial application schema.

Revision ID: 20260713_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "20260713_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("rental_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.String(), nullable=True),
        sa.Column("message_text", sa.String(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_table(
        "durations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_durations")),
        sa.UniqueConstraint("days", name=op.f("uq_durations_days")),
    )
    op.create_table(
        "limit_scopes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_limit_scopes")),
        sa.UniqueConstraint("code", name=op.f("uq_limit_scopes_code")),
        sa.UniqueConstraint("name", name=op.f("uq_limit_scopes_name")),
    )
    op.create_table(
        "message_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("lang", sa.String(length=8), nullable=False),
        sa.Column("content", sa.String(length=4000), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_templates")),
        sa.UniqueConstraint("key", "lang", name="uq_message_key_lang"),
    )
    op.create_table(
        "seller_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("funpay_session_key", sa.String(), nullable=True),
        sa.Column("funpay_session_valid", sa.Boolean(), nullable=False),
        sa.Column("funpay_node_id", sa.Integer(), nullable=True),
        sa.Column("telegram_bot_token", sa.String(), nullable=True),
        sa.Column("telegram_seller_chat_id", sa.String(), nullable=True),
        sa.Column("check_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("limits_check_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("refresh_recover_concurrency", sa.Integer(), nullable=False),
        sa.Column("refresh_max_attempts", sa.Integer(), nullable=False),
        sa.Column("refresh_retry_delay_minutes", sa.Integer(), nullable=False),
        sa.Column("check_delay_seconds", sa.Integer(), nullable=False),
        sa.Column("bump_interval_hours", sa.Integer(), nullable=False),
        sa.Column("auto_bump_enabled", sa.Boolean(), nullable=False),
        sa.Column("default_max_active_rentals", sa.Integer(), nullable=False),
        sa.Column("funpay_commission_percent", sa.Integer(), nullable=False),
        sa.Column("limits_warn_threshold_pct", sa.Integer(), nullable=False),
        sa.Column("admin_password_hash", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_settings")),
    )
    op.create_table(
        "subscription_tiers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_tiers")),
        sa.UniqueConstraint("name", name=op.f("uq_subscription_tiers_name")),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("login", sa.String(), nullable=False),
        sa.Column("password_encrypted", sa.String(), nullable=False),
        sa.Column("totp_secret_encrypted", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("email_password_encrypted", sa.String(), nullable=True),
        sa.Column("tier_id", sa.Integer(), nullable=False),
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_active_rentals", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("chatgpt_last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tier_id"], ["subscription_tiers.id"], name=op.f("fk_accounts_tier_id_subscription_tiers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("login", name=op.f("uq_accounts_login")),
    )
    op.create_table(
        "lot_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tier_id", sa.Integer(), nullable=True),
        sa.Column("limit_scope_id", sa.Integer(), nullable=True),
        sa.Column("title_template_ru", sa.String(length=255), nullable=False),
        sa.Column("title_template_en", sa.String(length=255), nullable=False),
        sa.Column("description_template_ru", sa.String(length=4000), nullable=False),
        sa.Column("description_template_en", sa.String(length=4000), nullable=False),
        sa.ForeignKeyConstraint(
            ["limit_scope_id"], ["limit_scopes.id"], name=op.f("fk_lot_templates_limit_scope_id_limit_scopes")
        ),
        sa.ForeignKeyConstraint(
            ["tier_id"], ["subscription_tiers.id"], name=op.f("fk_lot_templates_tier_id_subscription_tiers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lot_templates")),
    )
    op.create_table(
        "lots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("funpay_id", sa.String(), nullable=True),
        sa.Column("funpay_node_id", sa.Integer(), nullable=True),
        sa.Column("tier_id", sa.Integer(), nullable=False),
        sa.Column("duration_id", sa.Integer(), nullable=False),
        sa.Column("limit_scope_id", sa.Integer(), nullable=False),
        sa.Column("min_limit_pct", sa.Integer(), nullable=True),
        sa.Column("max_5h_pct", sa.Integer(), nullable=True),
        sa.Column("max_weekly_pct", sa.Integer(), nullable=True),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("title_ru", sa.String(length=255), nullable=False),
        sa.Column("title_en", sa.String(length=255), nullable=False),
        sa.Column("description_ru", sa.String(length=4000), nullable=False),
        sa.Column("description_en", sa.String(length=4000), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("paused_reason", sa.String(), nullable=True),
        sa.Column("auto_created", sa.Boolean(), nullable=False),
        sa.Column("config_key", sa.String(length=96), nullable=False),
        sa.ForeignKeyConstraint(["duration_id"], ["durations.id"], name=op.f("fk_lots_duration_id_durations")),
        sa.ForeignKeyConstraint(["limit_scope_id"], ["limit_scopes.id"], name=op.f("fk_lots_limit_scope_id_limit_scopes")),
        sa.ForeignKeyConstraint(["tier_id"], ["subscription_tiers.id"], name=op.f("fk_lots_tier_id_subscription_tiers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lots")),
        sa.UniqueConstraint("config_key", name=op.f("uq_lots_config_key")),
    )
    op.create_table(
        "price_matrix",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tier_id", sa.Integer(), nullable=False),
        sa.Column("duration_id", sa.Integer(), nullable=False),
        sa.Column("limit_scope_id", sa.Integer(), nullable=False),
        sa.Column("min_limit_pct", sa.Integer(), nullable=True),
        sa.Column("max_5h_pct", sa.Integer(), nullable=True),
        sa.Column("max_weekly_pct", sa.Integer(), nullable=True),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("config_key", sa.String(length=96), nullable=False),
        sa.ForeignKeyConstraint(["duration_id"], ["durations.id"], name=op.f("fk_price_matrix_duration_id_durations")),
        sa.ForeignKeyConstraint(["limit_scope_id"], ["limit_scopes.id"], name=op.f("fk_price_matrix_limit_scope_id_limit_scopes")),
        sa.ForeignKeyConstraint(["tier_id"], ["subscription_tiers.id"], name=op.f("fk_price_matrix_tier_id_subscription_tiers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_matrix")),
        sa.UniqueConstraint("config_key", name=op.f("uq_price_matrix_config_key")),
    )
    op.create_table(
        "account_check_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("job_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_account_check_jobs_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_check_jobs")),
    )
    op.create_table(
        "account_limits",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.String(), nullable=False),
        sa.Column("access_token_encrypted", sa.String(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_id_openai", sa.String(), nullable=True),
        sa.Column("chat_5h_remaining_pct", sa.Integer(), nullable=True),
        sa.Column("chat_weekly_remaining_pct", sa.Integer(), nullable=True),
        sa.Column("codex_5h_remaining_pct", sa.Integer(), nullable=True),
        sa.Column("codex_weekly_remaining_pct", sa.Integer(), nullable=True),
        sa.Column("plan_type", sa.String(), nullable=True),
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_status", sa.String(length=16), nullable=False),
        sa.Column("refresh_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_recover_attempts", sa.Integer(), nullable=False),
        sa.Column("refresh_last_recover_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_account_limits_account_id_accounts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_account_limits")),
    )
    op.create_table(
        "bump_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=False),
        sa.Column("bumped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["lot_id"], ["lots.id"], name=op.f("fk_bump_logs_lot_id_lots"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bump_logs")),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("funpay_order_id", sa.String(length=64), nullable=False),
        sa.Column("funpay_chat_id", sa.String(length=64), nullable=False),
        sa.Column("buyer_funpay_id", sa.String(length=64), nullable=False),
        sa.Column("buyer_locale", sa.String(length=8), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=True),
        sa.Column("tier_id", sa.Integer(), nullable=True),
        sa.Column("duration_id", sa.Integer(), nullable=True),
        sa.Column("limit_scope_id", sa.Integer(), nullable=True),
        sa.Column("min_limit_pct", sa.Integer(), nullable=True),
        sa.Column("max_5h_pct", sa.Integer(), nullable=True),
        sa.Column("max_weekly_pct", sa.Integer(), nullable=True),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["duration_id"], ["durations.id"], name=op.f("fk_orders_duration_id_durations")),
        sa.ForeignKeyConstraint(["limit_scope_id"], ["limit_scopes.id"], name=op.f("fk_orders_limit_scope_id_limit_scopes")),
        sa.ForeignKeyConstraint(["lot_id"], ["lots.id"], name=op.f("fk_orders_lot_id_lots")),
        sa.ForeignKeyConstraint(["tier_id"], ["subscription_tiers.id"], name=op.f("fk_orders_tier_id_subscription_tiers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("funpay_order_id", name=op.f("uq_orders_funpay_order_id")),
    )
    op.create_table(
        "rentals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("buyer_funpay_id", sa.String(length=64), nullable=False),
        sa.Column("buyer_funpay_chat_id", sa.String(length=64), nullable=False),
        sa.Column("tier_id", sa.Integer(), nullable=False),
        sa.Column("duration_id", sa.Integer(), nullable=False),
        sa.Column("limit_scope_id", sa.Integer(), nullable=False),
        sa.Column("min_limit_pct", sa.Integer(), nullable=True),
        sa.Column("max_5h_pct", sa.Integer(), nullable=True),
        sa.Column("max_weekly_pct", sa.Integer(), nullable=True),
        sa.Column("lang", sa.String(length=8), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("replaced_by_rental_id", sa.Integer(), nullable=True),
        sa.Column("replacement_count", sa.Integer(), nullable=False),
        sa.Column("last_code_request_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_chat_5h_pct", sa.Integer(), nullable=True),
        sa.Column("issued_chat_weekly_pct", sa.Integer(), nullable=True),
        sa.Column("issued_codex_5h_pct", sa.Integer(), nullable=True),
        sa.Column("issued_codex_weekly_pct", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], name=op.f("fk_rentals_account_id_accounts")),
        sa.ForeignKeyConstraint(["duration_id"], ["durations.id"], name=op.f("fk_rentals_duration_id_durations")),
        sa.ForeignKeyConstraint(["limit_scope_id"], ["limit_scopes.id"], name=op.f("fk_rentals_limit_scope_id_limit_scopes")),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_rentals_order_id_orders")),
        sa.ForeignKeyConstraint(["tier_id"], ["subscription_tiers.id"], name=op.f("fk_rentals_tier_id_subscription_tiers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rentals")),
        sa.UniqueConstraint("order_id", name="uq_rental_order"),
        sa.UniqueConstraint("order_id", name=op.f("uq_rentals_order_id")),
    )


def downgrade() -> None:
    op.drop_table("rentals")
    op.drop_table("orders")
    op.drop_table("bump_logs")
    op.drop_table("account_limits")
    op.drop_table("account_check_jobs")
    op.drop_table("price_matrix")
    op.drop_table("lots")
    op.drop_table("lot_templates")
    op.drop_table("accounts")
    op.drop_table("subscription_tiers")
    op.drop_table("seller_settings")
    op.drop_table("message_templates")
    op.drop_table("limit_scopes")
    op.drop_table("durations")
    op.drop_table("audit_logs")
