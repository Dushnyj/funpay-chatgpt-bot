from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.settings import SellerSettings


async def get_effective_funpay_key(
    session: AsyncSession, settings: Settings
) -> str:
    """Return the encrypted-at-rest DB key, falling back to the environment."""
    seller_settings = await session.get(SellerSettings, 1)
    if seller_settings is not None and seller_settings.funpay_session_key:
        return seller_settings.funpay_session_key
    return settings.funpay_session_key


def key_status(key: str) -> dict[str, bool | str | None]:
    return {
        "configured": bool(key),
        "last4": key[-4:] if key else None,
    }
