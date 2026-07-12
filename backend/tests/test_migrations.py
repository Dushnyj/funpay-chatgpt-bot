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
    assert version == "20260713_0006"
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
        row = (
            await connection.execute(
                text(
                    "SELECT a.password_encrypted, s.funpay_session_key, "
                    "s.telegram_bot_token, a.tier_id, a.status, j.job_type, "
                    "j.status, v.version_num "
                    "FROM accounts a "
                    "JOIN account_check_jobs j ON j.account_id=a.id "
                    "CROSS JOIN seller_settings s "
                    "CROSS JOIN alembic_version v WHERE a.id=1 AND s.id=1"
                )
            )
        ).one()
        (
            password,
            golden,
            telegram,
            tier_id,
            account_status,
            job_type,
            job_status,
            version,
        ) = row
    assert decrypt(password) == "account-password"
    assert decrypt(golden) == "legacy-funpay-key"
    assert decrypt(telegram) == "123456789:legacy-token"
    assert tier_id is None
    assert account_status == "pending_validation"
    assert job_type == "full_validation"
    assert job_status == "pending"
    assert version == "20260713_0006"
    await engine.dispose()


async def test_upgrade_from_existing_0005_revalidates_only_untrusted_accounts(
    tmp_path, monkeypatch
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'old-0005.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    await asyncio.to_thread(
        command.upgrade, _alembic_config(database_url), "20260713_0005"
    )
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        plus_id = (
            await connection.execute(
                text("SELECT id FROM subscription_tiers WHERE code = 'plus'")
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, login, password_encrypted, totp_secret_encrypted, tier_id, "
                "status, plan_raw_type, plan_source, plan_confidence, plan_detected_at) "
                "VALUES "
                "(1, 'legacy-active', :password, :totp, :tier_id, 'active', "
                "NULL, NULL, NULL, NULL), "
                "(2, 'verified-active', :password, :totp, :tier_id, 'active', "
                "'plus', 'accounts_check', 0.98, CURRENT_TIMESTAMP), "
                "(3, 'legacy-disabled', :password, :totp, :tier_id, 'disabled', "
                "NULL, NULL, NULL, NULL), "
                "(4, 'legacy-pending', :password, :totp, :tier_id, "
                "'pending_validation', NULL, NULL, NULL, NULL)"
            ),
            {
                "password": encrypt("password"),
                "totp": encrypt("TOTP"),
                "tier_id": plus_id,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO account_check_jobs "
                "(account_id, priority, job_type, status, created_at) "
                "VALUES (4, 'new', 'full_validation', 'pending', CURRENT_TIMESTAMP)"
            )
        )
    await engine.dispose()

    engine = create_async_engine(database_url)
    await upgrade_database(engine)

    async with engine.connect() as connection:
        accounts = {
            row.login: (row.tier_id, row.status)
            for row in (
                await connection.execute(
                    text("SELECT login, tier_id, status FROM accounts ORDER BY id")
                )
            )
        }
        jobs = {
            row.account_id: row.count
            for row in (
                await connection.execute(
                    text(
                        "SELECT account_id, COUNT(*) AS count "
                        "FROM account_check_jobs WHERE job_type='full_validation' "
                        "AND status IN ('pending', 'running') GROUP BY account_id"
                    )
                )
            )
        }
        version = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()

    assert accounts == {
        "legacy-active": (None, "pending_validation"),
        "verified-active": (plus_id, "active"),
        "legacy-disabled": (None, "disabled"),
        "legacy-pending": (None, "pending_validation"),
    }
    assert jobs == {1: 1, 4: 1}
    assert version == "20260713_0006"
    await engine.dispose()
