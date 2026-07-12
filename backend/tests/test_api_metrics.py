import pytest
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.account import Account
from app.models.catalog import SubscriptionTier
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
    session.add(SubscriptionTier(id=1, name="Plus"))
    session.add(SellerSettings(id=1, default_max_active_rentals=2))
    session.add_all([
        Account(
            login="pool-a@example.test",
            password_encrypted="secret-a",
            totp_secret_encrypted="totp-a",
            tier_id=1,
            max_active_rentals=3,
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
            max_active_rentals=10,
            status="maintenance",
        ),
    ])
    await session.commit()

    response = await auth_client.get("/api/metrics")

    assert response.status_code == 200
    assert response.json()["available_accounts"] == 5


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
