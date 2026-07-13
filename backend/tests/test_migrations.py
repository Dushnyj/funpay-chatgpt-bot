import asyncio

from alembic import command
import pytest
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


async def test_0016_backfills_only_exact_sale_conversations(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'verified-sales-0016.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260713_0015")

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO orders "
            "(id, funpay_order_id, funpay_chat_id, buyer_funpay_id, "
            "buyer_locale, price, status, fulfillment_attempts, created_at) "
            "VALUES (1, 'SALE-1', '100', '200', 'ru', 100, 'pending', 0, "
            "CURRENT_TIMESTAMP)"
        ))
        await connection.execute(text(
            "INSERT INTO chat_conversations "
            "(id, funpay_chat_id, buyer_funpay_id, funpay_order_id, order_id, "
            "unread_count, created_at, updated_at) VALUES "
            "(1, '100', '200', 'SALE-1', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "(2, '101', '200', 'SALE-1', NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "(3, '102', '999', NULL, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260713_0016")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        sale = (
            await connection.execute(text(
                "SELECT funpay_order_id, order_id, funpay_chat_id, "
                "buyer_funpay_id, status FROM funpay_sales"
            ))
        ).one()
        flags = dict((await connection.execute(text(
            "SELECT id, verified_sale FROM chat_conversations ORDER BY id"
        ))).all())
    await engine.dispose()

    assert tuple(sale) == ("SALE-1", 1, "100", "200", "paid")
    assert {key: bool(value) for key, value in flags.items()} == {
        1: True,
        2: False,
        3: False,
    }


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
        sale_sync_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns(
                    "funpay_sale_sync_state"
                )
            }
        )
        funpay_sale_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("funpay_sales")
            }
        )
        chat_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns(
                    "chat_conversations"
                )
            }
        )
    assert version == "20260714_0017"
    assert "funpay_sales" in tables
    assert "funpay_sale_sync_state" in tables
    assert {
        "backfill_cursor",
        "backfill_complete",
        "head_synced_at",
        "page_backoff_attempts",
        "page_backoff_until",
        "updated_at",
    } <= sale_sync_columns
    assert {"detail_attempts", "detail_next_attempt_at"} <= funpay_sale_columns
    assert {"profile_attempts", "profile_next_attempt_at"} <= chat_columns
    assert catalog == {
        "free", "go", "plus", "pro_5x", "pro_20x", "business",
        "enterprise", "edu", "teachers", "healthcare", "clinicians", "gov",
    }
    assert account_columns["tier_id"]["nullable"] is True
    assert {
        "operator_status_override",
        "validation_rerun_requested",
    } <= set(account_columns)
    assert {
        "plan_raw_type", "plan_source", "plan_confidence", "plan_detected_at"
    } <= set(account_columns)
    async with engine.connect() as connection:
        limits_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("account_limits")
            }
        )
        scope_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("limit_scopes")
            }
        )
    assert {
        "codex_primary_remaining_pct",
        "codex_primary_window_seconds",
        "codex_primary_resets_at",
        "codex_secondary_remaining_pct",
        "codex_secondary_window_seconds",
        "codex_secondary_resets_at",
        "plan_window_status",
        "expected_long_window_seconds",
        "low_limit_warning_fingerprint",
        "low_limit_warned_at",
    } <= limits_columns
    assert {"is_enabled", "sort_order"} <= scope_columns
    assert {
        "admin_login_failure_count",
        "admin_login_window_started_at",
        "admin_login_blocked_until",
    } <= seller_columns
    assert "email_oauth_credentials" in tables
    async with engine.connect() as connection:
        oauth_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns(
                    "email_oauth_credentials"
                )
            }
        )
    assert {
        "account_id",
        "provider",
        "email",
        "external_subject",
        "refresh_token_encrypted",
        "scopes",
        "status",
        "connected_at",
        "updated_at",
    } <= oauth_columns
    async with engine.connect() as connection:
        rental_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("rentals")
            }
        )
    assert {
        "credentials_delivery_status",
        "credentials_delivery_template",
        "credentials_delivery_started_at",
        "credentials_delivered_at",
        "credentials_delivery_attempts",
        "credentials_delivery_last_error",
        "credentials_delivery_next_attempt_at",
        "issued_codex_primary_pct",
        "issued_codex_primary_window_seconds",
        "issued_codex_primary_resets_at",
        "issued_codex_secondary_pct",
        "issued_codex_secondary_window_seconds",
        "issued_codex_secondary_resets_at",
        "issued_plan_window_status",
        "issued_expected_long_window_seconds",
        "issued_limits_measured_at",
    } <= rental_columns
    async with engine.connect() as connection:
        order_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("orders")
            }
        )
    assert {
        "fulfillment_attempts",
        "fulfillment_next_attempt_at",
        "fulfillment_last_error",
    } <= order_columns
    await engine.dispose()


