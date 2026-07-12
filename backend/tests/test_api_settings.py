import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.settings import SellerSettings


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.cookies.set(COOKIE_NAME, token)
        yield c


async def test_get_settings(auth_client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1, funpay_node_id=55, default_max_active_rentals=3))
    await session.commit()
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["funpay_node_id"] == 55
    assert data["default_max_active_rentals"] == 3
    assert "admin_password_hash" not in data


async def test_update_settings(auth_client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1))
    await session.commit()
    resp = await auth_client.put("/api/settings", json={"default_max_active_rentals": 5})
    assert resp.status_code == 200
    assert resp.json()["default_max_active_rentals"] == 5


async def test_get_settings_not_configured(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 404
