import asyncio
import io
import re

from alembic import command
from alembic.script import ScriptDirectory
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


async def test_0018_quarantines_legacy_sales_and_round_trips_contract(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'managed-sales-0018.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260714_0017")

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        tier_id = (
            await connection.execute(
                text("SELECT id FROM subscription_tiers WHERE code = 'plus'")
            )
        ).scalar_one()
        await connection.execute(text(
            "INSERT INTO durations (id, minutes, is_enabled, sort_order) "
            "VALUES (1801, 60, 1, 60)"
        ))
        await connection.execute(text(
            "INSERT INTO limit_scopes "
            "(id, code, name, is_enabled, sort_order) VALUES "
            "(1801, 'managed-sale-test', 'Managed sale test', 1, 1801)"
        ))
        await connection.execute(text(
            "INSERT INTO lots "
            "(id, funpay_id, funpay_node_id, tier_id, duration_id, "
            "limit_scope_id, price, title_ru, title_en, description_ru, "
            "description_en, status, auto_created, config_key) VALUES "
            "(1801, 'offer-1801', 1355, :tier_id, 1801, 1801, 100, "
            "'Managed', 'Managed', '', '', 'active', 1, 'managed-1801')"
        ), {"tier_id": tier_id})
        await connection.execute(text(
            "INSERT INTO orders "
            "(id, funpay_order_id, funpay_chat_id, buyer_funpay_id, "
            "buyer_locale, lot_id, tier_id, duration_id, limit_scope_id, "
            "price, status, fulfillment_attempts, created_at) VALUES "
            "(1801, 'LEGACY01', 'chat-legacy', 'buyer-legacy', 'ru', "
            "1801, :tier_id, 1801, 1801, 100, 'completed', 0, "
            "CURRENT_TIMESTAMP)"
        ), {"tier_id": tier_id})
        await connection.execute(text(
            "INSERT INTO funpay_sales "
            "(id, funpay_order_id, order_id, funpay_chat_id, "
            "buyer_funpay_id, status, created_at, detail_attempts, "
            "updated_at) VALUES "
            "(1801, 'LEGACY01', 1801, 'chat-legacy', 'buyer-legacy', "
            "'completed', CURRENT_TIMESTAMP, 0, CURRENT_TIMESTAMP), "
            "(1802, 'ORPHAN01', NULL, 'chat-orphan', 'buyer-orphan', "
            "'completed', CURRENT_TIMESTAMP, 0, CURRENT_TIMESTAMP)"
        ))
        await connection.execute(text(
            "INSERT INTO chat_conversations "
            "(id, funpay_chat_id, buyer_funpay_id, funpay_order_id, "
            "order_id, unread_count, profile_attempts, verified_sale, "
            "created_at, updated_at) VALUES "
            "(1801, 'chat-legacy', 'buyer-legacy', 'LEGACY01', 1801, "
            "1, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "(1802, 'chat-orphan', 'buyer-orphan', 'ORPHAN01', NULL, "
            "0, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "(1804, 'chat-empty-stale', 'buyer-stale', NULL, NULL, "
            "0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        await connection.execute(text(
            "INSERT INTO chat_messages "
            "(id, conversation_id, direction, text, delivery_status, "
            "is_read, created_at) VALUES "
            "(1801, 1801, 'incoming', 'retained audit message', "
            "'received', 0, CURRENT_TIMESTAMP)"
        ))
        await connection.execute(text(
            "UPDATE funpay_sale_sync_state SET "
            "backfill_cursor='legacy-cursor', backfill_complete=0 WHERE id=1"
        ))
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260714_0018")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        sales = (await connection.execute(text(
            "SELECT funpay_order_id FROM funpay_sales"
        ))).all()
        conversations = {
            row.id: bool(row.verified_sale)
            for row in (
                await connection.execute(text(
                    "SELECT id, verified_sale FROM chat_conversations ORDER BY id"
                ))
            )
        }
        message_conversations = set((await connection.execute(text(
            "SELECT conversation_id FROM chat_messages"
        ))).scalars())
        lot_state = (
            await connection.execute(text(
                "SELECT provenance_token, provenance_marker_synced "
                "FROM lots WHERE id=1801"
            ))
        ).one()
        order_state = (
            await connection.execute(text(
                "SELECT lot_binding_method, funpay_offer_id, "
                "lot_provenance_token FROM orders WHERE id=1801"
            ))
        ).one()
        sync_state = (
            await connection.execute(text(
                "SELECT backfill_cursor, backfill_complete "
                "FROM funpay_sale_sync_state WHERE id=1"
            ))
        ).one()
        schema = await connection.run_sync(
            lambda sync_connection: {
                "order_column": next(
                    column
                    for column in inspect(sync_connection).get_columns(
                        "funpay_sales"
                    )
                    if column["name"] == "order_id"
                ),
                "order_fk": next(
                    foreign_key
                    for foreign_key in inspect(sync_connection).get_foreign_keys(
                        "funpay_sales"
                    )
                    if foreign_key["constrained_columns"] == ["order_id"]
                ),
                "lot_columns": {
                    column["name"]: column
                    for column in inspect(sync_connection).get_columns("lots")
                },
                "order_columns": {
                    column["name"]: column
                    for column in inspect(sync_connection).get_columns("orders")
                },
                "lot_unique_names": {
                    constraint["name"]
                    for constraint in inspect(sync_connection)
                    .get_unique_constraints("lots")
                },
            }
        )
    await engine.dispose()

    assert sales == []
    assert conversations == {1801: False}
    assert message_conversations == {1801}
    assert re.fullmatch(r"[0-9a-f]{32}", lot_state.provenance_token)
    assert lot_state.provenance_marker_synced == 0
    assert tuple(order_state) == (None, None, None)
    assert tuple(sync_state) == (None, 1)
    assert schema["order_column"]["nullable"] is False
    assert schema["order_fk"]["referred_table"] == "orders"
    assert schema["order_fk"]["options"].get("ondelete") == "CASCADE"
    assert schema["lot_columns"]["provenance_token"]["nullable"] is False
    assert schema["lot_columns"]["provenance_token"]["type"].length == 32
    assert (
        schema["lot_columns"]["provenance_marker_synced"]["nullable"]
        is False
    )
    assert "uq_lots_provenance_token" in schema["lot_unique_names"]
    assert {
        "lot_binding_method",
        "funpay_offer_id",
        "lot_provenance_token",
    } <= set(schema["order_columns"])
    assert all(
        schema["order_columns"][name]["nullable"] is True
        for name in (
            "lot_binding_method",
            "funpay_offer_id",
            "lot_provenance_token",
        )
    )
    assert schema["order_columns"]["lot_provenance_token"]["type"].length == 32

    await asyncio.to_thread(command.downgrade, config, "20260714_0017")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        downgraded = await connection.run_sync(
            lambda sync_connection: {
                "order_column": next(
                    column
                    for column in inspect(sync_connection).get_columns(
                        "funpay_sales"
                    )
                    if column["name"] == "order_id"
                ),
                "order_fk": next(
                    foreign_key
                    for foreign_key in inspect(sync_connection).get_foreign_keys(
                        "funpay_sales"
                    )
                    if foreign_key["constrained_columns"] == ["order_id"]
                ),
                "lot_columns": {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("lots")
                },
                "order_columns": {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("orders")
                },
            }
        )
        downgraded_state = (
            await connection.execute(text(
                "SELECT backfill_cursor, backfill_complete "
                "FROM funpay_sale_sync_state WHERE id=1"
            ))
        ).one()
        version = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    await engine.dispose()

    assert downgraded["order_column"]["nullable"] is True
    assert downgraded["order_fk"]["options"].get("ondelete") == "SET NULL"
    assert "provenance_token" not in downgraded["lot_columns"]
    assert "provenance_marker_synced" not in downgraded["lot_columns"]
    assert {
        "lot_binding_method",
        "funpay_offer_id",
        "lot_provenance_token",
    }.isdisjoint(downgraded["order_columns"])
    assert tuple(downgraded_state) == (None, 0)
    assert version == "20260714_0017"