async def test_0013_limit_scope_availability_fails_closed_for_unknown_codes(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'catalog-0013.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260713_0012")

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO limit_scopes (code, name) VALUES "
                "('any', 'Any'), ('chat', 'Chat'), ('codex', 'Codex'), "
                "('legacy', 'Legacy')"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260713_0013")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT code, is_enabled FROM limit_scopes")
            )
        ).all()
    await engine.dispose()

    availability = {row.code: bool(row.is_enabled) for row in rows}
    assert availability == {
        "any": True,
        "chat": False,
        "codex": True,
        "legacy": False,
    }


async def test_0014_normalizes_catalog_sort_mirrors(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'catalog-0014.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260713_0013")

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO durations (days, is_enabled, sort_order) VALUES "
                "(8, true, 999), (3, true, 0)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO limit_scopes "
                "(code, name, is_enabled, sort_order) VALUES "
                "('any', 'Any', true, 99), "
                "('chat', 'Chat', false, 1), "
                "('codex', 'Codex', true, 2), "
                "('legacy', 'Legacy', false, -1)"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260713_0014")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        duration_rows = (
            await connection.execute(
                text("SELECT days, sort_order FROM durations")
            )
        ).all()
        scope_rows = (
            await connection.execute(
                text("SELECT code, sort_order FROM limit_scopes")
            )
        ).all()
    await engine.dispose()

    assert {row.days: row.sort_order for row in duration_rows} == {8: 8, 3: 3}
    assert {row.code: row.sort_order for row in scope_rows} == {
        "any": 10,
        "chat": 20,
        "codex": 30,
        "legacy": 100,
    }


async def test_0015_preserves_duration_ids_and_clamps_rental_capacity(
    tmp_path, monkeypatch,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'duration-0015.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260713_0014")
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        tier_id = (
            await connection.execute(
                text("SELECT id FROM subscription_tiers WHERE code = 'plus'")
            )
        ).scalar_one()
        await connection.execute(text(
            "INSERT INTO durations (id, days, is_enabled, sort_order) "
            "VALUES (42, 7, 1, 7)"
        ))
        await connection.execute(text(
            "INSERT INTO limit_scopes (id, code, name, is_enabled, sort_order) "
            "VALUES (42, 'any', 'Any', 1, 10), "
            "(43, 'chat', 'Chat', 0, 20)"
        ))
        await connection.execute(text(
            "INSERT INTO price_matrix "
            "(id, tier_id, duration_id, limit_scope_id, price, config_key) "
            "VALUES (42, :tier_id, 42, 42, 100, 'duration-fk')"
        ), {"tier_id": tier_id})
        await connection.execute(text(
            "INSERT INTO accounts "
            "(id, login, password_encrypted, totp_secret_encrypted, tier_id, "
            "max_active_rentals, status) VALUES "
            "(42, 'capacity@example.test', 'password', 'totp', :tier_id, 7, "
            "'maintenance')"
        ), {"tier_id": tier_id})
        await connection.execute(text(
            "INSERT INTO seller_settings "
            "(id, funpay_session_valid, check_interval_minutes, "
            "limits_check_interval_minutes, refresh_recover_concurrency, "
            "refresh_max_attempts, refresh_retry_delay_minutes, "
            "check_delay_seconds, bump_interval_hours, auto_bump_enabled, "
            "default_max_active_rentals, funpay_commission_percent, "
            "limits_warn_threshold_pct) VALUES "
            "(1, 0, 1440, 5, 3, 3, 5, 45, 4, 1, 9, 15, 20)"
        ))
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260713_0015")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        duration = (
            await connection.execute(
                text("SELECT id, minutes, sort_order FROM durations WHERE id=42")
            )
        ).one()
        price_duration_id = (
            await connection.execute(
                text("SELECT duration_id FROM price_matrix WHERE id=42")
            )
        ).scalar_one()
        capacities = (
            await connection.execute(text(
                "SELECT a.max_active_rentals, s.default_max_active_rentals "
                "FROM accounts a CROSS JOIN seller_settings s "
                "WHERE a.id=42 AND s.id=1"
            ))
        ).one()
        scope_codes = set((await connection.execute(
            text("SELECT code FROM limit_scopes")
        )).scalars())
        columns = await connection.run_sync(lambda sync_connection: {
            table: {
                column["name"]
                for column in inspect(sync_connection).get_columns(table)
            }
            for table in ("durations", "account_limits", "rentals")
        })
        duration_unique_names = await connection.run_sync(
            lambda sync_connection: {
                constraint["name"]
                for constraint in inspect(sync_connection)
                .get_unique_constraints("durations")
            }
        )
        rental_indexes = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_indexes(
                "rentals"
            )
        )
        rental_unique_names = await connection.run_sync(
            lambda sync_connection: {
                constraint["name"]
                for constraint in inspect(sync_connection)
                .get_unique_constraints("rentals")
            }
        )
        rental_foreign_keys = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_foreign_keys(
                "rentals"
            )
        )
    await engine.dispose()

    assert tuple(duration) == (42, 7 * 24 * 60, 7 * 24 * 60)
    assert price_duration_id == 42
    assert tuple(capacities) == (1, 1)
    assert "chat" not in scope_codes
    assert "days" not in columns["durations"]
    assert "uq_durations_minutes" in duration_unique_names
    assert any(
        index["name"] == "uq_rentals_one_occupying_account"
        and index["unique"]
        for index in rental_indexes
    )
    assert "chat_5h_remaining_pct" not in columns["account_limits"]
    assert "issued_chat_5h_pct" not in columns["rentals"]
    assert "replacement_target_account_id" in columns["rentals"]
    assert (
        "uq_rentals_replacement_target_account_id"
        in rental_unique_names
    )
    assert any(
        foreign_key["constrained_columns"]
        == ["replacement_target_account_id"]
        and foreign_key["referred_table"] == "accounts"
        for foreign_key in rental_foreign_keys
    )


