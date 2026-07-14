from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import OrderOut
from app.models.audit import AuditLog
from app.models.rental import Order
from app.services.order_provenance import (
    exact_lot_binding_exists,
    verified_sale_for_order_exists,
)
from app.services.order_notifications import (
    BUYER_ORDER_CONFIRMED_EVENT,
    BUYER_ORDER_CONFIRMED_DUE_EVENT,
    BUYER_ORDER_CONFIRMED_REQUEUED_EVENT,
)

router = APIRouter(prefix="/api/orders", tags=["orders"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[OrderOut])
async def list_orders(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(
        select(Order)
        .where(
            exact_lot_binding_exists(Order),
            verified_sale_for_order_exists(Order),
        )
        .order_by(Order.id.desc())
    )
    return result.scalars().all()


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: int, session: AsyncSession = Depends(get_db_session)):
    order = await session.scalar(
        select(Order).where(
            Order.id == order_id,
            exact_lot_binding_exists(Order),
            verified_sale_for_order_exists(Order),
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.post("/{order_id}/retry-confirmation", response_model=OrderOut)
async def retry_order_confirmation(
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    """Requeue a confirmation that exhausted per-chat delivery retries."""

    order = await session.scalar(
        select(Order)
        .where(
            Order.id == order_id,
            exact_lot_binding_exists(Order),
            verified_sale_for_order_exists(Order),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != "completed":
        raise HTTPException(status_code=409, detail="Order is not completed")

    sent_marker = await session.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.event_type == BUYER_ORDER_CONFIRMED_EVENT,
            AuditLog.order_id == order.id,
        )
        .limit(1)
    )
    if sent_marker is not None or order.confirmation_delivery_status == "sent":
        raise HTTPException(
            status_code=409,
            detail="Order confirmation was already delivered",
        )
    if order.confirmation_delivery_status != "manual":
        raise HTTPException(
            status_code=409,
            detail="Order confirmation is already queued for delivery",
        )

    due_marker = await session.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.event_type == BUYER_ORDER_CONFIRMED_DUE_EVENT,
            AuditLog.order_id == order.id,
        )
        .limit(1)
    )
    if due_marker is None:
        session.add(AuditLog(
            event_type=BUYER_ORDER_CONFIRMED_DUE_EVENT,
            order_id=order.id,
            chat_id=order.funpay_chat_id,
            metadata_={"template": "order_confirmed"},
        ))
    session.add(AuditLog(
        event_type=BUYER_ORDER_CONFIRMED_REQUEUED_EVENT,
        order_id=order.id,
        chat_id=order.funpay_chat_id,
        metadata_={"previous_attempts": order.confirmation_delivery_attempts},
    ))
    order.confirmation_delivery_status = "pending"
    order.confirmation_delivery_attempts = 0
    order.confirmation_delivery_next_attempt_at = None
    order.confirmation_delivery_last_error = None
    await session.commit()
    await session.refresh(order)
    return order
