from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models.settings import SellerSettings
from app.services.seed_data import seed_catalog, seed_message_templates


async def bootstrap_database(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Initialize singleton settings and stable reference data.

    Existing operator-managed values are never overwritten. In particular,
    ``ADMIN_PASSWORD_HASH`` is copied from the environment only when the
    database does not yet contain an admin password.
    """
    async with session_factory() as session:
        seller_settings = await session.get(SellerSettings, 1)
        if seller_settings is None:
            seller_settings = SellerSettings(
                id=1,
                admin_password_hash=settings.admin_password_hash or None,
            )
            session.add(seller_settings)
        elif not seller_settings.admin_password_hash and settings.admin_password_hash:
            seller_settings.admin_password_hash = settings.admin_password_hash

        await seed_catalog(session, commit=False)
        await seed_message_templates(session, commit=False)
        await session.commit()