async def test_0015_keeps_disabled_chat_tombstone_for_historical_refs(
    tmp_path, monkeypatch,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'chat-history-0015.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260713_0014")
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        tier_id = (
            await connection.execute(
                text("SELECT id FROM subscription_tiers WHERE code = 'plus'")
            )
        ).scalar_one()
        await connection.execute(text(
            "INSERT INTO durations (id, days, is_enabled, sort_order) "
            "VALUES (51, 1, 1, 1)"
        ))
        await connection.execute(text(
            "INSERT INTO limit_scopes (id, code, name, is_enabled, sort_order) "
            "VALUES (51, 'chat', 'Chat', 1, 20)"
        ))
        await connection.execute(text(
            "INSERT INTO price_matrix "
            "(id, tier_id, duration_id, limit_scope_id, price, config_key) "
            "VALUES (51, :tier_id, 51, 51, 100, 'chat-history')"
        ), {"tier_id": tier_id})
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260713_0015")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        scope = (
            await connection.execute(text(
                "SELECT is_enabled, sort_order FROM limit_scopes "
                "WHERE code='chat'"
            ))
        ).one()
        price_scope = (
            await connection.execute(
                text("SELECT limit_scope_id FROM price_matrix WHERE id=51")
            )
        ).scalar_one()
    await engine.dispose()
    assert tuple(scope) == (0, 100)
    assert price_scope == 51


