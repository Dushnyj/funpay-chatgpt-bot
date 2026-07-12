from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import (
    FunPayKeyStatus,
    FunPayKeyUpdate,
    StatusResponse,
    SettingsOut,
    SettingsUpdate,
    TelegramConfigStatus,
    TelegramConfigUpdate,
)
from app.config import get_settings as get_app_settings
from app.models.settings import SellerSettings
from app.services.golden_key import get_effective_funpay_key, key_status
from app.telegram_notifier import TelegramNotifier, get_effective_telegram_config

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=SettingsOut)
async def get_settings(session: AsyncSession = Depends(get_db_session)):
    settings = await session.get(SellerSettings, 1)
    if settings is None:
        raise HTTPException(status_code=404, detail="Settings not configured")
    return settings


@router.put("", response_model=SettingsOut)
async def update_settings(
    req: SettingsUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    settings = await session.get(SellerSettings, 1)
    if settings is None:
        settings = SellerSettings(id=1)
        session.add(settings)
    update = req.model_dump(exclude_unset=True)
    for field, value in update.items():
        setattr(settings, field, value)
    await session.commit()
    await session.refresh(settings)
    if "funpay_node_id" in update:
        lifecycle = getattr(request.app.state, "lifecycle", None)
        if lifecycle is not None and hasattr(lifecycle, "reconfigure_funpay"):
            await lifecycle.reconfigure_funpay()
    return settings


@router.get("/funpay-key", response_model=FunPayKeyStatus)
async def get_funpay_key_status(
    session: AsyncSession = Depends(get_db_session),
) -> FunPayKeyStatus:
    key = await get_effective_funpay_key(session, get_app_settings())
    return FunPayKeyStatus(**key_status(key))


@router.put("/funpay-key", response_model=FunPayKeyStatus)
async def set_funpay_key(
    req: FunPayKeyUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> FunPayKeyStatus:
    settings = await session.get(SellerSettings, 1)
    if settings is None:
        settings = SellerSettings(id=1)
        session.add(settings)
    settings.funpay_session_key = req.key
    settings.funpay_session_valid = False
    await session.commit()
    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is not None and hasattr(lifecycle, "reconfigure_funpay"):
        await lifecycle.reconfigure_funpay(req.key)
    return FunPayKeyStatus(**key_status(req.key))


@router.delete("/funpay-key", response_model=FunPayKeyStatus)
async def clear_funpay_key(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> FunPayKeyStatus:
    settings = await session.get(SellerSettings, 1)
    if settings is not None:
        settings.funpay_session_key = None
        settings.funpay_session_valid = False
        await session.commit()
    key = get_app_settings().funpay_session_key
    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is not None and hasattr(lifecycle, "reconfigure_funpay"):
        # Reload after clearing the DB override so an intentionally configured
        # environment fallback remains consistent with the reported status.
        await lifecycle.reconfigure_funpay()
    return FunPayKeyStatus(**key_status(key))


def _telegram_status(token: str, chat_id: str) -> TelegramConfigStatus:
    return TelegramConfigStatus(
        configured=bool(token and chat_id),
        token_last4=token[-4:] if token else None,
        seller_chat_id=chat_id or None,
    )


@router.get("/telegram", response_model=TelegramConfigStatus)
async def get_telegram_status(
    session: AsyncSession = Depends(get_db_session),
) -> TelegramConfigStatus:
    token, chat_id = await get_effective_telegram_config(session)
    return _telegram_status(token, chat_id)


@router.put("/telegram", response_model=TelegramConfigStatus)
async def update_telegram_config(
    req: TelegramConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> TelegramConfigStatus:
    settings = await session.get(SellerSettings, 1)
    if settings is None:
        settings = SellerSettings(id=1)
        session.add(settings)
    update = req.model_dump(exclude_unset=True)
    if "token" in update:
        settings.telegram_bot_token = update["token"]
    if "seller_chat_id" in update:
        settings.telegram_seller_chat_id = update["seller_chat_id"]
    await session.commit()
    token, chat_id = await get_effective_telegram_config(session)
    return _telegram_status(token, chat_id)


@router.delete("/telegram", response_model=TelegramConfigStatus)
async def clear_telegram_config(
    session: AsyncSession = Depends(get_db_session),
) -> TelegramConfigStatus:
    settings = await session.get(SellerSettings, 1)
    if settings is not None:
        settings.telegram_bot_token = None
        settings.telegram_seller_chat_id = None
        await session.commit()
    app_settings = get_app_settings()
    return _telegram_status(
        app_settings.telegram_bot_token, app_settings.telegram_seller_chat_id
    )


@router.post("/telegram/test", response_model=StatusResponse)
async def test_telegram_config(
    session: AsyncSession = Depends(get_db_session),
) -> StatusResponse:
    token, chat_id = await get_effective_telegram_config(session)
    if not token or not chat_id:
        raise HTTPException(status_code=400, detail="Telegram is not configured")
    try:
        await TelegramNotifier(token, chat_id).send_test()
    except Exception:
        raise HTTPException(status_code=502, detail="Telegram test failed")
    return StatusResponse(status="ok")
