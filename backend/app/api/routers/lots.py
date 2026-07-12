from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import LotCreate, LotOut
from app.models.lot import Lot

router = APIRouter(prefix="/api/lots", tags=["lots"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[LotOut])
async def list_lots(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Lot).order_by(Lot.id))
    return result.scalars().all()


@router.post("", response_model=LotOut, status_code=201)
async def create_lot(req: LotCreate, session: AsyncSession = Depends(get_db_session)):
    lot = Lot(
        funpay_node_id=req.funpay_node_id,
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
        status="active",
        auto_created=False,
    )
    session.add(lot)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Invalid or duplicate lot configuration")
    await session.refresh(lot)
    return lot


@router.delete("/{lot_id}", status_code=204)
async def delete_lot(lot_id: int, session: AsyncSession = Depends(get_db_session)):
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")
    lot.status = "deleted"
    await session.commit()
