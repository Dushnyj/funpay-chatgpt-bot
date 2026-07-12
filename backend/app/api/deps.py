from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, decode_access_token_claims
from app.db.session import get_session
from app.models.settings import SellerSettings


async def get_db_session() -> AsyncSession:
    async for session in get_session():
        yield session


async def get_current_user(
    access_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    session: AsyncSession = Depends(get_db_session),
) -> str:
    if access_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    claims = decode_access_token_claims(access_token)
    if claims is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    settings = await session.get(SellerSettings, 1)
    current_version = settings.admin_session_version if settings is not None else 0
    if claims.session_version != current_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked")
    return claims.subject
