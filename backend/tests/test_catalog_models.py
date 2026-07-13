import pytest
from sqlalchemy import select

# Регистрируем модели в Base.metadata на этапе импорта модуля,
# чтобы create_all в фикстуре test_engine создал таблицы.
from app.models.catalog import Duration, LimitScope, SubscriptionTier


@pytest.mark.asyncio
async def test_create_subscription_tier(session):
    tier = SubscriptionTier(name="Plus", description="ChatGPT Plus", is_active=True)
    session.add(tier)
    await session.commit()

    result = await session.execute(select(SubscriptionTier).where(SubscriptionTier.name == "Plus"))
    fetched = result.scalar_one()
    assert fetched.id is not None
    assert fetched.is_active is True
    assert fetched.description == "ChatGPT Plus"


@pytest.mark.asyncio
async def test_create_duration(session):
    dur = Duration(
        minutes=7 * 24 * 60,
        is_enabled=True,
        sort_order=7 * 24 * 60,
    )
    session.add(dur)
    await session.commit()

    result = await session.execute(
        select(Duration).where(Duration.minutes == 7 * 24 * 60)
    )
    fetched = result.scalar_one()
    assert fetched.is_enabled is True
    assert fetched.sort_order == 7 * 24 * 60


@pytest.mark.asyncio
async def test_create_limit_scope(session):
    scope = LimitScope(
        code="codex",
        name="Codex",
        is_enabled=True,
        sort_order=30,
    )
    session.add(scope)
    await session.commit()

    result = await session.execute(select(LimitScope).where(LimitScope.code == "codex"))
    fetched = result.scalar_one()
    assert fetched.name == "Codex"
    assert fetched.is_enabled is True
    assert fetched.sort_order == 30
