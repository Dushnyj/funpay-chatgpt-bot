import pytest
from sqlalchemy import func, select

# Регистрируем модель в Base.metadata на этапе импорта модуля,
# чтобы create_all в фикстуре test_engine создал таблицу.
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import LotTemplate
from app.models.message import MessageTemplate
from app.services.lot_templates import DEFAULT_LOT_TEMPLATES
from app.services.messages import validate_template_content
from app.services.seed_data import (
    DEFAULT_DURATIONS,
    DEFAULT_LIMIT_SCOPES,
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_TIERS,
    LEGACY_LIMIT_MESSAGE_TEMPLATES,
    seed_catalog,
    seed_lot_templates,
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


def test_default_message_templates_use_only_valid_placeholders():
    for key, translations in DEFAULT_MESSAGE_TEMPLATES.items():
        for lang, content in translations.items():
            validate_template_content(key, lang, content)


@pytest.mark.asyncio
async def test_seed_message_templates_idempotent(session):
    await seed_message_templates(session)
    expected_count = len(DEFAULT_MESSAGE_TEMPLATES) * 2  # ru + en
    count_result = await session.execute(
        select(func.count()).select_from(MessageTemplate)
    )
    assert count_result.scalar_one() == expected_count

    # Повторный вызов — число не меняется
    await seed_message_templates(session)
    count_result = await session.execute(
        select(func.count()).select_from(MessageTemplate)
    )
    assert count_result.scalar_one() == expected_count


@pytest.mark.asyncio
async def test_seed_lot_templates_is_idempotent_and_preserves_content(session):
    await seed_lot_templates(session)
    template = (
        await session.execute(
            select(LotTemplate).where(LotTemplate.key == "default")
        )
    ).scalar_one()
    template.description_template_ru = "Свой текст"
    template.is_enabled = False
    await session.commit()

    await seed_lot_templates(session)
    rows = (await session.execute(select(LotTemplate))).scalars().all()

    assert len(rows) == len(DEFAULT_LOT_TEMPLATES)
    assert rows[0].description_template_ru == "Свой текст"
    assert rows[0].is_enabled is True
    assert rows[0].system_managed is True

@pytest.mark.asyncio
async def test_seed_upgrades_only_exact_legacy_limit_defaults(session):
    legacy_welcome = MessageTemplate(
        key="welcome",
        lang="ru",
        content=LEGACY_LIMIT_MESSAGE_TEMPLATES["welcome"]["ru"],
    )
    customized_subscription = MessageTemplate(
        key="subscription",
        lang="ru",
        content="Мой шаблон: {tier}",
    )
    session.add_all([legacy_welcome, customized_subscription])
    await session.commit()

    await seed_message_templates(session)
    await session.refresh(legacy_welcome)
    await session.refresh(customized_subscription)

    assert legacy_welcome.content == DEFAULT_MESSAGE_TEMPLATES["welcome"]["ru"]
    assert "{codex_primary_window}" in legacy_welcome.content
    assert customized_subscription.content == "Мой шаблон: {tier}"


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
    assert all(duration.sort_order == duration.days for duration in durations)
    assert {scope.code for scope in scopes} == {
        code for code, _ in DEFAULT_LIMIT_SCOPES
    }
    scopes_by_code = {scope.code: scope for scope in scopes}
    assert scopes_by_code["any"].is_enabled is True
    assert scopes_by_code["chat"].is_enabled is False
    assert scopes_by_code["codex"].is_enabled is True
    assert [
        scope.code for scope in sorted(scopes, key=lambda scope: scope.sort_order)
    ] == ["any", "chat", "codex"]
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


@pytest.mark.asyncio
async def test_seed_catalog_preserves_operator_sellable_override(session):
    await seed_catalog(session)
    plus = (
        await session.execute(
            select(SubscriptionTier).where(SubscriptionTier.code == "plus")
        )
    ).scalar_one()
    plus.is_sellable = False
    await session.commit()

    await seed_catalog(session)
    await session.refresh(plus)

    assert plus.system_managed is True
    assert plus.is_sellable is False


@pytest.mark.asyncio
async def test_seed_catalog_preserves_limit_scope_availability_override(session):
    await seed_catalog(session)
    codex = (
        await session.execute(
            select(LimitScope).where(LimitScope.code == "codex")
        )
    ).scalar_one()
    codex.is_enabled = False
    codex.sort_order = 5
    await session.commit()

    await seed_catalog(session)
    await session.refresh(codex)

    assert codex.is_enabled is False
    assert codex.sort_order == 30


@pytest.mark.asyncio
async def test_seed_catalog_does_not_restore_missing_duration(session):
    await seed_catalog(session)
    duration = (
        await session.execute(select(Duration).where(Duration.days == 3))
    ).scalar_one()
    await session.delete(duration)
    custom = Duration(days=8, is_enabled=True, sort_order=999)
    session.add(custom)
    await session.commit()

    await seed_catalog(session)

    durations = (await session.execute(select(Duration))).scalars().all()
    assert 3 not in {item.days for item in durations}
    custom = next(item for item in durations if item.days == 8)
    assert custom.sort_order == 8
