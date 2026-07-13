from __future__ import annotations

from collections import Counter
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_lifecycle import FunPayUnavailableError
from app.api.deps import get_current_user, get_db_session
from app.api.schemas import LotCreate, LotOut
from app.models.catalog import LimitScope
from app.models.lot import Lot
from app.models.settings import SellerSettings
from app.services.offer_configuration import (
    OfferConfigurationError,
    validate_offer_configurations,
)

router = APIRouter(prefix="/api/lots", tags=["lots"], dependencies=[Depends(get_current_user)])


class LotStatusUpdate(BaseModel):
    status: Literal["active", "paused"]


class LotSyncResponse(BaseModel):
    status: Literal["ok"] = "ok"
    created: int = 0
    updated: int = 0
    paused: int = 0
    activated: int = 0
    total: int = 0


@router.get("", response_model=list[LotOut])
async def list_lots(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(
        select(Lot)
        .join(LimitScope, LimitScope.id == Lot.limit_scope_id)
        .where(LimitScope.code.in_(("any", "codex")))
        .order_by(Lot.id)
    )
    return result.scalars().all()


@router.post("", response_model=LotOut, status_code=201)
async def create_lot(
    req: LotCreate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        await validate_offer_configurations(session, [req])
    except OfferConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    settings = await session.get(SellerSettings, 1)
    node_id = req.funpay_node_id or (settings.funpay_node_id if settings else None)
    if not node_id:
        raise HTTPException(
            status_code=422,
            detail="FunPay Node ID is required in the lot or seller settings",
        )
    lifecycle = _require_lifecycle(request)
    lot = Lot(
        # Manual publications are intentionally independent from the automatic
        # price-matrix key. This permits a custom title/price/node for the same
        # account criteria without colliding with or being managed as an auto lot.
        config_key=f"manual:{uuid.uuid4().hex}",
        funpay_node_id=node_id,
        tier_id=req.tier_id,
        duration_id=req.duration_id,
        limit_scope_id=req.limit_scope_id,
        min_limit_pct=req.min_limit_pct,
        max_5h_pct=req.max_5h_pct,
        max_weekly_pct=req.max_weekly_pct,
        price=req.price,
        title_ru=req.title_ru,
        title_en=req.title_en,
        description_ru=req.description_ru,
        description_en=req.description_en,
        status="paused",
        paused_reason="manual_pending_sync",
        auto_created=False,
    )
    session.add(lot)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Invalid or duplicate lot configuration")
    try:
        await lifecycle.sync_manual_lot(lot.id, active=True)
    except Exception as exc:
        await session.refresh(lot)
        lot.status = "paused"
        lot.paused_reason = "sync_failed"
        await session.commit()
        _raise_remote_error(exc, "Lot saved as paused, but FunPay publication failed")
    await session.refresh(lot)
    return lot


@router.post("/sync", response_model=LotSyncResponse)
async def sync_lots(request: Request) -> LotSyncResponse:
    lifecycle = _require_lifecycle(request)
    try:
        actions = await lifecycle.reconcile_lots()
    except Exception as exc:
        _raise_remote_error(exc, "FunPay lot reconciliation failed")
    counts = Counter(action.action for action in actions)
    return LotSyncResponse(
        created=counts["create"],
        updated=counts["update"],
        paused=counts["pause"],
        activated=counts["activate"],
        total=len(actions),
    )


@router.patch("/{lot_id}", response_model=LotOut)
async def update_lot_status(
    lot_id: int,
    req: LotStatusUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    lifecycle = _require_lifecycle(request)
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    if lot.status == "deleted":
        raise HTTPException(status_code=409, detail="Deleted lot cannot change status")

    if req.status == "active":
        try:
            # Re-check catalog availability at activation time. A manual lot
            # may have been paused before its tier, duration or scope was
            # disabled, and must not bypass the same rules as a new lot.
            await validate_offer_configurations(session, [lot])
        except OfferConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        if req.status == "active" and not lot.funpay_id:
            await lifecycle.sync_manual_lot(lot.id, active=True)
        elif req.status == "paused" and not lot.funpay_id:
            lot.status = "paused"
            lot.paused_reason = "manual"
            await session.commit()
        else:
            await lifecycle.set_lot_active(lot.id, req.status == "active")
    except Exception as exc:
        _raise_remote_error(exc, "FunPay did not confirm the lot status change")
    await session.refresh(lot)
    return lot


@router.delete("/{lot_id}", status_code=204)
async def delete_lot(
    lot_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    if lot.auto_created:
        raise HTTPException(
            status_code=409,
            detail="Automatic lots must be removed through the price matrix",
        )
    if lot.funpay_id:
        lifecycle = _require_lifecycle(request)
        try:
            await lifecycle.set_lot_active(lot.id, False)
        except Exception as exc:
            _raise_remote_error(exc, "FunPay did not confirm that the lot was removed")
    lot.status = "deleted"
    lot.paused_reason = "manual_deleted"
    await session.commit()


def _require_lifecycle(request: Request):
    lifecycle = getattr(request.app.state, "lifecycle", None)
    required = ("sync_manual_lot", "set_lot_active", "reconcile_lots")
    if lifecycle is None or not all(hasattr(lifecycle, name) for name in required):
        raise HTTPException(status_code=503, detail="FunPay runtime is unavailable")
    return lifecycle


def _raise_remote_error(exc: Exception, detail: str) -> None:
    if isinstance(exc, FunPayUnavailableError):
        raise HTTPException(status_code=503, detail="FunPay is not connected") from exc
    raise HTTPException(status_code=502, detail=detail) from exc
