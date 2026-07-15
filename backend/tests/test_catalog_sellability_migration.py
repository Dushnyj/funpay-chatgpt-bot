import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.migrations import _alembic_config


@pytest.mark.asyncio
async def test_0021_disables_only_system_tiers_unsupported_by_funpay(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'sellability-0021.db'}"
    config = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260714_0020")

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE subscription_tiers SET is_sellable = true "
                "WHERE system_managed = true"
            )
        )
        # A supported operator-disabled tier must remain disabled.
        await connection.execute(
            text(
                "UPDATE subscription_tiers SET is_sellable = false "
                "WHERE code = 'plus'"
            )
        )
        # Operator-created tiers are outside the system catalog and must not be
        # rewritten even when their code is unknown to the FunPay form.
        await connection.execute(
            text(
                "INSERT INTO subscription_tiers "
                "(name, code, is_active, system_managed, is_sellable, sort_order) "
                "VALUES ('Custom', 'custom', true, false, true, 999)"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260715_0021")

    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT code, is_sellable FROM subscription_tiers")
            )
        ).all()
    await engine.dispose()

    sellability = {code: bool(value) for code, value in rows}
    for code in (
        "enterprise", "edu", "teachers", "healthcare", "clinicians", "gov",
    ):
        assert sellability[code] is False
    assert sellability["plus"] is False
    assert sellability["business"] is True
    assert sellability["custom"] is True
