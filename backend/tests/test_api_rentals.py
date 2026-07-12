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


async def test_list_rentals_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/rentals")
    assert resp.status_code == 200
    assert resp.json() == []