async def test_0018_exact_binding_cleanup_blocks_quarantine_resurrection(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'exact-sales-0018.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260714_0018")
    migration = ScriptDirectory.from_config(config).get_revision(
        "20260714_0018"
    ).module

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        tier_id = (
            await connection.execute(text(
                "SELECT id FROM subscription_tiers WHERE code='plus'"
            ))
        ).scalar_one()
        await connection.execute(text(
            "INSERT INTO durations (id, minutes, is_enabled, sort_order) "
            "VALUES (1901, 60, 1, 60)"
        ))
        await connection.execute(text(
            "INSERT INTO limit_scopes "
            "(id, code, name, is_enabled, sort_order) VALUES "
            "(1901, 'exact-sale-test', 'Exact sale test', 1, 1901)"
        ))
        for lot_id in range(1901, 1910):
            await connection.execute(text(
                "INSERT INTO lots "
                "(id, funpay_id, provenance_token, provenance_marker_synced, "
                "funpay_node_id, tier_id, duration_id, limit_scope_id, price, "
                "title_ru, title_en, description_ru, description_en, status, "
                "auto_created, config_key) VALUES "
                "(:id, :offer, :token, :marker, 1355, :tier, 1901, 1901, 100, "
                "'Exact', 'Exact', '', '', 'active', 1, :config)"
            ), {
                "id": lot_id,
                "offer": f"offer-{lot_id}" if lot_id != 1902 else None,
                "token": f"token-{lot_id}",
                "marker": 0 if lot_id in {1901, 1909} else 1,
                "tier": tier_id,
                "config": f"exact-{lot_id}",
            })

        bindings = (
            (1901, "offer_id", "offer-1901", None),
            (1902, "provenance_token", None, "token-1902"),
            (1903, "offer_id", "wrong-offer", None),
            (1904, "provenance_token", None, "wrong-token"),
            # Legacy all-NULL snapshots remain insertable for audit, but the
            # cleanup must never trust them. Invalid mixed shapes are blocked
            # directly by ck_orders_bot_lot_binding_shape.
            (1905, None, None, None),
            (1906, "offer_id", "offer-1906", None),
            (1907, "offer_id", "offer-1907", None),
            (1908, "offer_id", "offer-1908", None),
            (1909, "provenance_token", None, "token-1909"),
        )
        for order_id, method, offer_snapshot, token_snapshot in bindings:
            await connection.execute(text(
                "INSERT INTO orders "
                "(id, funpay_order_id, funpay_chat_id, buyer_funpay_id, "
                "buyer_locale, lot_id, lot_binding_method, funpay_offer_id, "
                "lot_provenance_token, tier_id, duration_id, limit_scope_id, "
                "price, status, fulfillment_attempts, created_at) VALUES "
                "(:id, :remote, :chat, :buyer, 'ru', :id, :method, :offer, "
                ":token, :tier, 1901, 1901, 100, 'completed', 0, "
                "CURRENT_TIMESTAMP)"
            ), {
                "id": order_id,
                "remote": f"ORDER{order_id}",
                "chat": f"chat-{order_id}",
                "buyer": f"buyer-{order_id}",
                "method": method,
                "offer": offer_snapshot,
                "token": token_snapshot,
                "tier": tier_id,
            })
            sale_buyer = (
                "tampered-buyer" if order_id == 1906 else f"buyer-{order_id}"
            )
            sale_remote = (
                "TAMPER1907" if order_id == 1907 else f"ORDER{order_id}"
            )
            sale_chat = (
                "tampered-chat" if order_id == 1908 else f"chat-{order_id}"
            )
            await connection.execute(text(
                "INSERT INTO funpay_sales "
                "(id, funpay_order_id, order_id, funpay_chat_id, "
                "buyer_funpay_id, status, created_at, detail_attempts, "
                "updated_at) VALUES "
                "(:id, :remote, :id, :chat, :buyer, 'completed', "
                "CURRENT_TIMESTAMP, 0, CURRENT_TIMESTAMP)"
            ), {
                "id": order_id,
                "remote": sale_remote,
                "chat": sale_chat,
                "buyer": sale_buyer,
            })
            await connection.execute(text(
                "INSERT INTO chat_conversations "
                "(id, funpay_chat_id, buyer_funpay_id, funpay_order_id, "
                "order_id, unread_count, profile_attempts, verified_sale, "
                "created_at, updated_at) VALUES "
                "(:id, :chat, :buyer, :remote, :id, 0, 0, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {
                "id": order_id,
                "chat": sale_chat,
                "buyer": sale_buyer,
                "remote": sale_remote,
            })
        await connection.execute(text(
            "INSERT INTO chat_messages "
            "(id, conversation_id, direction, text, delivery_status, "
            "is_read, created_at) VALUES "
            "(1904, 1904, 'incoming', 'quarantined audit message', "
            "'received', 0, CURRENT_TIMESTAMP)"
        ))

        await connection.run_sync(migration._quarantine_unmanaged_sales)

        # Simulate an old importer trying to resurrect the quarantined chat.
        await connection.execute(text(
            "INSERT INTO funpay_sales "
            "(id, funpay_order_id, order_id, funpay_chat_id, buyer_funpay_id, "
            "status, created_at, detail_attempts, updated_at) VALUES "
            "(1914, 'ORDER1904', 1904, 'chat-1904', 'buyer-1904', "
            "'completed', CURRENT_TIMESTAMP, 0, CURRENT_TIMESTAMP)"
        ))
        await connection.execute(text(
            "UPDATE chat_conversations SET verified_sale=1 WHERE id=1904"
        ))
        await connection.run_sync(migration._quarantine_unmanaged_sales)

        sales = set((await connection.execute(text(
            "SELECT funpay_order_id FROM funpay_sales"
        ))).scalars())
        conversations = {
            row.id: bool(row.verified_sale)
            for row in (await connection.execute(text(
                "SELECT id, verified_sale FROM chat_conversations ORDER BY id"
            )))
        }
        message_conversations = set((await connection.execute(text(
            "SELECT conversation_id FROM chat_messages"
        ))).scalars())
    await engine.dispose()

    assert sales == {"ORDER1901", "ORDER1902"}
    assert conversations == {1901: True, 1902: True, 1904: False}
    assert message_conversations == {1904}


def test_0018_generates_transactional_postgresql_upgrade_and_downgrade_sql():
    database_url = "postgresql+asyncpg://migration:test@localhost/migration"

    upgrade_output = io.StringIO()
    upgrade_config = _alembic_config(database_url)
    upgrade_config.output_buffer = upgrade_output
    command.upgrade(
        upgrade_config,
        "20260714_0017:20260714_0018",
        sql=True,
    )
    upgrade_sql = upgrade_output.getvalue()

    assert "BEGIN;" in upgrade_sql
    assert "ADD COLUMN provenance_token VARCHAR(32)" in upgrade_sql
    assert "ADD COLUMN provenance_marker_synced BOOLEAN DEFAULT false NOT NULL" in upgrade_sql
    assert (
        "md5(random()::text || clock_timestamp()::text || id::text)"
        in upgrade_sql
    )
    assert "ADD CONSTRAINT uq_lots_provenance_token UNIQUE" in upgrade_sql
    assert "ADD COLUMN lot_binding_method VARCHAR(32)" in upgrade_sql
    assert "ADD COLUMN funpay_offer_id VARCHAR(64)" in upgrade_sql
    assert "ADD COLUMN lot_provenance_token VARCHAR(32)" in upgrade_sql
    assert "JOIN lots ON lots.id = orders.lot_id" in upgrade_sql
    assert "orders.funpay_order_id = funpay_sales.funpay_order_id" in upgrade_sql
    assert "orders.buyer_funpay_id = funpay_sales.buyer_funpay_id" in upgrade_sql
    assert "orders.funpay_chat_id = funpay_sales.funpay_chat_id" in upgrade_sql
    assert "orders.lot_binding_method = 'offer_id'" in upgrade_sql
    assert "orders.funpay_offer_id = lots.funpay_id" in upgrade_sql
    assert "orders.lot_binding_method = 'provenance_token'" in upgrade_sql
    assert "lots.provenance_marker_synced = true" in upgrade_sql
    assert "orders.lot_provenance_token = lots.provenance_token" in upgrade_sql
    assert "ALTER COLUMN order_id SET NOT NULL" in upgrade_sql
    assert "ON DELETE CASCADE" in upgrade_sql
    assert "backfill_complete = true" in upgrade_sql
    assert "COMMIT;" in upgrade_sql

    downgrade_output = io.StringIO()
    downgrade_config = _alembic_config(database_url)
    downgrade_config.output_buffer = downgrade_output
    command.downgrade(
        downgrade_config,
        "20260714_0018:20260714_0017",
        sql=True,
    )
    downgrade_sql = downgrade_output.getvalue()

    assert "BEGIN;" in downgrade_sql
    assert "ALTER COLUMN order_id DROP NOT NULL" in downgrade_sql
    assert "ON DELETE SET NULL" in downgrade_sql
    assert "backfill_complete = false" in downgrade_sql
    assert "DROP COLUMN lot_provenance_token" in downgrade_sql
    assert "DROP COLUMN funpay_offer_id" in downgrade_sql
    assert "DROP COLUMN lot_binding_method" in downgrade_sql
    assert "DROP CONSTRAINT uq_lots_provenance_token" in downgrade_sql
    assert "DROP COLUMN provenance_marker_synced" in downgrade_sql
    assert "DROP COLUMN provenance_token" in downgrade_sql
    assert "COMMIT;" in downgrade_sql


async def test_0019_adds_isolated_sale_recovery_queue_and_resets_backfill(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'sale-recovery-0019.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260714_0018")

    await asyncio.to_thread(command.upgrade, config, "20260714_0019")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        version = (
            await connection.execute(
                text("SELECT version_num FROM alembic_version")
            )
        ).scalar_one()
        sync_state = (
            await connection.execute(text(
                "SELECT backfill_cursor, backfill_complete "
                "FROM funpay_sale_sync_state WHERE id=1"
            ))
        ).one()
        schema = await connection.run_sync(
            lambda sync_connection: {
                "tables": set(inspect(sync_connection).get_table_names()),
                "columns": {
                    column["name"]
                    for column in inspect(sync_connection).get_columns(
                        "funpay_sale_candidates"
                    )
                },
                "indexes": {
                    index["name"]
                    for index in inspect(sync_connection).get_indexes(
                        "funpay_sale_candidates"
                    )
                },
                "audit_indexes": {
                    index["name"]
                    for index in inspect(sync_connection).get_indexes(
                        "audit_logs"
                    )
                },
                "order_columns": {
                    column["name"]
                    for column in inspect(sync_connection).get_columns(
                        "orders"
                    )
                },
                "order_indexes": {
                    index["name"]
                    for index in inspect(sync_connection).get_indexes(
                        "orders"
                    )
                },
            }
        )
    await engine.dispose()

    assert version == "20260714_0019"
    assert tuple(sync_state) == (None, 0)
    assert "funpay_sale_candidates" in schema["tables"]
    assert {
        "funpay_order_id",
        "buyer_funpay_id",
        "recovery_state",
        "attempts",
        "next_attempt_at",
        "last_error",
    } <= schema["columns"]
    assert "ix_funpay_sale_candidates_recovery_due" in schema["indexes"]
    assert "ix_audit_logs_event_order" in schema["audit_indexes"]
    assert {
        "confirmation_delivery_status",
        "confirmation_delivery_attempts",
        "confirmation_delivery_next_attempt_at",
        "confirmation_delivery_last_error",
    } <= schema["order_columns"]
    assert (
        "ix_orders_confirmation_delivery_retry" in schema["order_indexes"]
    )

    await asyncio.to_thread(command.downgrade, config, "20260714_0018")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        tables, audit_indexes, order_columns, order_indexes = (
            await connection.run_sync(
                lambda sync_connection: (
                    set(inspect(sync_connection).get_table_names()),
                    {
                        index["name"]
                        for index in inspect(sync_connection).get_indexes(
                            "audit_logs"
                        )
                    },
                    {
                        column["name"]
                        for column in inspect(sync_connection).get_columns(
                            "orders"
                        )
                    },
                    {
                        index["name"]
                        for index in inspect(sync_connection).get_indexes(
                            "orders"
                        )
                    },
                )
            )
        )
        complete = (
            await connection.execute(text(
                "SELECT backfill_complete FROM funpay_sale_sync_state WHERE id=1"
            ))
        ).scalar_one()
    await engine.dispose()

    assert "funpay_sale_candidates" not in tables
    assert "ix_audit_logs_event_order" not in audit_indexes
    assert "confirmation_delivery_status" not in order_columns
    assert "ix_orders_confirmation_delivery_retry" not in order_indexes
    assert bool(complete) is True


async def test_0020_clears_unsourced_expiry_and_revalidates_paid_accounts(
    tmp_path,
    monkeypatch,
):
    database_url = (
        f"sqlite+aiosqlite:///{tmp_path / 'expiry-provenance-0020.db'}"
    )
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260714_0019")
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        tiers = {
            row.code: row.id
            for row in (
                await connection.execute(
                    text(
                        "SELECT id, code FROM subscription_tiers "
                        "WHERE code IN ('plus', 'free')"
                    )
                )
            )
        }
        await connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, login, password_encrypted, totp_secret_encrypted, "
                "tier_id, subscription_expires_at, status, "
                "operator_status_override, validation_rerun_requested) VALUES "
                "(1, 'paid-pending', :password, :totp, :plus, "
                "CURRENT_TIMESTAMP, 'active', NULL, 0), "
                "(2, 'free-active', :password, :totp, :free, "
                "CURRENT_TIMESTAMP, 'active', NULL, 0), "
                "(3, 'paid-disabled', :password, :totp, :plus, "
                "CURRENT_TIMESTAMP, 'disabled', 'disabled', 0), "
                "(4, 'paid-running', :password, :totp, :plus, "
                "CURRENT_TIMESTAMP, 'active', NULL, 0), "
                "(5, 'paid-device-auth', :password, :totp, :plus, "
                "CURRENT_TIMESTAMP, 'active', NULL, 0)"
            ),
            {
                "password": encrypt("password"),
                "totp": encrypt("TOTP"),
                "plus": tiers["plus"],
                "free": tiers["free"],
            },
        )
        for account_id in range(1, 6):
            await connection.execute(
                text(
                    "INSERT INTO account_limits "
                    "(account_id, refresh_token_encrypted, "
                    "subscription_expires_at, refresh_status, "
                    "refresh_recover_attempts) VALUES "
                    "(:account_id, :refresh, CURRENT_TIMESTAMP, 'ok', 0)"
                ),
                {
                    "account_id": account_id,
                    "refresh": encrypt(f"refresh-{account_id}"),
                },
            )
        await connection.execute(
            text(
                "INSERT INTO account_check_jobs "
                "(id, account_id, priority, job_type, status, created_at) "
                "VALUES "
                "(1, 1, 'limit_check', 'limit_check', 'pending', "
                "CURRENT_TIMESTAMP), "
                "(2, 4, 'limit_check', 'limit_check', 'running', "
                "CURRENT_TIMESTAMP), "
                "(3, 5, 'manual', 'device_auth', 'pending', "
                "CURRENT_TIMESTAMP)"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260714_0020")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        account_rows = {
            row.login: (
                row.status,
                row.subscription_expires_at,
                row.subscription_expiry_source,
                bool(row.validation_rerun_requested),
            )
            for row in (
                await connection.execute(
                    text(
                        "SELECT login, status, subscription_expires_at, "
                        "subscription_expiry_source, "
                        "validation_rerun_requested FROM accounts"
                    )
                )
            )
        }
        limit_rows = list(
            await connection.execute(
                text(
                    "SELECT subscription_expires_at, "
                    "subscription_expiry_source FROM account_limits"
                )
            )
        )
        jobs = list(
            await connection.execute(
                text(
                    "SELECT account_id, job_type, status, result "
                    "FROM account_check_jobs ORDER BY id"
                )
            )
        )
        version = (
            await connection.execute(
                text("SELECT version_num FROM alembic_version")
            )
        ).scalar_one()
        check_constraints = await connection.run_sync(
            lambda sync_connection: {
                constraint["name"]
                for table_name in ("accounts", "account_limits")
                for constraint in inspect(sync_connection).get_check_constraints(
                    table_name
                )
            }
        )
    await engine.dispose()

    assert version == "20260714_0020"
    assert {
        "ck_accounts_subscription_expiry_source_trusted",
        "ck_account_limits_subscription_expiry_source_trusted",
    } <= check_constraints
    assert account_rows == {
        "paid-pending": ("pending_validation", None, None, False),
        "free-active": ("active", None, None, False),
        "paid-disabled": ("disabled", None, None, False),
        "paid-running": ("pending_validation", None, None, True),
        "paid-device-auth": ("pending_validation", None, None, True),
    }
    assert all(tuple(row) == (None, None) for row in limit_rows)
    assert [tuple(row) for row in jobs] == [
        (1, "limit_check", "done", "superseded:expiry_provenance_migration"),
        (4, "limit_check", "running", None),
        (5, "device_auth", "pending", None),
        (1, "full_validation", "pending", None),
    ]

    await asyncio.to_thread(command.downgrade, config, "20260714_0019")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        account_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("accounts")
            }
        )
        limit_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns(
                    "account_limits"
                )
            }
        )
    await engine.dispose()
    assert "subscription_expiry_source" not in account_columns
    assert "subscription_expiry_source" not in limit_columns


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
        proxy_schema = await connection.run_sync(
            lambda sync_connection: {
                "columns": {
                    column["name"]: column
                    for column in inspect(sync_connection).get_columns(
                        "proxy_routes"
                    )
                },
                "indexes": {
                    index["name"]: index
                    for index in inspect(sync_connection).get_indexes(
                        "proxy_routes"
                    )
                },
                "account_fks": inspect(sync_connection).get_foreign_keys(
                    "accounts"
                ),
                "settings_fks": inspect(sync_connection).get_foreign_keys(
                    "seller_settings"
                ),
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
    assert version == "20260716_0022"
    assert "funpay_sales" in tables
    assert "funpay_sale_sync_state" in tables
    assert "funpay_sale_candidates" in tables
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
    }
    assert account_columns["tier_id"]["nullable"] is True
    assert {
        "operator_status_override",
        "validation_rerun_requested",
    } <= set(account_columns)
    assert {
        "plan_raw_type", "plan_source", "plan_confidence", "plan_detected_at"
    } <= set(account_columns)
    assert proxy_schema["columns"]["config_revision"]["nullable"] is False
    assert str(
        proxy_schema["columns"]["config_revision"]["default"]
    ).strip("'\"") == "1"
    singleton_index = proxy_schema["indexes"][
        "uq_proxy_routes_single_home_relay"
    ]
    assert singleton_index["unique"] == 1
    assert singleton_index["column_names"] == ["mode"]
    account_proxy_fk = next(
        foreign_key
        for foreign_key in proxy_schema["account_fks"]
        if foreign_key["constrained_columns"] == ["proxy_route_id"]
    )
    settings_proxy_fk = next(
        foreign_key
        for foreign_key in proxy_schema["settings_fks"]
        if foreign_key["constrained_columns"] == ["default_proxy_route_id"]
    )
    assert account_proxy_fk["options"].get("ondelete") == "RESTRICT"
    assert settings_proxy_fk["options"].get("ondelete") == "RESTRICT"
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
    assert version == "20260716_0022"
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
        # The plan evidence remains valid, but pre-0020 subscription dates had
        # no durable provenance and paid accounts therefore revalidate once.
        "verified-active": (plus_id, "pending_validation", None),
        "legacy-disabled": (None, "disabled", "disabled"),
        "legacy-pending": (None, "pending_validation", None),
    }
    assert jobs == {1: 1, 2: 1, 4: 1}
    assert version == "20260716_0022"
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
