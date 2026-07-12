from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import OrderOut
from app.models.rental import Order

router = APIRouter(prefix="/api/orders", tags=["orders"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[OrderOut])
async def list_orders(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Order).order_by(Order.id.desc()))
    return result.scalars().all()


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: int, session: AsyncSession = Depends(get_db_session)):
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
