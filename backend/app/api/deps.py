from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, decode_access_token
from app.db.session import get_session


async def get_current_user(
    access_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> str:
    if access_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    sub = decode_access_token(access_token)
    if sub is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return sub


async def get_db_session() -> AsyncSession:
    async for session in get_session():
        yield session
