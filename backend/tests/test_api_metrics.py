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


async def test_metrics_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_rentals"] == 0
    assert data["available_accounts"] == 0
    assert data["orders_today"] == 0
    assert data["bot_status"] in ("connected", "disconnected")
