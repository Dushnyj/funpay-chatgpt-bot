import pytest
from httpx import ASGITransport, AsyncClient
from passlib.hash import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.settings import SellerSettings


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_login_success(client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1, admin_password_hash=bcrypt.hash("secret123")))
    await session.commit()

    resp = await client.post("/api/auth/login", json={"password": "secret123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert COOKIE_NAME in resp.cookies


async def test_login_wrong_password(client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1, admin_password_hash=bcrypt.hash("secret123")))
    await session.commit()

    resp = await client.post("/api/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401


async def test_login_no_settings_returns_500(client: AsyncClient, session: AsyncSession):
    resp = await client.post("/api/auth/login", json={"password": "any"})
    assert resp.status_code == 500


async def test_logout_clears_cookie(client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1, admin_password_hash=bcrypt.hash("secret123")))
    await session.commit()

    await client.post("/api/auth/login", json={"password": "secret123"})
    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 200
