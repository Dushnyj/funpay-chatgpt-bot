import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app


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


async def test_create_tier(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.post("/api/tiers", json={"name": "Plus", "is_active": True})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Plus"
    assert "id" in data


async def test_create_tier_duplicate_returns_409(auth_client: AsyncClient, session: AsyncSession):
    await auth_client.post("/api/tiers", json={"name": "Plus"})
    resp = await auth_client.post("/api/tiers", json={"name": "Plus"})
    assert resp.status_code == 409


async def test_update_tier(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.post("/api/tiers", json={"name": "Plus"})
    tier_id = resp.json()["id"]
    resp = await auth_client.patch(f"/api/tiers/{tier_id}", json={"is_active": False})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_delete_tier(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.post("/api/tiers", json={"name": "Plus"})
    tier_id = resp.json()["id"]
    resp = await auth_client.delete(f"/api/tiers/{tier_id}")
    assert resp.status_code == 204


async def test_list_durations(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/durations")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_unauthorized_request_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/tiers")
        assert resp.status_code == 401
