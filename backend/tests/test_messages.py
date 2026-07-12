import pytest

# Регистрируем модель в Base.metadata на этапе импорта модуля,
# чтобы create_all в фикстуре test_engine создал таблицу.
from app.models.message import MessageTemplate
from app.services.messages import render_message


@pytest.mark.asyncio
async def test_render_welcome_substitutes_all_vars(session):
    template = MessageTemplate(
        key="welcome", lang="ru",
        content="Логин: {login}\nПароль: {password}\nПодписка до {expires_at}\nЛимиты: чат {chat_5h}%/{chat_weekly}% codex {codex_5h}%/{codex_weekly}%",
    )
    session.add(template)
    await session.commit()

    rendered = await render_message(
        session, "welcome", "ru",
        login="user@example.com",
        password="pass123",
        expires_at="2026-08-01",
        chat_5h=82, chat_weekly=67, codex_5h=90, codex_weekly=75,
    )
    assert "user@example.com" in rendered
    assert "pass123" in rendered
    assert "2026-08-01" in rendered
    assert "82%/67%" in rendered
    assert "90%/75%" in rendered


@pytest.mark.asyncio
async def test_render_message_missing_template_raises(session):
    with pytest.raises(ValueError, match="MessageTemplate"):
        await render_message(session, "nonexistent", "ru")


@pytest.mark.asyncio
async def test_render_code_success(session):
    template = MessageTemplate(
        key="code_success", lang="ru",
        content="🔑 Код: {code}\nОсталось: {expires_in}",
    )
    session.add(template)
    await session.commit()

    rendered = await render_message(session, "code_success", "ru", code="482193", expires_in="23ч 14мин")
    assert "482193" in rendered
    assert "23ч 14мин" in rendered


@pytest.mark.asyncio
async def test_render_falls_back_to_ru_if_lang_missing(session):
    # Только ru-шаблон, запрашиваем en — должен вернуться ru
    template = MessageTemplate(key="help", lang="ru", content="Помощь")
    session.add(template)
    await session.commit()

    rendered = await render_message(session, "help", "en")
    assert rendered == "Помощь"
