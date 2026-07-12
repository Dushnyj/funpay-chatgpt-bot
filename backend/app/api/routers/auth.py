from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token, verify_password
from app.api.deps import get_db_session
from app.models.settings import SellerSettings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class StatusResponse(BaseModel):
    status: str


@router.post("/login", response_model=StatusResponse)
async def login(
    req: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> StatusResponse:
    settings = await session.get(SellerSettings, 1)
    if settings is None or not settings.admin_password_hash:
        raise HTTPException(status_code=500, detail="Admin not configured")
    if not verify_password(req.password, settings.admin_password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong password")
    token = create_access_token()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400,
    )
    return StatusResponse(status="ok")


@router.post("/logout", response_model=StatusResponse)
async def logout(response: Response) -> StatusResponse:
    response.delete_cookie(COOKIE_NAME)
    return StatusResponse(status="ok")
