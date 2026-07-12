from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import FakeChatGateway
from app.models.account import Account
from app.models.catalog import SubscriptionTier, Duration, LimitScope
from app.models.rental import Order, Rental
from app.services.rental_expiry import RentalExpiryService


async def _make_rental(
    session: AsyncSession,
    expires_delta: timedelta,
    chat_id: str = "100",
    status: str = "active",
) -> Rental:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    acc = Account(
        login="acc1", password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=tier.id, status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    order = Order(
        funpay_order_id="o1", funpay_chat_id=chat_id, buyer_funpay_id="200",
        lot_id=None, tier_id=tier.id, duration_id=duration.id,
        limit_scope_id=scope.id, price=100, status="pending",
    )
    session.add(order)
    await session.flush()
    rental = Rental(
        order_id=order.id, account_id=acc.id,
        buyer_funpay_id="200", buyer_funpay_chat_id=chat_id,
        tier_id=tier.id, duration_id=duration.id, limit_scope_id=scope.id,
        lang="ru", started_at=datetime.now(timezone.utc) - timedelta(days=8),
        expires_at=datetime.now(timezone.utc) + expires_delta,
        status=status,
    )
    session.add(rental)
    await session.flush()
    return rental


async def test_expire_marks_overdue_rentals(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    rental = await _make_rental(session, expires_delta=timedelta(seconds=-1))
    gateway = FakeChatGateway()
    svc = RentalExpiryService()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 1
    await session.refresh(rental)
    assert rental.status == "expired"
    assert len(gateway.sent_messages) == 1


async def test_expire_skips_active_rentals(session: AsyncSession):
    await _make_rental(session, expires_delta=timedelta(days=1))
    gateway = FakeChatGateway()
    svc = RentalExpiryService()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 0
    assert len(gateway.sent_messages) == 0


async def test_expire_skips_already_expired(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _make_rental(session, expires_delta=timedelta(seconds=-1), status="expired")
    gateway = FakeChatGateway()
    svc = RentalExpiryService()
    expired = await svc.expire_overdue(session, gateway)
    assert len(expired) == 0
    assert len(gateway.sent_messages) == 0


async def test_expire_sends_to_correct_chat(session: AsyncSession):
    from app.services.seed_data import seed_message_templates
    await seed_message_templates(session)
    await _make_rental(session, expires_delta=timedelta(seconds=-1), chat_id="555")
    gateway = FakeChatGateway()
    svc = RentalExpiryService()
    await svc.expire_overdue(session, gateway)
    assert len(gateway.sent_messages) == 1
    chat_id, _ = gateway.sent_messages[0]
    assert chat_id == 555
