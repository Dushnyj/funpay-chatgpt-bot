from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Rental, Order
from app.models.settings import SellerSettings
from app.services.account_pool import AccountPool, AccountCriteria


async def _seed_tier_ds(session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope_any = LimitScope(code="any", name="Любой")
    session.add(scope_any)
    await session.flush()
    return tier, duration, scope_any


async def _add_account(
    session: AsyncSession,
    tier: SubscriptionTier,
    login: str = "acc1",
    expires_in_days: int = 30,
    chat_5h: int = 80,
    chat_weekly: int = 70,
    codex_5h: int = 60,
    codex_weekly: int = 50,
    refresh_status: str = "ok",
    max_active_rentals: int | None = None,
) -> Account:
    acc = Account(
        login=login,
        password_encrypted="enc",
        totp_secret_encrypted="enc",
        tier_id=tier.id,
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        status="active",
        max_active_rentals=max_active_rentals,
    )
    session.add(acc)
    await session.flush()
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="enc",
        chat_5h_remaining_pct=chat_5h,
        chat_weekly_remaining_pct=chat_weekly,
        codex_5h_remaining_pct=codex_5h,
        codex_weekly_remaining_pct=codex_weekly,
        measured_at=datetime.now(timezone.utc),
        refresh_status=refresh_status,
    )
    session.add(limits)
    await session.flush()
    return acc


async def test_acquire_returns_account_matching_basic_criteria(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    acc = await _add_account(session, tier)

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id, duration_days=duration.days, scope="any",
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is not None
    assert result.id == acc.id


async def test_acquire_returns_none_when_no_active_accounts(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    acc = await _add_account(session, tier)
    acc.status = "maintenance"
    await session.flush()

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id, duration_days=duration.days, scope="any",
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is None


async def test_acquire_filters_out_expired_subscription(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    await _add_account(session, tier, expires_in_days=3)

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id, duration_days=7, scope="any",
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is None


async def test_acquire_filters_out_refresh_expired(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    await _add_account(session, tier, refresh_status="expired")

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id, duration_days=7, scope="any",
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is None


async def test_acquire_scope_any_with_max_5h_threshold(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    await _add_account(session, tier, chat_5h=80, codex_5h=80)
    acc2 = await _add_account(session, tier, login="acc2", chat_5h=20, codex_5h=25)

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id, duration_days=7, scope="any",
        min_limit_pct=None, max_5h_pct=30, max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is not None
    assert result.id == acc2.id


async def test_acquire_scope_codex_with_min_limit(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    await _add_account(session, tier, codex_5h=40, codex_weekly=30)
    acc2 = await _add_account(session, tier, login="acc2", codex_5h=70, codex_weekly=60)

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id, duration_days=7, scope="codex",
        min_limit_pct=50, max_5h_pct=None, max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is not None
    assert result.id == acc2.id


async def test_acquire_respects_max_active_rentals(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    acc = await _add_account(session, tier, max_active_rentals=1)

    order = Order(
        funpay_order_id="o1", funpay_chat_id="1", buyer_funpay_id="1",
        lot_id=None, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope_any.id, price=100, status="pending",
    )
    session.add(order)
    await session.flush()
    rental = Rental(
        order_id=order.id, account_id=acc.id,
        buyer_funpay_id="1", buyer_funpay_chat_id="1",
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope_any.id,
        lang="ru", started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
    )
    session.add(rental)
    await session.flush()

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id, duration_days=7, scope="any",
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is None


async def test_acquire_fifo_orders_by_subscription_expires_asc(session: AsyncSession):
    tier, duration, scope_any = await _seed_tier_ds(session)
    acc1 = await _add_account(session, tier, login="acc1", expires_in_days=30)
    acc2 = await _add_account(session, tier, login="acc2", expires_in_days=10)

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id, duration_days=7, scope="any",
        min_limit_pct=None, max_5h_pct=None, max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=5)
    assert result is not None
    assert result.id == acc2.id
