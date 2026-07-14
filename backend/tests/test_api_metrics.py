from datetime import datetime, timezone

import pytest
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_metrics_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_rentals"] == 0
    assert data["available_accounts"] == 0
    assert data["orders_today"] == 0
    assert data["bot_status"] == "disconnected"


async def test_metrics_report_free_rental_capacity(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    session.add(SubscriptionTier(
        id=1,
        code="free",
        name="Free",
        is_active=True,
        is_sellable=True,
    ))
    session.add(SellerSettings(id=1, default_max_active_rentals=1))
    session.add_all([
        Account(
            login="pool-a@example.test",
            password_encrypted="secret-a",
            totp_secret_encrypted="totp-a",
            tier_id=1,
            max_active_rentals=1,
            status="active",
        ),
        Account(
            login="pool-b@example.test",
            password_encrypted="secret-b",
            totp_secret_encrypted="totp-b",
            tier_id=1,
            max_active_rentals=None,
            status="active",
        ),
        Account(
            login="paused@example.test",
            password_encrypted="secret-c",
            totp_secret_encrypted="totp-c",
            tier_id=1,
            max_active_rentals=1,
            status="maintenance",
        ),
    ])
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="any", name="Any", is_enabled=True)
    session.add_all([duration, scope])
    await session.flush()
    measured_at = datetime.now(timezone.utc)
    session.add_all([
        AccountLimits(
            account_id=1,
            refresh_token_encrypted="refresh-a",
            refresh_status="ok",
            measured_at=measured_at,
            plan_type="free",
            plan_window_status="ok",
            expected_long_window_seconds=30 * 24 * 60 * 60,
        ),
        AccountLimits(
            account_id=2,
            refresh_token_encrypted="refresh-b",
            refresh_status="ok",
            measured_at=measured_at,
            plan_type="free",
            plan_window_status="ok",
            expected_long_window_seconds=30 * 24 * 60 * 60,
        ),
        AccountLimits(
            account_id=3,
            refresh_token_encrypted="refresh-c",
            refresh_status="ok",
            measured_at=measured_at,
            plan_type="free",
            plan_window_status="ok",
            expected_long_window_seconds=30 * 24 * 60 * 60,
        ),
    ])
    order = Order(
        funpay_order_id="expiry-pending-capacity",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        tier_id=1,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        status="completed",
    )
    session.add(order)
    await session.flush()
    session.add(Rental(
        order_id=order.id,
        account_id=1,
        buyer_funpay_id="200",
        buyer_funpay_chat_id="100",
        tier_id=1,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        lang="ru",
        started_at=measured_at,
        expires_at=measured_at,
        status="expiry_pending",
        credentials_delivery_status="sent",
        credentials_delivery_template="welcome",
    ))
    await session.commit()

    response = await auth_client.get("/api/metrics")

    assert response.status_code == 200
    assert response.json()["available_accounts"] == 1


