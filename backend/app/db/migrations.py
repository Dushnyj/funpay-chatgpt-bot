import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

_BASELINE_REVISION = "20260713_0001"
_CHAT_REVISION = "20260713_0002"
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_LEGACY_BASELINE_TABLES = {
    "account_check_jobs",
    "account_limits",
    "accounts",
    "audit_logs",
    "bump_logs",
    "durations",
    "limit_scopes",
    "lot_templates",
    "lots",
    "message_templates",
    "orders",
    "price_matrix",
    "rentals",
    "seller_settings",
    "subscription_tiers",
}
_CHAT_TABLES = {"chat_conversations", "chat_messages"}


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    # ConfigParser treats '%' as interpolation syntax (common in passwords).
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )


async def upgrade_database(engine: AsyncEngine) -> None:
    """Upgrade to Alembic head, safely adopting a complete legacy schema.

    Releases before Alembic startup integration created the current metadata
    with ``create_all``. A database that contains every baseline table but no
    ``alembic_version`` is stamped at the baseline and then upgraded. A partial
    legacy schema aborts startup instead of being incorrectly stamped.
    """
    database_url = engine.url.render_as_string(hide_password=False)
    tables = await _table_names(engine)
    application_tables = tables & (_LEGACY_BASELINE_TABLES | _CHAT_TABLES)

    if "alembic_version" not in tables and application_tables:
        missing = sorted(_LEGACY_BASELINE_TABLES - tables)
        if missing:
            raise RuntimeError(
                "Refusing to stamp partial legacy schema; missing tables: "
                + ", ".join(missing)
            )
        present_chat_tables = tables & _CHAT_TABLES
        if present_chat_tables and present_chat_tables != _CHAT_TABLES:
            missing_chat = sorted(_CHAT_TABLES - present_chat_tables)
            raise RuntimeError(
                "Refusing to stamp partial chat schema; missing tables: "
                + ", ".join(missing_chat)
            )
        revision = _CHAT_REVISION if present_chat_tables else _BASELINE_REVISION
        config = _alembic_config(database_url)
        await asyncio.to_thread(command.stamp, config, revision)

    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
