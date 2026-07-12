import asyncio

from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.migrations import (
    _CHAT_TABLES,
    _LEGACY_BASELINE_TABLES,
    _alembic_config,
    upgrade_database,
)
from app.services.crypto import decrypt, encrypt


async def _schema(engine):
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: (
                set(inspect(sync_connection).get_table_names()),
                {
                    column["name"]
                    for column in inspect(sync_connection).get_columns(
                        "seller_settings"
                    )
                },
            )
        )


async def test_upgrade_database_creates_head_schema_idempotently(
    tmp_path, monkeypatch
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = create_async_engine(database_url)

    await upgrade_database(engine)
    await upgrade_database(engine)

    tables, seller_columns = await _schema(engine)
    assert (_LEGACY_BASELINE_TABLES | _CHAT_TABLES) <= tables
    assert "admin_session_version" in seller_columns
    async with engine.connect() as connection:
        version = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        catalog = set(
            (
                await connection.execute(
                    text(
                        "SELECT code FROM subscription_tiers "
                        "WHERE system_managed = 1 AND is_sellable = 1"
                    )
                )
            ).scalars()
        )
        account_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]: column
                for column in inspect(sync_connection).get_columns("accounts")
            }
        )
    assert version == "20260713_0005"
    assert catalog == {
        "free", "go", "plus", "pro_5x", "pro_20x", "business",
        "enterprise", "edu", "teachers", "healthcare", "clinicians", "gov",
    }
    assert account_columns["tier_id"]["nullable"] is True
    assert {
        "plan_raw_type", "plan_source", "plan_confidence", "plan_detected_at"
    } <= set(account_columns)
    await engine.dispose()


async def test_upgrade_adopts_pre_chat_schema_and_normalizes_secrets(
    tmp_path, monkeypatch
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    await asyncio.to_thread(
        command.upgrade, _alembic_config(database_url), "20260713_0001"
    )
    engine = create_async_engine(database_url)
    double_password = encrypt(encrypt("account-password"))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO subscription_tiers "
                "(id, name, description, is_active) VALUES (1, 'Plus', NULL, 1)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, login, password_encrypted, totp_secret_encrypted, email, "
                "email_password_encrypted, tier_id, subscription_expires_at, "
                "max_active_rentals, status, chatgpt_last_check_at, notes) "
                "VALUES (1, 'legacy', :password, :totp, NULL, NULL, 1, NULL, "
                "NULL, 'pending_validation', NULL, NULL)"
            ),
            {"password": double_password, "totp": encrypt("TOTP")},
        )
        await connection.execute(
            text(
                "INSERT INTO seller_settings "
                "(id, funpay_session_key, funpay_session_valid, funpay_node_id, "
                "telegram_bot_token, telegram_seller_chat_id, check_interval_minutes, "
                "limits_check_interval_minutes, refresh_recover_concurrency, "
                "refresh_max_attempts, refresh_retry_delay_minutes, check_delay_seconds, "
                "bump_interval_hours, auto_bump_enabled, default_max_active_rentals, "
                "funpay_commission_percent, limits_warn_threshold_pct, admin_password_hash) "
                "VALUES (1, :golden, 0, NULL, :telegram, '123', 10, 5, 3, 3, 5, "
                "45, 4, 1, 1, 15, 20, 'hash')"
            ),
            {
                "golden": "legacy-funpay-key",
                "telegram": "123456789:legacy-token",
            },
        )
        await connection.execute(text("DROP TABLE alembic_version"))

    await upgrade_database(engine)

    tables, seller_columns = await _schema(engine)
    assert {"chat_conversations", "chat_messages"} <= tables
    assert "admin_session_version" in seller_columns
    async with engine.connect() as connection:
        password, golden, telegram, tier_id, version = (
            await connection.execute(
                text(
                    "SELECT a.password_encrypted, s.funpay_session_key, "
                    "s.telegram_bot_token, a.tier_id, v.version_num "
                    "FROM accounts a "
                    "CROSS JOIN seller_settings s "
                    "CROSS JOIN alembic_version v WHERE a.id=1 AND s.id=1"
                )
            )
        ).one()
    assert decrypt(password) == "account-password"
    assert decrypt(golden) == "legacy-funpay-key"
    assert decrypt(telegram) == "123456789:legacy-token"
    assert tier_id is None
    assert version == "20260713_0005"
    await engine.dispose()
