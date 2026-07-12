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


async def test_update_and_list_templates(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.put("/api/templates", json={
        "items": [
            {"key": "welcome", "lang": "ru", "content": "Привет!"},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1

    resp = await auth_client.get("/api/templates")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["key"] == "welcome"


async def test_update_template_idempotent(auth_client: AsyncClient, session: AsyncSession):
    await auth_client.put("/api/templates", json={
        "items": [{"key": "help", "lang": "ru", "content": "Старый"}],
    })
    resp = await auth_client.put("/api/templates", json={
        "items": [{"key": "help", "lang": "ru", "content": "Новый"}],
    })
    assert resp.status_code == 200

    resp = await auth_client.get("/api/templates")
    items = resp.json()
    assert len(items) == 1
    assert items[0]["content"] == "Новый"
