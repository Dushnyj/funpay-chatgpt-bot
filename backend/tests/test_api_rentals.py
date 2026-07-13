import pytest
from datetime import datetime, timedelta, timezone
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.account import Account
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.funpay_sale import FunPaySale
from app.models.lot import Lot
from app.models.rental import Order, Rental


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_list_rentals_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/rentals")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_manual_status_patch_cannot_bypass_revocation_workflow(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    tier = SubscriptionTier(code="plus", name="Plus", is_active=True)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="any", name="Any")
    session.add_all([tier, duration, scope])
    await session.flush()
    account = Account(
        login="account@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
    )
    session.add(account)
    lot = Lot(
        funpay_id="5001",
        provenance_token="1" * 32,
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
        funpay_order_id="order-1",
        funpay_chat_id="100",
        buyer_funpay_id="200",
        lot_id=lot.id,
        lot_binding_method="offer_id",
        funpay_offer_id=lot.funpay_id,
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        status="completed",
    )
    session.add(order)
    await session.flush()
    session.add(FunPaySale(
        funpay_order_id=order.funpay_order_id,
        order_id=order.id,
        funpay_chat_id=order.funpay_chat_id,
        buyer_funpay_id=order.buyer_funpay_id,
        status="paid",
    ))
    rental = Rental(
        order_id=order.id,
        account_id=account.id,
        buyer_funpay_id="200",
        buyer_funpay_chat_id="100",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        lang="ru",
        started_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="active",
        credentials_delivery_status="sent",
        credentials_delivery_template="welcome",
        credentials_delivery_attempts=1,
    )
    session.add(rental)
    await session.commit()

    response = await auth_client.patch(
        f"/api/rentals/{rental.id}",
        json={"status": "expired"},
    )

    assert response.status_code == 409
    await session.refresh(rental)
    assert rental.status == "active"
