import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.audit import AuditLog
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.funpay_sale import FunPaySale
from app.models.lot import Lot
from app.models.rental import Order
from app.services.order_notifications import (
    BUYER_ORDER_CONFIRMED_DUE_EVENT,
    BUYER_ORDER_CONFIRMED_REQUEUED_EVENT,
)


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_list_orders_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/orders")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_retry_confirmation_requeues_manual_verified_order(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    tier = SubscriptionTier(
        code="plus",
        name="Plus",
        is_active=True,
        is_sellable=True,
    )
    duration = Duration(minutes=60, is_enabled=True, sort_order=1)
    scope = LimitScope(code="any", name="Any", is_enabled=True)
    session.add_all([tier, duration, scope])
    await session.flush()
    lot = Lot(
        funpay_id="901",
        provenance_token="9" * 32,
        provenance_marker_synced=True,
        funpay_node_id=55,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        title_ru="Аренда Plus",
        title_en="Plus rental",
        status="active",
    )
    session.add(lot)
    await session.flush()
    order = Order(
        funpay_order_id="manual-confirmation-retry",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        lot_id=lot.id,
        lot_binding_method="offer_id",
        funpay_offer_id=lot.funpay_id,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        status="completed",
        confirmation_delivery_status="manual",
        confirmation_delivery_attempts=12,
        confirmation_delivery_last_error="RuntimeError",
    )
    session.add(order)
    await session.flush()
    session.add_all([
        FunPaySale(
            funpay_order_id=order.funpay_order_id,
            order_id=order.id,
            funpay_chat_id=order.funpay_chat_id,
            buyer_funpay_id=order.buyer_funpay_id,
            status="completed",
        ),
        AuditLog(
            event_type=BUYER_ORDER_CONFIRMED_DUE_EVENT,
            order_id=order.id,
            chat_id=order.funpay_chat_id,
        ),
    ])
    await session.commit()

    response = await auth_client.post(
        f"/api/orders/{order.id}/retry-confirmation"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["confirmation_delivery_status"] == "pending"
    assert payload["confirmation_delivery_attempts"] == 0
    assert payload["confirmation_delivery_next_attempt_at"] is None
    assert payload["confirmation_delivery_last_error"] is None
    await session.refresh(order)
    assert order.confirmation_delivery_status == "pending"
    markers = list((await session.execute(
        select(AuditLog).where(
            AuditLog.event_type == BUYER_ORDER_CONFIRMED_REQUEUED_EVENT,
            AuditLog.order_id == order.id,
        )
    )).scalars())
    assert len(markers) == 1
    assert markers[0].metadata_["previous_attempts"] == 12


async def test_retry_confirmation_rejects_unverified_order(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    order = Order(
        funpay_order_id="unverified-manual-confirmation",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        buyer_locale="ru",
        price=100,
        status="completed",
        confirmation_delivery_status="manual",
        confirmation_delivery_attempts=12,
    )
    session.add(order)
    await session.commit()

    response = await auth_client.post(
        f"/api/orders/{order.id}/retry-confirmation"
    )

    assert response.status_code == 404
    await session.refresh(order)
    assert order.confirmation_delivery_status == "manual"