async def test_0015_aborts_when_chat_has_live_order(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'chat-live-0015.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260713_0014")
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        tier_id = (
            await connection.execute(
                text("SELECT id FROM subscription_tiers WHERE code = 'plus'")
            )
        ).scalar_one()
        await connection.execute(text(
            "INSERT INTO durations (id, days, is_enabled, sort_order) "
            "VALUES (61, 1, 1, 1)"
        ))
        await connection.execute(text(
            "INSERT INTO limit_scopes (id, code, name, is_enabled, sort_order) "
            "VALUES (61, 'chat', 'Chat', 1, 20)"
        ))
        await connection.execute(text(
            "INSERT INTO orders "
            "(id, funpay_order_id, funpay_chat_id, buyer_funpay_id, "
            "buyer_locale, tier_id, duration_id, limit_scope_id, price, "
            "status, created_at) VALUES "
            "(61, 'live-chat-order', '100', '200', 'ru', :tier_id, 61, 61, "
            "100, 'pending', CURRENT_TIMESTAMP)"
        ), {"tier_id": tier_id})
    await engine.dispose()

    with pytest.raises(RuntimeError, match="live buyer state.*orders=1"):
        await asyncio.to_thread(command.upgrade, config, "20260713_0015")


async def test_0015_aborts_when_account_has_two_occupying_rentals(
    tmp_path, monkeypatch,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'shared-live-0015.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260713_0014")
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        tier_id = (
            await connection.execute(
                text("SELECT id FROM subscription_tiers WHERE code = 'plus'")
            )
        ).scalar_one()
        await connection.execute(text(
            "INSERT INTO durations (id, days, is_enabled, sort_order) "
            "VALUES (71, 1, 1, 1)"
        ))
        await connection.execute(text(
            "INSERT INTO limit_scopes (id, code, name, is_enabled, sort_order) "
            "VALUES (71, 'any', 'Any', 1, 10)"
        ))
        await connection.execute(text(
            "INSERT INTO accounts "
            "(id, login, password_encrypted, totp_secret_encrypted, tier_id, "
            "status) VALUES (71, 'shared@example.test', 'password', 'totp', "
            ":tier_id, 'active')"
        ), {"tier_id": tier_id})
        for index in (1, 2):
            await connection.execute(text(
                "INSERT INTO orders "
                "(id, funpay_order_id, funpay_chat_id, buyer_funpay_id, "
                "buyer_locale, tier_id, duration_id, limit_scope_id, price, "
                "status, created_at) VALUES "
                "(:id, :remote, :chat, :buyer, 'ru', :tier_id, 71, 71, 100, "
                "'completed', CURRENT_TIMESTAMP)"
            ), {
                "id": 70 + index,
                "remote": f"shared-{index}",
                "chat": str(index),
                "buyer": str(index),
                "tier_id": tier_id,
            })
            await connection.execute(text(
                "INSERT INTO rentals "
                "(id, order_id, account_id, buyer_funpay_id, "
                "buyer_funpay_chat_id, tier_id, duration_id, limit_scope_id, "
                "lang, started_at, expires_at, status, replacement_count, "
                "credentials_delivery_status, credentials_delivery_template, "
                "credentials_delivery_attempts) VALUES "
                "(:id, :id, 71, :buyer, :chat, :tier_id, 71, 71, 'ru', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :status, 0, 'sent', "
                "'welcome', 1)"
            ), {
                "id": 70 + index,
                "buyer": str(index),
                "chat": str(index),
                "tier_id": tier_id,
                "status": "active" if index == 1 else "expiry_pending",
            })
    await engine.dispose()

    with pytest.raises(RuntimeError, match="multiple live rentals"):
        await asyncio.to_thread(command.upgrade, config, "20260713_0015")


async def test_0015_downgrade_rejects_sub_day_duration(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'subday-0015.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260713_0015")
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO durations (minutes, is_enabled, sort_order) "
            "VALUES (30, 1, 30)"
        ))
    await engine.dispose()

    with pytest.raises(RuntimeError, match="sub-day values"):
        await asyncio.to_thread(command.downgrade, config, "20260713_0014")


async def test_lot_template_upgrade_preserves_legacy_draft_disabled(
    tmp_path, monkeypatch,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'lot-template-upgrade.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260713_0011")

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO lot_templates "
                "(tier_id, limit_scope_id, title_template_ru, title_template_en, "
                "description_template_ru, description_template_en) VALUES "
                "(NULL, NULL, 'Старый {plan}', 'Legacy {plan}', '', '')"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260713_0012")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT key, name, is_enabled, system_managed "
                    "FROM lot_templates"
                )
            )
        ).one()
        columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("lot_templates")
            }
        )
        unique = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_unique_constraints(
                "lot_templates"
            )
        )
        target_index_sql = (
            await connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' "
                    "AND name = 'uq_lot_templates_enabled_custom_target'"
                )
            )
        ).scalar_one_or_none()
    assert tuple(row) == ("legacy-1", "Legacy template 1", 0, 0)
    assert {"key", "name", "is_enabled", "system_managed"} <= columns
    assert any(item["column_names"] == ["key"] for item in unique)
    assert target_index_sql is not None
    normalized_index_sql = " ".join(target_index_sql.lower().split())
    assert "coalesce(tier_id, 0)" in normalized_index_sql
    assert "coalesce(limit_scope_id, 0)" in normalized_index_sql
    assert "where system_managed = 0 and is_enabled = 1" in normalized_index_sql
    await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "20260713_0011")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        downgraded = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("lot_templates")
            }
        )
        downgraded_target_index_sql = (
            await connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' "
                    "AND name = 'uq_lot_templates_enabled_custom_target'"
                )
            )
        ).scalar_one_or_none()
    assert not {"key", "name", "is_enabled", "system_managed"} & downgraded
    assert downgraded_target_index_sql is None
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
                    "s.telegram_bot_token, s.check_interval_minutes, "
                    "a.tier_id, a.status, j.job_type, "
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
            check_interval_minutes,
            tier_id,
            account_status,
            job_type,
            job_status,
            version,
        ) = row
    assert decrypt(password) == "account-password"
    assert decrypt(golden) == "legacy-funpay-key"
    assert decrypt(telegram) == "123456789:legacy-token"
    assert check_interval_minutes == 1440
    assert tier_id is None
    assert account_status == "pending_validation"
    assert job_type == "full_validation"
    assert job_status == "pending"
    assert version == "20260714_0017"
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
            row.login: (row.tier_id, row.status, row.operator_status_override)
            for row in (
                await connection.execute(
                    text(
                        "SELECT login, tier_id, status, operator_status_override "
                        "FROM accounts ORDER BY id"
                    )
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
        "legacy-active": (None, "pending_validation", None),
        "verified-active": (plus_id, "active", None),
        "legacy-disabled": (None, "disabled", "disabled"),
        "legacy-pending": (None, "pending_validation", None),
    }
    assert jobs == {1: 1, 4: 1}
    assert version == "20260714_0017"
    await engine.dispose()


async def test_0009_round_trip_preserves_failed_delivery_state(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'delivery-round-trip.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    await asyncio.to_thread(
        command.upgrade,
        _alembic_config(database_url),
        "20260713_0009",
    )
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        tier_id = (
            await connection.execute(
                text("SELECT id FROM subscription_tiers WHERE code = 'plus'")
            )
        ).scalar_one()
        await connection.execute(text(
            "INSERT INTO durations (id, days, is_enabled, sort_order) "
            "VALUES (901, 7, 1, 10)"
        ))
        await connection.execute(text(
            "INSERT INTO limit_scopes (id, code, name) "
            "VALUES (901, 'rollback-test', 'Rollback test')"
        ))
        await connection.execute(text(
            "INSERT INTO accounts "
            "(id, login, password_encrypted, totp_secret_encrypted, tier_id, status) "
            "VALUES (901, 'rollback@example.com', 'password', 'totp', "
            ":tier_id, 'maintenance')"
        ), {"tier_id": tier_id})
        await connection.execute(text(
            "INSERT INTO orders "
            "(id, funpay_order_id, funpay_chat_id, buyer_funpay_id, buyer_locale, "
            "tier_id, duration_id, limit_scope_id, price, status, created_at) "
            "VALUES (901, 'rollback-order', '100', '200', 'ru', :tier_id, "
            "901, 901, 100, 'completed', CURRENT_TIMESTAMP)"
        ), {"tier_id": tier_id})
        await connection.execute(text(
            "INSERT INTO rentals "
            "(id, order_id, account_id, buyer_funpay_id, buyer_funpay_chat_id, "
            "tier_id, duration_id, limit_scope_id, lang, started_at, expires_at, "
            "status, replacement_count, credentials_delivery_status, "
            "credentials_delivery_template, credentials_delivery_attempts, "
            "credentials_delivery_last_error) VALUES "
            "(901, 901, 901, '200', '100', :tier_id, 901, 901, 'ru', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'active', 0, 'failed', "
            "'welcome', 5, 'network timeout')"
        ), {"tier_id": tier_id})
    await engine.dispose()

    await asyncio.to_thread(
        command.downgrade,
        _alembic_config(database_url),
        "20260713_0008",
    )
    await asyncio.to_thread(
        command.upgrade,
        _alembic_config(database_url),
        "20260713_0009",
    )
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        restored = (
            await connection.execute(text(
                "SELECT credentials_delivery_status, "
                "credentials_delivery_template, credentials_delivery_attempts, "
                "credentials_delivery_last_error FROM rentals WHERE id = 901"
            ))
        ).one()
        tables = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )

    assert tuple(restored) == ("failed", "welcome", 5, "network timeout")
    assert "rental_delivery_state_rollback_backup" not in tables
    await engine.dispose()


