from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import MetricsOut
from app.models.account import Account
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings

router = APIRouter(prefix="/api/metrics", tags=["metrics"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=MetricsOut)
async def get_metrics(session: AsyncSession = Depends(get_db_session)):
    active_rentals = (
        await session.execute(
            select(func.count()).select_from(Rental).where(Rental.status == "active")
        )
    ).scalar_one()

    available_accounts = (
        await session.execute(
            select(func.count()).select_from(Account).where(Account.status == "active")
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    orders_today = (
        await session.execute(
            select(func.count()).select_from(Order).where(Order.created_at >= today_start)
        )
    ).scalar_one()

    revenue_brutto = (
        await session.execute(
            select(func.coalesce(func.sum(Order.price), 0)).where(
                Order.created_at >= today_start,
                Order.status.in_(["pending", "completed"]),
            )
        )
    ).scalar_one()

    settings = await session.get(SellerSettings, 1)
    commission = settings.funpay_commission_percent if settings else 15
    revenue_netto = int(revenue_brutto * (100 - commission) / 100)

    return MetricsOut(
        active_rentals=active_rentals,
        available_accounts=available_accounts,
        orders_today=orders_today,
        revenue_brutto=revenue_brutto,
        revenue_netto=revenue_netto,
        bot_status="disconnected",
    )
