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


async def _seed_catalog(session: AsyncSession):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    session.add(duration)
    scope = LimitScope(code="any", name="Любой")
    session.add(scope)
    await session.flush()
    return tier, duration, scope


async def test_list_lots_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/lots")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_manual_lot(auth_client: AsyncClient, session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    resp = await auth_client.post("/api/lots", json={
        "funpay_node_id": 55,
        "tier_id": tier.id, "duration_id": duration.id, "limit_scope_id": scope.id,
        "price": 599, "title_ru": "Тест", "title_en": "Test",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["price"] == 599
    assert data["auto_created"] is False


async def test_delete_lot(auth_client: AsyncClient, session: AsyncSession):
    tier, duration, scope = await _seed_catalog(session)
    resp = await auth_client.post("/api/lots", json={
        "funpay_node_id": 55,
        "tier_id": tier.id, "duration_id": duration.id, "limit_scope_id": scope.id,
        "price": 599, "title_ru": "Т", "title_en": "T",
    })
    lot_id = resp.json()["id"]
    resp = await auth_client.delete(f"/api/lots/{lot_id}")
    assert resp.status_code == 204