async def test_0011_migrates_runtime_state_and_round_trips_chat_encryption(
    tmp_path, monkeypatch
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'pre-0011.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    await asyncio.to_thread(
        command.upgrade, _alembic_config(database_url), "20260713_0010"
    )

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        free_tier_id = (
            await connection.execute(
                text("SELECT id FROM subscription_tiers WHERE code = 'free'")
            )
        ).scalar_one()
        plus_tier_id = (
            await connection.execute(
                text("SELECT id FROM subscription_tiers WHERE code = 'plus'")
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO durations (id, days, is_enabled, sort_order) "
                "VALUES (1, 7, 1, 10)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO limit_scopes (id, code, name) VALUES "
                "(1, 'any', 'Any'), (2, 'codex', 'Codex')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO price_matrix "
                "(id, tier_id, duration_id, limit_scope_id, min_limit_pct, "
                "max_5h_pct, max_weekly_pct, price, config_key) VALUES "
                "(1, :free_tier_id, 1, 1, NULL, 45, 60, 100, 'free-any'), "
                "(2, :free_tier_id, 1, 2, NULL, 45, 60, 100, 'free-codex'), "
                "(3, :plus_tier_id, 1, 1, NULL, 45, 60, 100, 'plus-any')"
            ),
            {
                "free_tier_id": free_tier_id,
                "plus_tier_id": plus_tier_id,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, login, password_encrypted, totp_secret_encrypted, tier_id, "
                "status) VALUES "
                "(1, 'disabled-account', 'password', 'totp', :tier_id, 'disabled'), "
                "(2, 'maintenance-account', 'password', 'totp', :tier_id, "
                "'maintenance'), "
                "(3, 'active-account', 'password', 'totp', :tier_id, 'active')"
            ),
            {"tier_id": free_tier_id},
        )
        await connection.execute(
            text(
                "INSERT INTO account_limits "
                "(account_id, refresh_token_encrypted, measured_at, refresh_status, "
                "refresh_recover_attempts) VALUES "
                "(3, 'refresh-token', CURRENT_TIMESTAMP, 'ok', 0)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO seller_settings "
                "(id, funpay_session_key, funpay_session_valid, funpay_node_id, "
                "telegram_bot_token, telegram_seller_chat_id, "
                "check_interval_minutes, limits_check_interval_minutes, "
                "refresh_recover_concurrency, refresh_max_attempts, "
                "refresh_retry_delay_minutes, check_delay_seconds, "
                "bump_interval_hours, auto_bump_enabled, "
                "default_max_active_rentals, funpay_commission_percent, "
                "limits_warn_threshold_pct, admin_password_hash) VALUES "
                "(1, NULL, 0, NULL, NULL, NULL, 1440, 120, 3, 3, 5, 45, "
                "4, 1, 1, 15, 20, 'hash')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO chat_conversations "
                "(id, funpay_chat_id, unread_count, last_message_text, "
                "last_message_direction, created_at, updated_at) VALUES "
                "(1, 'chat-1', 1, 'conversation plaintext', 'incoming', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO chat_messages "
                "(id, conversation_id, direction, text, delivery_status, "
                "is_read, created_at) VALUES "
                "(1, 1, 'incoming', 'message plaintext', 'received', 0, "
                "CURRENT_TIMESTAMP)"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(
        command.upgrade, _alembic_config(database_url), "20260713_0011"
    )
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        account_states = {
            row.login: (row.operator_status_override, row.validation_rerun_requested)
            for row in (
                await connection.execute(
                    text(
                        "SELECT login, operator_status_override, "
                        "validation_rerun_requested FROM accounts ORDER BY id"
                    )
                )
            )
        }
        limits_state = (
            await connection.execute(
                text(
                    "SELECT measured_at, plan_window_status, "
                    "expected_long_window_seconds, low_limit_warning_fingerprint, "
                    "low_limit_warned_at FROM account_limits WHERE account_id = 3"
                )
            )
        ).one()
        settings_state = (
            await connection.execute(
                text(
                    "SELECT limits_check_interval_minutes, "
                    "admin_login_failure_count, admin_login_window_started_at, "
                    "admin_login_blocked_until FROM seller_settings WHERE id = 1"
                )
            )
        ).one()
        matrix_ceilings = {
            row.config_key: row.max_5h_pct
            for row in (
                await connection.execute(
                    text(
                        "SELECT config_key, max_5h_pct FROM price_matrix "
                        "ORDER BY id"
                    )
                )
            )
        }
        encrypted_conversation = (
            await connection.execute(
                text(
                    "SELECT last_message_text FROM chat_conversations WHERE id = 1"
                )
            )
        ).scalar_one()
        encrypted_message = (
            await connection.execute(
                text("SELECT text FROM chat_messages WHERE id = 1")
            )
        ).scalar_one()
        chat_types = await connection.run_sync(
            lambda sync_connection: {
                "conversation": str(
                    next(
                        column["type"]
                        for column in inspect(sync_connection).get_columns(
                            "chat_conversations"
                        )
                        if column["name"] == "last_message_text"
                    )
                ).upper(),
                "message": str(
                    next(
                        column["type"]
                        for column in inspect(sync_connection).get_columns(
                            "chat_messages"
                        )
                        if column["name"] == "text"
                    )
                ).upper(),
            }
        )

    assert account_states == {
        "disabled-account": ("disabled", 0),
        "maintenance-account": ("maintenance", 0),
        "active-account": (None, 0),
    }
    assert tuple(limits_state) == (None, "unknown", None, None, None)
    assert tuple(settings_state) == (55, 0, None, None)
    assert matrix_ceilings == {
        "free-any": None,
        "free-codex": 45,
        "plus-any": 45,
    }
    assert encrypted_conversation != "conversation plaintext"
    assert encrypted_message != "message plaintext"
    assert decrypt(encrypted_conversation) == "conversation plaintext"
    assert decrypt(encrypted_message) == "message plaintext"
    assert chat_types == {"conversation": "TEXT", "message": "TEXT"}
    await engine.dispose()

    await asyncio.to_thread(
        command.downgrade, _alembic_config(database_url), "20260713_0010"
    )
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        plaintext_conversation = (
            await connection.execute(
                text(
                    "SELECT last_message_text FROM chat_conversations WHERE id = 1"
                )
            )
        ).scalar_one()
        plaintext_message = (
            await connection.execute(
                text("SELECT text FROM chat_messages WHERE id = 1")
            )
        ).scalar_one()
        downgraded_schema = await connection.run_sync(
            lambda sync_connection: {
                "account": {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("accounts")
                },
                "limits": {
                    column["name"]
                    for column in inspect(sync_connection).get_columns(
                        "account_limits"
                    )
                },
                "rental": {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("rentals")
                },
                "conversation_type": str(
                    next(
                        column["type"]
                        for column in inspect(sync_connection).get_columns(
                            "chat_conversations"
                        )
                        if column["name"] == "last_message_text"
                    )
                ).upper(),
                "message_type": str(
                    next(
                        column["type"]
                        for column in inspect(sync_connection).get_columns(
                            "chat_messages"
                        )
                        if column["name"] == "text"
                    )
                ).upper(),
            }
        )

    assert plaintext_conversation != "conversation plaintext"
    assert plaintext_message != "message plaintext"
    assert decrypt(plaintext_conversation) == "conversation plaintext"
    assert decrypt(plaintext_message) == "message plaintext"
    assert "operator_status_override" not in downgraded_schema["account"]
    assert "plan_window_status" not in downgraded_schema["limits"]
    assert "issued_plan_window_status" not in downgraded_schema["rental"]
    assert downgraded_schema["conversation_type"] == "VARCHAR(4000)"
    assert downgraded_schema["message_type"] == "VARCHAR(4000)"
    await engine.dispose()

    await asyncio.to_thread(
        command.upgrade, _alembic_config(database_url), "20260713_0011"
    )
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        reencrypted_conversation = (
            await connection.execute(
                text(
                    "SELECT last_message_text FROM chat_conversations WHERE id = 1"
                )
            )
        ).scalar_one()
        reencrypted_message = (
            await connection.execute(
                text("SELECT text FROM chat_messages WHERE id = 1")
            )
        ).scalar_one()
    assert reencrypted_conversation != "conversation plaintext"
    assert reencrypted_message != "message plaintext"
    assert decrypt(reencrypted_conversation) == "conversation plaintext"
    assert decrypt(reencrypted_message) == "message plaintext"
    await engine.dispose()
