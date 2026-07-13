from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Rental, Order
from app.services.account_pool import AccountPool, AccountCriteria


async def _seed_tier_ds(session: AsyncSession):
    tier = SubscriptionTier(
        code="plus", name="Plus", is_active=True, is_sellable=True,
    )
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
    expires_in_days: int | None = 30,
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
        subscription_expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            if expires_in_days is not None
            else None
        ),
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
        plan_type=tier.code or "plus",
        plan_window_status="ok",
        expected_long_window_seconds=(
            30 * 24 * 60 * 60 if tier.code == "free" else 7 * 24 * 60 * 60
        ),
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


async def test_operator_override_blocks_late_validation_status_race(
    session: AsyncSession,
):
    tier, duration, _scope_any = await _seed_tier_ds(session)
    account = await _add_account(session, tier)
    # Simulate a late validation commit that wrote status=active after the
    # operator had already persisted a maintenance override.
    account.operator_status_override = "maintenance"
    account.status = "active"
    await session.flush()

    result = await AccountPool().acquire(
        session,
        AccountCriteria(
            tier_id=tier.id,
            duration_days=duration.days,
            scope="any",
            min_limit_pct=None,
            max_5h_pct=None,
            max_weekly_pct=None,
        ),
        default_max_active_rentals=1,
    )

    assert result is None


async def test_acquire_allows_verified_free_plan_without_billing_expiry(
    session: AsyncSession,
):
    tier, duration, _scope_any = await _seed_tier_ds(session)
    tier.code = "free"
    account = await _add_account(session, tier, expires_in_days=None)
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=duration.days,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=None,
        max_weekly_pct=None,
    )
    result = await AccountPool().acquire(
        session,
        criteria,
        default_max_active_rentals=1,
    )
    assert result is not None and result.id == account.id


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
    first = await _add_account(session, tier, codex_5h=40, codex_weekly=30)
    second = await _add_account(session, tier, login="acc2", codex_5h=70, codex_weekly=60)
    first_limits = await session.get(AccountLimits, first.id)
    second_limits = await session.get(AccountLimits, second.id)
    first_limits.codex_primary_remaining_pct = 40
    first_limits.codex_secondary_remaining_pct = 30
    second_limits.codex_primary_remaining_pct = 70
    second_limits.codex_secondary_remaining_pct = 60

    pool = AccountPool()
    criteria = AccountCriteria(
        tier_id=tier.id, duration_days=7, scope="codex",
        min_limit_pct=50, max_5h_pct=None, max_weekly_pct=None,
    )
    result = await pool.acquire(session, criteria, default_max_active_rentals=1)
    assert result is not None
    assert result.id == second.id


async def test_acquire_scope_codex_accepts_exact_primary_when_secondary_absent(
    session: AsyncSession,
):
    tier, duration, _scope_any = await _seed_tier_ds(session)
    account = await _add_account(session, tier)
    limits = await session.get(AccountLimits, account.id)
    limits.codex_primary_remaining_pct = 95
    limits.codex_primary_window_seconds = 2_592_000
    limits.codex_secondary_remaining_pct = None
    limits.codex_secondary_window_seconds = None

    result = await AccountPool().acquire(
        session,
        AccountCriteria(
            tier_id=tier.id,
            duration_days=duration.days,
            scope="codex",
            min_limit_pct=90,
            max_5h_pct=None,
            max_weekly_pct=None,
        ),
        default_max_active_rentals=1,
    )

    assert result is not None and result.id == account.id


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


async def test_postgresql_query_uses_portable_case_and_skip_locked():
    class _Result:
        def scalar_one_or_none(self):
            return None

    class _CaptureSession:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result()

    session = _CaptureSession()
    criteria = AccountCriteria(
        tier_id=1,
        duration_days=7,
        scope="chat",
        min_limit_pct=50,
        max_5h_pct=None,
        max_weekly_pct=None,
    )
    await AccountPool().acquire(session, criteria, default_max_active_rentals=1)

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "CASE WHEN" in sql
    assert "min(" not in sql.lower()
    assert "FOR UPDATE OF accounts SKIP LOCKED" in sql


