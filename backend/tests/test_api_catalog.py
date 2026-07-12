import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.catalog import SubscriptionTier


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_list_tiers_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/tiers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_tier_is_rejected(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.post("/api/tiers", json={"name": "Plus", "is_active": True})
    assert resp.status_code == 405


async def test_list_tier_returns_system_metadata(auth_client: AsyncClient, session: AsyncSession):
    session.add(SubscriptionTier(
        name="Pro 5x",
        code="pro_5x",
        system_managed=True,
        is_sellable=False,
        sort_order=40,
        usage_multiplier=5.0,
    ))
    await session.commit()
    resp = await auth_client.get("/api/tiers")
    assert resp.status_code == 200
    assert resp.json()[0]["code"] == "pro_5x"
    assert resp.json()[0]["usage_multiplier"] == 5.0


async def test_update_tier(auth_client: AsyncClient, session: AsyncSession):
    tier = SubscriptionTier(name="Plus", code="plus", is_sellable=False)
    session.add(tier)
    await session.commit()
    resp = await auth_client.patch(
        f"/api/tiers/{tier.id}",
        json={"is_active": False, "is_sellable": True},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    assert resp.json()["is_sellable"] is True


async def test_delete_tier(auth_client: AsyncClient, session: AsyncSession):
    tier = SubscriptionTier(name="Plus", code="plus")
    session.add(tier)
    await session.commit()
    resp = await auth_client.delete(f"/api/tiers/{tier.id}")
    assert resp.status_code == 405


async def test_list_durations(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/durations")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_unauthorized_request_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/tiers")
        assert resp.status_code == 401
