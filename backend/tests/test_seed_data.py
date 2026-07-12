import pytest
from sqlalchemy import func, select

# Регистрируем модель в Base.metadata на этапе импорта модуля,
# чтобы create_all в фикстуре test_engine создал таблицу.
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.message import MessageTemplate
from app.services.seed_data import (
    DEFAULT_DURATIONS,
    DEFAULT_LIMIT_SCOPES,
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_TIERS,
    seed_catalog,
    seed_message_templates,
)


@pytest.mark.asyncio
async def test_seed_message_templates_creates_all_keys(session):
    await seed_message_templates(session)

    # Проверяем что создались шаблоны для всех ключей и RU+EN
    for key in DEFAULT_MESSAGE_TEMPLATES:
        for lang in ("ru", "en"):
            result = await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.key == key, MessageTemplate.lang == lang
                )
            )
            assert result.scalar_one() is not None, f"missing {key}/{lang}"


@pytest.mark.asyncio
async def test_seed_message_templates_idempotent(session):
    await seed_message_templates(session)
    expected_count = len(DEFAULT_MESSAGE_TEMPLATES) * 2  # ru + en
    count_result = await session.execute(select(func.count()).select_from(MessageTemplate))
    assert count_result.scalar_one() == expected_count

    # Повторный вызов — число не меняется
    await seed_message_templates(session)
    count_result = await session.execute(select(func.count()).select_from(MessageTemplate))
    assert count_result.scalar_one() == expected_count


@pytest.mark.asyncio
async def test_seed_catalog_is_complete_idempotent_and_preserves_existing(session):
    session.add(
        SubscriptionTier(name="Plus", description="operator value", is_active=False)
    )
    await session.commit()

    await seed_catalog(session)
    await seed_catalog(session)

    tiers = (await session.execute(select(SubscriptionTier))).scalars().all()
    durations = (await session.execute(select(Duration))).scalars().all()
    scopes = (await session.execute(select(LimitScope))).scalars().all()
    assert {tier.name for tier in tiers} == {name for name, _ in DEFAULT_TIERS}
    assert {duration.days for duration in durations} == set(DEFAULT_DURATIONS)
    assert {scope.code for scope in scopes} == {
        code for code, _ in DEFAULT_LIMIT_SCOPES
    }
    plus = next(tier for tier in tiers if tier.name == "Plus")
    assert plus.description == "operator value"
    assert plus.is_active is False
    assert plus.code == "plus"
    assert plus.system_managed is True
    assert plus.is_sellable is True
    assert {tier.code for tier in tiers} == {
        "free", "go", "plus", "pro_5x", "pro_20x", "business",
        "enterprise", "edu", "teachers", "healthcare", "clinicians", "gov",
    }