async def test_acquire_excluding_does_not_mutate_excluded_account(session: AsyncSession):
    tier, duration, _ = await _seed_tier_ds(session)
    excluded = await _add_account(session, tier, login="excluded")
    candidate = await _add_account(session, tier, login="candidate")
    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=duration.days,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=None,
        max_weekly_pct=None,
    )

    result = await AccountPool().acquire_excluding(
        session,
        criteria,
        exclude_account_id=excluded.id,
        default_max_active_rentals=1,
    )

    assert result is not None and result.id == candidate.id
    assert excluded.status == "active"


@pytest.mark.parametrize("tier_flag", ["is_active", "is_sellable"])
async def test_acquire_honours_operator_tier_switch(
    session: AsyncSession,
    tier_flag: str,
):
    tier, duration, _ = await _seed_tier_ds(session)
    await _add_account(session, tier)
    setattr(tier, tier_flag, False)
    await session.flush()

    result = await AccountPool().acquire(
        session,
        AccountCriteria(
            tier_id=tier.id,
            duration_days=duration.days,
            scope="any",
            min_limit_pct=None,
            max_5h_pct=None,
            max_weekly_pct=None,
        ),
        default_max_active_rentals=1,
    )
    assert result is None


@pytest.mark.parametrize("window_status", ["unknown", "mismatch"])
async def test_acquire_rejects_unverified_plan_window(
    session: AsyncSession,
    window_status: str,
):
    tier, duration, _ = await _seed_tier_ds(session)
    account = await _add_account(session, tier)
    limits = await session.get(AccountLimits, account.id)
    limits.plan_window_status = window_status
    await session.flush()

    result = await AccountPool().acquire(
        session,
        AccountCriteria(
            tier_id=tier.id,
            duration_days=duration.days,
            scope="any",
            min_limit_pct=None,
            max_5h_pct=None,
            max_weekly_pct=None,
        ),
        default_max_active_rentals=1,
    )
    assert result is None


async def test_any_ceilings_use_free_long_window_not_primary_position(
    session: AsyncSession,
):
    tier, duration, _ = await _seed_tier_ds(session)
    tier.code = "free"
    account = await _add_account(session, tier, expires_in_days=None)
    limits = await session.get(AccountLimits, account.id)
    limits.codex_5h_remaining_pct = None
    limits.codex_weekly_remaining_pct = None
    limits.codex_primary_remaining_pct = 80
    limits.codex_primary_window_seconds = 30 * 24 * 60 * 60
    limits.codex_secondary_remaining_pct = None
    limits.codex_secondary_window_seconds = None
    limits.expected_long_window_seconds = 30 * 24 * 60 * 60

    criteria = AccountCriteria(
        tier_id=tier.id,
        duration_days=duration.days,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=30,
        max_weekly_pct=90,
    )
    # Free has no 5-hour observation, so an explicit short-window condition
    # must fail closed instead of treating NULL as a match.
    assert await AccountPool().acquire(session, criteria, 1) is None

    long_only = AccountCriteria(**{**criteria.__dict__, "max_5h_pct": None})
    assert await AccountPool().acquire(session, long_only, 1) is not None
    long_only = AccountCriteria(
        **{**long_only.__dict__, "max_weekly_pct": 70}
    )
    assert await AccountPool().acquire(session, long_only, 1) is None


async def test_any_ceilings_use_paid_five_hour_and_seven_day_semantics(
    session: AsyncSession,
):
    tier, duration, _ = await _seed_tier_ds(session)
    account = await _add_account(session, tier)
    limits = await session.get(AccountLimits, account.id)
    limits.codex_primary_remaining_pct = 20
    limits.codex_primary_window_seconds = 5 * 60 * 60
    limits.codex_secondary_remaining_pct = 80
    limits.codex_secondary_window_seconds = 7 * 24 * 60 * 60
    limits.expected_long_window_seconds = 7 * 24 * 60 * 60
    # ChatGPT allowance is deliberately unknown in production and must not
    # contaminate an exact Codex-window semantics test.
    limits.chat_5h_remaining_pct = None
    limits.chat_weekly_remaining_pct = None

    allowed = AccountCriteria(
        tier_id=tier.id,
        duration_days=duration.days,
        scope="any",
        min_limit_pct=None,
        max_5h_pct=30,
        max_weekly_pct=90,
    )
    assert await AccountPool().acquire(session, allowed, 1) is not None
    blocked = AccountCriteria(**{**allowed.__dict__, "max_5h_pct": 10})
    assert await AccountPool().acquire(session, blocked, 1) is None
