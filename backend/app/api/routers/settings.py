from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import SettingsOut, SettingsUpdate
from app.models.settings import SellerSettings

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=SettingsOut)
async def get_settings(session: AsyncSession = Depends(get_db_session)):
    settings = await session.get(SellerSettings, 1)
    if settings is None:
        raise HTTPException(status_code=404, detail="Settings not configured")
    return settings


@router.put("", response_model=SettingsOut)
async def update_settings(req: SettingsUpdate, session: AsyncSession = Depends(get_db_session)):
    settings = await session.get(SellerSettings, 1)
    if settings is None:
        settings = SellerSettings(id=1)
        session.add(settings)
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(settings, field, value)
    await session.commit()
    await session.refresh(settings)
    return settings
