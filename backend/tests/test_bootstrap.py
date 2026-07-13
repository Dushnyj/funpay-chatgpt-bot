from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.bootstrap import bootstrap_database
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import LotTemplate
from app.models.message import MessageTemplate
from app.models.settings import SellerSettings
from app.services.seed_data import (
    DEFAULT_DURATIONS,
    DEFAULT_LIMIT_SCOPES,
    DEFAULT_LOT_TEMPLATES,
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_TIERS,
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