async def test_metrics_count_only_paid_accounts_with_trusted_expiry(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    tier = SubscriptionTier(
        code="plus",
        name="Plus",
        is_active=True,
        is_sellable=True,
    )
    session.add(tier)
    await session.flush()
    now = datetime.now(timezone.utc)
    accounts = [
        Account(
            login="trusted-paid@example.test",
            password_encrypted="password",
            totp_secret_encrypted="totp",
            tier_id=tier.id,
            status="active",
            subscription_expires_at=now.replace(year=now.year + 1),
            subscription_expiry_source="accounts_check",
        ),
        Account(
            login="unsourced-paid@example.test",
            password_encrypted="password",
            totp_secret_encrypted="totp",
            tier_id=tier.id,
            status="active",
            subscription_expires_at=now.replace(year=now.year + 1),
        ),
    ]
    session.add_all(accounts)
    await session.flush()
    session.add_all([
        AccountLimits(
            account_id=account.id,
            refresh_token_encrypted="refresh",
            refresh_status="ok",
            measured_at=now,
            plan_type="plus",
            plan_window_status="ok",
        )
        for account in accounts
    ])
    await session.commit()

    response = await auth_client.get("/api/metrics")

    assert response.status_code == 200
    assert response.json()["available_accounts"] == 1


async def test_metrics_exclude_replacement_target_from_available_capacity(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    tier = SubscriptionTier(
        code="plus",
        name="Plus",
        is_active=True,
        is_sellable=True,
    )
    duration = Duration(minutes=60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="any", name="Any", is_enabled=True)
    session.add_all([tier, duration, scope])
    await session.flush()
    old_account = Account(
        login="old-capacity@example.test",
        password_encrypted="secret-old",
        totp_secret_encrypted="totp-old",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc),
    )
    target_account = Account(
        login="target-capacity@example.test",
        password_encrypted="secret-target",
        totp_secret_encrypted="totp-target",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc),
    )
    session.add_all([old_account, target_account])
    await session.flush()
    measured_at = datetime.now(timezone.utc)
    old_account.subscription_expires_at = measured_at.replace(year=measured_at.year + 1)
    target_account.subscription_expires_at = measured_at.replace(
        year=measured_at.year + 1,
    )
    session.add_all(
        [
            AccountLimits(
                account_id=old_account.id,
                refresh_token_encrypted="refresh-old",
                refresh_status="ok",
                measured_at=measured_at,
                plan_type="plus",
                plan_window_status="ok",
            ),
            AccountLimits(
                account_id=target_account.id,
                refresh_token_encrypted="refresh-target",
                refresh_status="ok",
                measured_at=measured_at,
                plan_type="plus",
                plan_window_status="ok",
            ),
        ]
    )
    order = Order(
        funpay_order_id="reserved-metrics-order",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        status="completed",
    )
    session.add(order)
    await session.flush()
    session.add(
        Rental(
            order_id=order.id,
            account_id=old_account.id,
            replacement_target_account_id=target_account.id,
            buyer_funpay_id="200",
            buyer_funpay_chat_id="100",
            tier_id=tier.id,
            duration_id=duration.id,
            limit_scope_id=scope.id,
            lang="ru",
            started_at=measured_at,
            expires_at=measured_at,
            status="active",
            credentials_delivery_status="sent",
        )
    )
    await session.commit()

    response = await auth_client.get("/api/metrics")

    assert response.status_code == 200
    assert response.json()["active_rentals"] == 1
    assert response.json()["available_accounts"] == 0


@pytest.mark.parametrize("job_status", ["pending", "running"])
async def test_metrics_exclude_accounts_with_active_check_jobs(
    auth_client: AsyncClient,
    session: AsyncSession,
    job_status: str,
):
    tier = SubscriptionTier(
        code="plus",
        name="Plus",
        is_active=True,
        is_sellable=True,
    )
    session.add(tier)
    await session.flush()
    measured_at = datetime.now(timezone.utc)
    account = Account(
        login="checking-capacity@example.test",
        password_encrypted="secret",
        totp_secret_encrypted="totp",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=measured_at.replace(year=measured_at.year + 1),
    )
    session.add(account)
    await session.flush()
    session.add_all(
        [
            AccountLimits(
                account_id=account.id,
                refresh_token_encrypted="refresh",
                refresh_status="ok",
                measured_at=measured_at,
                plan_type="plus",
                plan_window_status="ok",
            ),
            AccountCheckJob(
                account_id=account.id,
                priority="limit_check",
                job_type="limit_check",
                status=job_status,
                started_at=(measured_at if job_status == "running" else None),
            ),
        ]
    )
    await session.commit()

    response = await auth_client.get("/api/metrics")

    assert response.status_code == 200
    assert response.json()["active_rentals"] == 0
    assert response.json()["available_accounts"] == 0


async def test_metrics_use_live_runner_state(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    previous = getattr(app.state, "lifecycle", None)
    app.state.lifecycle = SimpleNamespace(
        runner=SimpleNamespace(started=True, last_error=None),
        last_funpay_error=None,
    )
    try:
        response = await auth_client.get("/api/metrics")
    finally:
        if previous is None:
            del app.state.lifecycle
        else:
            app.state.lifecycle = previous

    assert response.status_code == 200
    assert response.json()["bot_status"] == "connected"
