from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings
from app.services.rental_service import RentalService


async def _seed_full(session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    settings = SellerSettings(id=1)
    session.add(settings)
    await session.flush()
    acc = Account(
        login="acc1",
        password_encrypted="plain_pass",
        totp_secret_encrypted="plain_totp",
        tier_id=tier.id,
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        status="active",
    )
    session.add(acc)
    await session.flush()
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="enc",
        chat_5h_remaining_pct=80,
        chat_weekly_remaining_pct=70,
        codex_5h_remaining_pct=60,
        codex_weekly_remaining_pct=50,
        measured_at=datetime.now(timezone.utc),
        refresh_status="ok",
    )
    session.add(limits)
    order = Order(
        funpay_order_id="ord-1",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        lot_id=None,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=599,
        status="pending",
    )
    session.add(order)
    await session.flush()
    return tier, duration, scope, acc, order


async def test_fulfill_order_creates_rental_and_sends_welcome(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()

    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)

    assert rental is not None
    assert rental.account_id == acc.id
    assert rental.order_id == order.id
    assert rental.status == "active"
    assert rental.expires_at > rental.started_at
    assert len(gateway.sent_messages) == 1
    chat_id, text = gateway.sent_messages[0]
    assert chat_id == 100


async def test_fulfill_order_sends_no_account_message_when_pool_empty(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    acc.status = "maintenance"
    await session.flush()
    gateway = FakeChatGateway()
    svc = RentalService()

    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)

    assert rental is None
    assert len(gateway.sent_messages) == 1


async def test_fulfill_order_idempotent_existing_rental(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()
    first = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    gateway.sent_messages.clear()
    second = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    assert second is not None
    assert second.id == first.id
    assert len(gateway.sent_messages) == 0

    rentals = (await session.execute(select(Rental).where(Rental.order_id == order.id))).scalars().all()
    assert len(rentals) == 1


async def test_fulfill_order_records_issued_limits(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()
    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    assert rental is not None
    assert rental.issued_chat_5h_pct == 80
    assert rental.issued_chat_weekly_pct == 70
    assert rental.issued_codex_5h_pct == 60
    assert rental.issued_codex_weekly_pct == 50


async def test_revoke_rental_sets_status(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    tier, duration, scope, acc, order = await _seed_full(session)
    gateway = FakeChatGateway()
    svc = RentalService()
    rental = await svc.fulfill_order(session, gateway, order.id, default_max_active_rentals=1)
    assert rental is not None
    await svc.revoke_rental(session, rental.id)
    await session.refresh(rental)
    assert rental.status == "revoked"
