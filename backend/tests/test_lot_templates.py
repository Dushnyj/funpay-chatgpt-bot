import pytest
from sqlalchemy.exc import IntegrityError

from app.models.lot import LotTemplate
from app.services.lot_templates import (
    DEFAULT_LOT_TEMPLATES,
    LotTemplateRenderError,
    LotTemplateValidationError,
    render_lot_template,
    validate_lot_template_content,
    validate_lot_template_key,
    validate_lot_template_values,
)


@pytest.mark.parametrize(
    "content",
    [
        "{plan.__class__} {days} {condition}",
        "{plan!r} {days} {condition}",
        "{plan:>20} {days} {condition}",
        "{unknown} {days} {condition}",
        "{plan",
    ],
)
def test_lot_template_rejects_unsafe_or_broken_placeholders(content: str):
    with pytest.raises(LotTemplateValidationError):
        validate_lot_template_content(content, field="title_ru")


def test_lot_title_requires_identity_fields():
    with pytest.raises(LotTemplateValidationError, match="must include"):
        validate_lot_template_content("ChatGPT {plan}", field="title_ru")


@pytest.mark.parametrize("key", ["A Bad Key", "x", "../escape", "кириллица"])
def test_lot_template_key_is_strict(key: str):
    with pytest.raises(LotTemplateValidationError):
        validate_lot_template_key(key)


def test_render_lot_template_checks_funpay_output_length():
    template = LotTemplate(
        id=1,
        key="long",
        name="Long",
        title_template_ru=("x" * 250) + "{plan}{days}{condition}",
        title_template_en="{plan} {days} {condition}",
        description_template_ru="",
        description_template_en="",
        is_enabled=True,
        system_managed=False,
    )

    with pytest.raises(LotTemplateRenderError, match="255"):
        render_lot_template(
            template,
            lang="ru",
            variables={
                "plan": "Plus",
                "days": 7,
                "condition": "Codex",
            },
        )


def test_lot_template_source_rejects_possible_post_substitution_overflow():
    with pytest.raises(LotTemplateValidationError, match="after variable"):
        validate_lot_template_values(
            title_ru=("x" * 140) + " {plan} {days} {condition}",
            title_en="{plan} {days} {condition}",
            description_ru="",
            description_en="",
        )


def test_default_lot_copy_is_localized_compact_and_contract_safe():
    template = DEFAULT_LOT_TEMPLATES["default"]
    title_ru_fields = validate_lot_template_content(
        template.title_ru, field="title_ru"
    )
    title_en_fields = validate_lot_template_content(
        template.title_en, field="title_en"
    )
    description_ru_fields = validate_lot_template_content(
        template.description_ru, field="description_ru"
    )
    description_en_fields = validate_lot_template_content(
        template.description_en, field="description_en"
    )

    assert title_ru_fields == title_en_fields
    assert description_ru_fields == description_en_fields
    assert "30 дней только на Free, 7 дней на платных тарифах" in (
        template.description_ru
    )
    assert "последнюю успешную проверку остатка лимита Codex" in (
        template.description_ru
    )
    assert "Данные для входа придут в чат FunPay" in template.description_ru
    assert not set("✅❌⚠⏳🔑📧📊📱🔄📢📖🙏⏰┌┐└┘─│╔╗╚╝║═").intersection(
        template.title_ru + template.title_en
        + template.description_ru + template.description_en
    )
    validate_lot_template_values(
        title_ru=template.title_ru,
        title_en=template.title_en,
        description_ru=template.description_ru,
        description_en=template.description_en,
    )


async def test_enabled_custom_lot_template_target_is_database_unique(session):
    common = {
        "name": "Custom target",
        "tier_id": None,
        "limit_scope_id": None,
        "title_template_ru": "{plan} {days} {condition}",
        "title_template_en": "{plan} {days} {condition}",
        "description_template_ru": "",
        "description_template_en": "",
        "system_managed": False,
    }
    session.add(LotTemplate(key="first-enabled", is_enabled=True, **common))
    await session.commit()

    # Drafts may share a target because they do not participate in resolution.
    session.add(LotTemplate(key="disabled-draft", is_enabled=False, **common))
    await session.commit()

    session.add(LotTemplate(key="second-enabled", is_enabled=True, **common))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
