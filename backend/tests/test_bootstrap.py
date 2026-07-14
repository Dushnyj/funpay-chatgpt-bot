from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.bootstrap import bootstrap_database
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import LotTemplate
from app.models.message import MessageTemplate
from app.models.settings import SellerSettings
from app.services.lot_templates import PRE_PREMIUM_LOT_TEMPLATES
from app.services.seed_data import (
    DEFAULT_DURATIONS,
    DEFAULT_LIMIT_SCOPES,
    DEFAULT_LOT_TEMPLATES,
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_TIERS,
    PRE_PREMIUM_MESSAGE_TEMPLATES,
    seed_lot_templates,
    seed_message_templates,
)


async def test_bootstrap_initializes_admin_and_reference_data(test_engine):
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    settings = Settings()

    await bootstrap_database(settings, factory)
    await bootstrap_database(settings, factory)

    async with factory() as session:
        seller_settings = await session.get(SellerSettings, 1)
        assert seller_settings is not None
        assert seller_settings.admin_password_hash == settings.admin_password_hash
        assert (
            await session.execute(select(func.count()).select_from(SubscriptionTier))
        ).scalar_one() == len(DEFAULT_TIERS)
        assert (
            await session.execute(select(func.count()).select_from(Duration))
        ).scalar_one() == len(DEFAULT_DURATIONS)
        assert (
            await session.execute(select(func.count()).select_from(LimitScope))
        ).scalar_one() == len(DEFAULT_LIMIT_SCOPES)
        assert (
            await session.execute(select(func.count()).select_from(MessageTemplate))
        ).scalar_one() == len(DEFAULT_MESSAGE_TEMPLATES) * 2
        assert (
            await session.execute(select(func.count()).select_from(LotTemplate))
        ).scalar_one() == len(DEFAULT_LOT_TEMPLATES)


async def test_bootstrap_does_not_overwrite_existing_admin_hash(test_engine):
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        session.add(SellerSettings(id=1, admin_password_hash="operator-hash"))
        await session.commit()

    await bootstrap_database(Settings(), factory)

    async with factory() as session:
        seller_settings = await session.get(SellerSettings, 1)
        assert seller_settings is not None
        assert seller_settings.admin_password_hash == "operator-hash"


async def test_bootstrap_does_not_restore_deliberately_deleted_durations(
    test_engine,
):
    factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    settings = Settings()
    await bootstrap_database(settings, factory)

    async with factory() as session:
        await session.execute(delete(Duration))
        await session.commit()

    # Simulate a later process restart. SellerSettings already exists, which
    # distinguishes this operator-managed empty catalog from a new install.
    await bootstrap_database(settings, factory)

    async with factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(Duration))
        ).scalar_one() == 0


async def test_message_seed_upgrades_bundled_copy_but_preserves_operator_edits(
    session,
):
    session.add_all(
        (
            MessageTemplate(
                key="welcome",
                lang="ru",
                content=PRE_PREMIUM_MESSAGE_TEMPLATES["welcome"]["ru"],
            ),
            MessageTemplate(
                key="help",
                lang="ru",
                content="Моя справка без изменений",
            ),
        )
    )
    await session.commit()

    await seed_message_templates(session)

    welcome = await session.scalar(
        select(MessageTemplate).where(
            MessageTemplate.key == "welcome",
            MessageTemplate.lang == "ru",
        )
    )
    custom_help = await session.scalar(
        select(MessageTemplate).where(
            MessageTemplate.key == "help",
            MessageTemplate.lang == "ru",
        )
    )
    assert welcome is not None
    assert welcome.content == DEFAULT_MESSAGE_TEMPLATES["welcome"]["ru"]
    assert custom_help is not None
    assert custom_help.content == "Моя справка без изменений"


async def test_lot_seed_upgrades_previous_bundled_copy(session):
    previous = PRE_PREMIUM_LOT_TEMPLATES["default"]
    session.add(
        LotTemplate(
            key=previous.key,
            name=previous.name,
            title_template_ru=previous.title_ru,
            title_template_en=previous.title_en,
            description_template_ru=previous.description_ru,
            description_template_en=previous.description_en,
            is_enabled=True,
            system_managed=True,
        )
    )
    await session.commit()

    await seed_lot_templates(session)

    stored = await session.scalar(
        select(LotTemplate).where(LotTemplate.key == "default")
    )
    current = DEFAULT_LOT_TEMPLATES["default"]
    assert stored is not None
    assert stored.title_template_ru == current.title_ru
    assert stored.title_template_en == current.title_en
    assert stored.description_template_ru == current.description_ru
    assert stored.description_template_en == current.description_en


async def test_lot_seed_preserves_operator_copy(session):
    custom_title_ru = "Свой ChatGPT {plan} · {duration} · {condition}"
    custom_title_en = "Custom ChatGPT {plan} · {duration} · {condition}"
    session.add(
        LotTemplate(
            key="default",
            name="Переименованный шаблон",
            title_template_ru=custom_title_ru,
            title_template_en=custom_title_en,
            description_template_ru="Собственное описание",
            description_template_en="Custom description",
            is_enabled=True,
            system_managed=True,
        )
    )
    await session.commit()

    await seed_lot_templates(session)

    stored = await session.scalar(
        select(LotTemplate).where(LotTemplate.key == "default")
    )
    assert stored is not None
    assert stored.title_template_ru == custom_title_ru
    assert stored.title_template_en == custom_title_en
    assert stored.description_template_ru == "Собственное описание"
    assert stored.description_template_en == "Custom description"
