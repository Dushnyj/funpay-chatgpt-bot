from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.account import Account
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import Lot
from app.models.rental import Order, Rental


@pytest.mark.asyncio
async def test_rental_order_unique(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(minutes=7 * 24 * 60, is_enabled=True)
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, dur, scope])
    await session.flush()

    acc = Account(
        login="u@e.com", password_encrypted="p", totp_secret_encrypted="t",
        tier_id=tier.id, status="active",
    )
    lot = Lot(
        funpay_node_id=1, tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        price=299, title_ru="t", title_en="t", description_ru="", description_en="",
    )
    session.add_all([acc, lot])
    await session.flush()

    order = Order(
        funpay_order_id="fp-order-123",
        funpay_chat_id="chat-123",
        buyer_funpay_id="buyer-1",
        buyer_locale="ru",
        lot_id=lot.id,
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        price=299,
    )
    session.add(order)
    await session.flush()

    r1 = Rental(
        order_id=order.id, account_id=acc.id,
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        buyer_funpay_id="buyer-1", buyer_funpay_chat_id="chat-123",
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    session.add(r1)
    await session.flush()

    # Дубликат rental на тот же order_id — IntegrityError
    r2 = Rental(
        order_id=order.id, account_id=acc.id,
        tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
        buyer_funpay_id="buyer-1", buyer_funpay_chat_id="chat-123",
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    session.add(r2)
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_order_funpay_id_unique(session):
    o1 = Order(funpay_order_id="dup", funpay_chat_id="c", buyer_funpay_id="b",
               buyer_locale="ru", price=100)
    o2 = Order(funpay_order_id="dup", funpay_chat_id="c2", buyer_funpay_id="b2",
               buyer_locale="ru", price=200)
    session.add_all([o1, o2])
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_rental_default_replacement_count(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    dur = Duration(minutes=1 * 24 * 60, is_enabled=True)
    scope = LimitScope(code="any", name="Любой")
    session.add_all([tier, dur, scope])
    await session.flush()

    acc = Account(login="u@e.com", password_encrypted="p", totp_secret_encrypted="t", tier_id=tier.id, status="active")
    lot = Lot(funpay_node_id=1, tier_id=tier.id, duration_id=dur.id, limit_scope_id=scope.id,
              price=99, title_ru="t", title_en="t", description_ru="", description_en="")
    session.add_all([acc, lot])
    await session.flush()

    order = Order(funpay_order_id="o1", funpay_chat_id="c", buyer_funpay_id="b",
                  buyer_locale="ru", lot_id=lot.id, tier_id=tier.id, duration_id=dur.id,
                  limit_scope_id=scope.id, price=99)
    session.add(order)
    await session.flush()

    rental = Rental(
        order_id=order.id, account_id=acc.id, tier_id=tier.id, duration_id=dur.id,
        limit_scope_id=scope.id, buyer_funpay_id="b", buyer_funpay_chat_id="c", lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    session.add(rental)
    await session.commit()

    fetched = await session.get(Rental, rental.id)
    assert fetched.replacement_count == 0
    assert fetched.status == "active"
