from datetime import datetime, timezone

import pytest

# Регистрируем модель в Base.metadata на этапе импорта модуля,
# чтобы create_all в фикстуре test_engine создал таблицу.
from app.models.account import AccountLimits
from app.models.message import MessageTemplate
from app.services.messages import (
    TemplateRenderError,
    TemplateValidationError,
    render_message,
    usage_template_variables,
    validate_template_content,
)


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


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("Привет {unknown}", "Unknown placeholder"),
        ("Привет {login.__class__}", "Unknown placeholder"),
        ("Привет {login!r}", "conversion"),
        ("Привет {login:>10}", "Format specification"),
        ("Привет {login", "Invalid placeholder syntax"),
    ],
)
def test_validate_template_rejects_unsafe_or_broken_placeholders(content, message):
    with pytest.raises(TemplateValidationError, match=message):
        validate_template_content("welcome", "ru", content)


def test_validate_template_accepts_escaped_braces_and_exact_usage_fields():
    used = validate_template_content(
        "subscription",
        "ru",
        "{{лимит}} {codex_primary_limit}, {codex_primary_window}, "
        "{codex_primary_reset}",
    )
    assert used == {
        "codex_primary_limit",
        "codex_primary_window",
        "codex_primary_reset",
    }


@pytest.mark.parametrize(
    ("key", "content"),
    [
        ("welcome", "Логин {login}"),
        ("replace_success", "Пароль {password}"),
        ("code_success", "Код готов"),
    ],
)
def test_validate_template_requires_delivery_critical_fields(key, content):
    with pytest.raises(TemplateValidationError, match="must include"):
        validate_template_content(key, "ru", content)


@pytest.mark.asyncio
async def test_render_message_reports_missing_variables_clearly(session):
    session.add(
        MessageTemplate(
            key="code_success", lang="ru", content="Код {code}, {expires_in}"
        )
    )
    await session.commit()

    with pytest.raises(TemplateRenderError, match=r"\{expires_in\}"):
        await render_message(session, "code_success", "ru", code="123456")


def test_usage_variables_render_observed_free_30_day_window():
    limits = AccountLimits(
        account_id=1,
        refresh_token_encrypted="token",
        codex_primary_remaining_pct=95,
        codex_primary_window_seconds=30 * 86_400,
        codex_primary_resets_at=datetime(2026, 8, 12, 10, 30, tzinfo=timezone.utc),
    )

    values = usage_template_variables(limits, lang="ru")

    assert values["codex_primary_limit"] == "95%"
    assert values["codex_primary_window"] == "30 дн."
    assert values["codex_primary_reset"] == "12.08.2026 10:30 UTC"


def test_usage_variables_render_observed_paid_7_day_window():
    limits = AccountLimits(
        account_id=1,
        refresh_token_encrypted="token",
        codex_primary_remaining_pct=73,
        codex_primary_window_seconds=7 * 86_400,
        codex_primary_resets_at=datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
    )

    values = usage_template_variables(limits, lang="en")

    assert values["codex_primary_limit"] == "73%"
    assert values["codex_primary_window"] == "7 days"
    assert values["codex_primary_reset"] == "2026-07-20 09:00 UTC"
