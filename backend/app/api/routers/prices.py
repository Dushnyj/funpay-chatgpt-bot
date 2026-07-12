from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import PriceMatrixItem
from app.models.lot import PriceMatrix

router = APIRouter(prefix="/api/prices", tags=["prices"], dependencies=[Depends(get_current_user)])


class PriceUpdateResponse(BaseModel):
    updated: int


class PriceUpdateRequest(BaseModel):
    items: list[PriceMatrixItem] = Field(min_length=1, max_length=10_000)


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
async def update_prices(
    req: PriceUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    await session.execute(delete(PriceMatrix))
    for item in req.items:
        session.add(PriceMatrix(
            tier_id=item.tier_id, duration_id=item.duration_id,
            limit_scope_id=item.limit_scope_id,
            min_limit_pct=item.min_limit_pct, max_5h_pct=item.max_5h_pct,
            max_weekly_pct=item.max_weekly_pct, price=item.price,
        ))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Invalid or duplicate price configuration")
    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is not None:
        try:
            await lifecycle.reconcile_lots()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Prices saved, but FunPay reconciliation failed: {exc}",
            ) from exc
    return PriceUpdateResponse(updated=len(req.items))
