import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.catalog import SubscriptionTier, Duration, LimitScope


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_update_and_get_prices(auth_client: AsyncClient, session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()

    resp = await auth_client.put("/api/prices", json={
        "items": [
            {"tier_id": tier.id, "duration_id": duration.id, "limit_scope_id": scope.id, "price": 599},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1

    resp = await auth_client.get("/api/prices")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["price"] == 599
