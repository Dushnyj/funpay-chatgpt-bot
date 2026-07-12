from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import PriceMatrixItem
from app.models.lot import PriceMatrix

router = APIRouter(prefix="/api/prices", tags=["prices"], dependencies=[Depends(get_current_user)])


class PriceUpdateResponse(BaseModel):
    updated: int


class PriceUpdateRequest(BaseModel):
    items: list[PriceMatrixItem]


@router.get("", response_model=list[PriceMatrixItem])
async def list_prices(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(PriceMatrix))
    return [
        PriceMatrixItem(
            tier_id=pm.tier_id, duration_id=pm.duration_id, limit_scope_id=pm.limit_scope_id,
            min_limit_pct=pm.min_limit_pct, max_5h_pct=pm.max_5h_pct,
            max_weekly_pct=pm.max_weekly_pct, price=pm.price,
        )
        for pm in result.scalars().all()
    ]


@router.put("", response_model=PriceUpdateResponse)
async def update_prices(req: PriceUpdateRequest, session: AsyncSession = Depends(get_db_session)):
    await session.execute(delete(PriceMatrix))
    for item in req.items:
        session.add(PriceMatrix(
            tier_id=item.tier_id, duration_id=item.duration_id,
            limit_scope_id=item.limit_scope_id,
            min_limit_pct=item.min_limit_pct, max_5h_pct=item.max_5h_pct,
            max_weekly_pct=item.max_weekly_pct, price=item.price,
        ))
    await session.commit()
    return PriceUpdateResponse(updated=len(req.items))
