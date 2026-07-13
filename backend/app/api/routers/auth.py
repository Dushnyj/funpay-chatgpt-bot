from __future__ import annotations

from dataclasses import dataclass, field
import math
from threading import Lock
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token, hash_password, verify_password
from app.api.deps import get_current_user, get_db_session
from app.config import get_settings as get_app_settings
from app.models.settings import SellerSettings

router = APIRouter(prefix="/api/auth", tags=["auth"])

_MAX_TRACKED_CLIENTS = 4096


@dataclass(slots=True)
class _ClientLoginThrottle:
    failures: list[float] = field(default_factory=list)
    blocked_until: float | None = None
    last_seen: float = 0.0


_login_throttles: dict[str, _ClientLoginThrottle] = {}
_login_throttles_lock = Lock()


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


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
    retry_after = _client_retry_after(
        client_key,
        window_seconds=app_settings.admin_login_window_seconds,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )
    settings = (
        await session.execute(
            select(SellerSettings)
            .where(SellerSettings.id == 1)
        )
    ).scalar_one_or_none()
    if settings is None or not settings.admin_password_hash:
        raise HTTPException(status_code=500, detail="Admin not configured")

    password_valid = verify_password(req.password, settings.admin_password_hash)
    if not password_valid:
        _record_client_failure(
            client_key,
            max_attempts=app_settings.admin_login_max_attempts,
            window_seconds=app_settings.admin_login_window_seconds,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong password")
    _clear_client_throttle(client_key)
    token = create_access_token(session_version=settings.admin_session_version)
    _set_auth_cookie(response, token)
    return StatusResponse(status="ok")


def _client_retry_after(client_key: str, *, window_seconds: int) -> int | None:
    now = time.monotonic()
    with _login_throttles_lock:
        state = _login_throttles.get(client_key)
        if state is None:
            return None
        state.last_seen = now
        cutoff = now - window_seconds
        state.failures = [value for value in state.failures if value > cutoff]
        if state.blocked_until is not None and state.blocked_until > now:
            return max(1, math.ceil(state.blocked_until - now))
        if not state.failures:
            _login_throttles.pop(client_key, None)
        else:
            state.blocked_until = None
        return None


def _record_client_failure(
    client_key: str,
    *,
    max_attempts: int,
    window_seconds: int,
) -> None:
    now = time.monotonic()
    with _login_throttles_lock:
        if client_key not in _login_throttles and len(_login_throttles) >= _MAX_TRACKED_CLIENTS:
            oldest = min(
                _login_throttles,
                key=lambda key: _login_throttles[key].last_seen,
            )
            _login_throttles.pop(oldest, None)
        state = _login_throttles.setdefault(client_key, _ClientLoginThrottle())
        cutoff = now - window_seconds
        state.failures = [value for value in state.failures if value > cutoff]
        state.failures.append(now)
        state.last_seen = now
        if len(state.failures) >= max_attempts:
            state.blocked_until = now + window_seconds


def _clear_client_throttle(client_key: str) -> None:
    with _login_throttles_lock:
        _login_throttles.pop(client_key, None)


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
