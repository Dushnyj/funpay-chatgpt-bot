from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import MetricsOut
from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.catalog import SubscriptionTier
from app.models.rental import OCCUPYING_RENTAL_STATUSES, Order, Rental
from app.models.settings import SellerSettings
from app.services.subscription_eligibility import (
    trusted_paid_subscription_expiry,
)

router = APIRouter(prefix="/api/metrics", tags=["metrics"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=MetricsOut)
async def get_metrics(request: Request, session: AsyncSession = Depends(get_db_session)):
    active_rentals = (
        await session.execute(
            select(func.count()).select_from(Rental).where(Rental.status == "active")
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

    active_by_account = (
        select(Rental.account_id, func.count(Rental.id).label("active_count"))
        .where(Rental.status.in_(OCCUPYING_RENTAL_STATUSES))
        .group_by(Rental.account_id)
        .subquery()
    )
    active_count = func.coalesce(active_by_account.c.active_count, 0)
    # One account cannot isolate logout between multiple buyers.
    account_capacity = 1
    free_capacity = case(
        (account_capacity > active_count, account_capacity - active_count),
        else_=0,
    )
    reserved_for_replacement = (
        select(Rental.id)
        .where(Rental.replacement_target_account_id == Account.id)
        .exists()
    )
    active_checks = select(AccountCheckJob.account_id).where(
        AccountCheckJob.status.in_(("pending", "running"))
    )
    available_accounts = (
        await session.execute(
            select(func.coalesce(func.sum(free_capacity), 0))
            .select_from(Account)
            .join(AccountLimits, AccountLimits.account_id == Account.id)
            .join(SubscriptionTier, SubscriptionTier.id == Account.tier_id)
            .outerjoin(active_by_account, active_by_account.c.account_id == Account.id)
            .where(
                Account.status == "active",
                Account.operator_status_override.is_(None),
                SubscriptionTier.is_active.is_(True),
                SubscriptionTier.is_sellable.is_(True),
                AccountLimits.refresh_status == "ok",
                AccountLimits.plan_window_status == "ok",
                AccountLimits.measured_at >= now - timedelta(hours=1),
                ~reserved_for_replacement,
                Account.id.not_in(active_checks),
                or_(
                    and_(
                        SubscriptionTier.code != "free",
                        trusted_paid_subscription_expiry(now),
                    ),
                    and_(
                        SubscriptionTier.code == "free",
                        Account.subscription_expires_at.is_(None),
                    ),
                ),
            )
        )
    ).scalar_one()

    lifecycle = getattr(request.app.state, "lifecycle", None)
    runner = getattr(lifecycle, "runner", None)
    runtime_error = getattr(lifecycle, "last_funpay_error", None) or getattr(
        runner, "last_error", None,
    )
    if runtime_error:
        bot_status = "error"
    elif runner is not None and getattr(runner, "started", False):
        bot_status = "connected"
    else:
        bot_status = "disconnected"

    return MetricsOut(
        active_rentals=active_rentals,
        available_accounts=available_accounts,
        orders_today=orders_today,
        revenue_brutto=revenue_brutto,
        revenue_netto=revenue_netto,
        bot_status=bot_status,
    )
