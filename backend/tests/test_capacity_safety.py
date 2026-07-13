from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.account import Account
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings


async def test_database_rejects_shared_account_capacity(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    session.add(
        Account(
            login="unsafe-db-capacity",
            password_encrypted="password",
            totp_secret_encrypted="totp",
            tier_id=tier.id,
            status="maintenance",
            max_active_rentals=2,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_database_rejects_shared_default_capacity(session):
    session.add(SellerSettings(id=1, default_max_active_rentals=2))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_database_rejects_two_occupying_rentals_for_one_account(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    duration = Duration(minutes=30, is_enabled=True)
    scope = LimitScope(code="any", name="Any")
    session.add_all([tier, duration, scope])
    await session.flush()
    account = Account(
        login="single-renter-db",
        password_encrypted="password",
        totp_secret_encrypted="totp",
        tier_id=tier.id,
        status="active",
    )
    session.add(account)
    await session.flush()
    now = datetime.now(timezone.utc)
    for index, status in enumerate(("active", "expiry_pending"), start=1):
        order = Order(
            funpay_order_id=f"single-renter-{index}",
            funpay_chat_id=str(index),
            buyer_funpay_id=str(index),
            tier_id=tier.id,
            duration_id=duration.id,
            limit_scope_id=scope.id,
            price=100,
            status="completed",
        )
        session.add(order)
        await session.flush()
        session.add(Rental(
            order_id=order.id,
            account_id=account.id,
            buyer_funpay_id=str(index),
            buyer_funpay_chat_id=str(index),
            tier_id=tier.id,
            duration_id=duration.id,
            limit_scope_id=scope.id,
            lang="ru",
            started_at=now,
            expires_at=now + timedelta(minutes=30),
            status=status,
        ))
        if index == 1:
            await session.flush()

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
