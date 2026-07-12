import pytest
from sqlalchemy import func, select

# Регистрируем модель в Base.metadata на этапе импорта модуля,
# чтобы create_all в фикстуре test_engine создал таблицу.
from app.models.message import MessageTemplate
from app.services.seed_data import DEFAULT_MESSAGE_TEMPLATES, seed_message_templates


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
