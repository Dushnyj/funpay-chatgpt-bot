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
    async with AsyncClient(transport=transport, base_url="https://test") as c:
        yield c


async def test_login_success(client: AsyncClient, session: AsyncSession):
    session.add(SellerSettings(id=1, admin_password_hash=bcrypt.hash("secret123")))
    await session.commit()

    resp = await client.post("/api/auth/login", json={"password": "secret123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert COOKIE_NAME in resp.cookies
    assert "Secure" in resp.headers["set-cookie"]
    assert "HttpOnly" in resp.headers["set-cookie"]


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


async def test_login_rate_limit_returns_retry_after(
    client: AsyncClient, session: AsyncSession
):
    session.add(SellerSettings(id=1, admin_password_hash=bcrypt.hash("secret123")))
    await session.commit()

    for _ in range(5):
        response = await client.post("/api/auth/login", json={"password": "wrong"})
        assert response.status_code == 401
    limited = await client.post("/api/auth/login", json={"password": "wrong"})

    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1


async def test_change_password_revokes_old_cookie_and_allows_new_login(
    client: AsyncClient, session: AsyncSession
):
    session.add(SellerSettings(id=1, admin_password_hash=bcrypt.hash("old-password")))
    await session.commit()
    login = await client.post(
        "/api/auth/login", json={"password": "old-password"}
    )
    old_cookie = login.cookies[COOKIE_NAME]

    changed = await client.post(
        "/api/auth/change-password",
        json={
            "current_password": "old-password",
            "new_password": "new-password-123",
        },
    )

    assert changed.status_code == 200
    assert changed.cookies[COOKIE_NAME] != old_cookie
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as old_client:
        old_client.cookies.set(COOKIE_NAME, old_cookie)
        rejected = await old_client.get("/api/settings")
    assert rejected.status_code == 401
    assert (await client.post(
        "/api/auth/login", json={"password": "old-password"}
    )).status_code == 401
    assert (await client.post(
        "/api/auth/login", json={"password": "new-password-123"}
    )).status_code == 200
