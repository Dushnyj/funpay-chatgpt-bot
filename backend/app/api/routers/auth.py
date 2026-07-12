from __future__ import annotations

import math
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token, hash_password, verify_password
from app.api.deps import get_current_user, get_db_session
from app.config import get_settings as get_app_settings
from app.models.settings import SellerSettings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRateLimiter:
    """Small per-process sliding-window limiter for admin password attempts."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def retry_after(
        self, client_key: str, *, max_attempts: int, window_seconds: int
    ) -> int | None:
        now = time.monotonic()
        attempts = self._failures[client_key]
        cutoff = now - window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._failures.pop(client_key, None)
            return None
        if len(attempts) < max_attempts:
            return None
        return max(1, math.ceil(attempts[0] + window_seconds - now))

    def record_failure(self, client_key: str) -> None:
        self._failures[client_key].append(time.monotonic())

    def clear(self, client_key: str | None = None) -> None:
        if client_key is None:
            self._failures.clear()
        else:
            self._failures.pop(client_key, None)


_login_limiter = LoginRateLimiter()


class LoginRequest(BaseModel):
    password: str


class StatusResponse(BaseModel):
    status: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=128)


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=get_app_settings().admin_cookie_secure,
        samesite="lax",
        max_age=86400,
    )


@router.post("/login", response_model=StatusResponse)
async def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> StatusResponse:
    app_settings = get_app_settings()
    client_key = request.client.host if request.client is not None else "unknown"
    retry_after = _login_limiter.retry_after(
        client_key,
        max_attempts=app_settings.admin_login_max_attempts,
        window_seconds=app_settings.admin_login_window_seconds,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    settings = await session.get(SellerSettings, 1)
    if settings is None or not settings.admin_password_hash:
        raise HTTPException(status_code=500, detail="Admin not configured")
    if not verify_password(req.password, settings.admin_password_hash):
        _login_limiter.record_failure(client_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong password")
    _login_limiter.clear(client_key)
    token = create_access_token(session_version=settings.admin_session_version)
    _set_auth_cookie(response, token)
    return StatusResponse(status="ok")


@router.post("/change-password", response_model=StatusResponse)
async def change_password(
    req: ChangePasswordRequest,
    response: Response,
    _current_user: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> StatusResponse:
    settings = (
        await session.execute(
            select(SellerSettings)
            .where(SellerSettings.id == 1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if settings is None or not settings.admin_password_hash:
        raise HTTPException(status_code=503, detail="Admin not configured")
    if not verify_password(req.current_password, settings.admin_password_hash):
        raise HTTPException(status_code=401, detail="Wrong password")
    if verify_password(req.new_password, settings.admin_password_hash):
        raise HTTPException(status_code=400, detail="New password must be different")

    settings.admin_password_hash = hash_password(req.new_password)
    settings.admin_session_version += 1
    await session.commit()

    token = create_access_token(session_version=settings.admin_session_version)
    _set_auth_cookie(response, token)
    return StatusResponse(status="ok")


@router.post("/logout", response_model=StatusResponse)
async def logout(response: Response) -> StatusResponse:
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        secure=get_app_settings().admin_cookie_secure,
        samesite="lax",
    )
    return StatusResponse(status="ok")
