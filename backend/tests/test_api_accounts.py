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


async def _seed_tier(session: AsyncSession) -> int:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    return tier.id


async def test_list_accounts_empty(auth_client: AsyncClient, session: AsyncSession):
    resp = await auth_client.get("/api/accounts")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_account(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc1", "password": "pass", "totp_secret": "JBSWY3DPEHPK3PXP",
        "tier_id": tier_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["login"] == "acc1"
    assert data["status"] == "pending_validation"
    assert "password" not in str(data)
    assert "totp_secret" not in str(data)


async def test_get_account_detail(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc1", "password": "pass", "totp_secret": "JBSWY3DPEHPK3PXP",
        "tier_id": tier_id,
    })
    acc_id = resp.json()["id"]
    resp = await auth_client.get(f"/api/accounts/{acc_id}")
    assert resp.status_code == 200


async def test_delete_account(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc1", "password": "pass", "totp_secret": "JBSWY3DPEHPK3PXP",
        "tier_id": tier_id,
    })
    acc_id = resp.json()["id"]
    resp = await auth_client.delete(f"/api/accounts/{acc_id}")
    assert resp.status_code == 204


async def test_bulk_add_accounts(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts/bulk", json={
        "tier_id": tier_id,
        "accounts": [
            {"login": "a1", "password": "p", "totp_secret": "JBSWY3DPEHPK3PXP"},
            {"login": "a2", "password": "p", "totp_secret": "JBSWY3DPEHPK3PXP"},
        ],
    })
    assert resp.status_code == 201
    assert resp.json()["created"] == 2


async def test_patch_account_status(auth_client: AsyncClient, session: AsyncSession):
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc1", "password": "pass", "totp_secret": "JBSWY3DPEHPK3PXP",
        "tier_id": tier_id,
    })
    acc_id = resp.json()["id"]
    resp = await auth_client.patch(f"/api/accounts/{acc_id}", json={"status": "active"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


async def test_create_account_with_email(auth_client: AsyncClient, session: AsyncSession):
    """Аккаунт с email + email_password: email возвращается, пароль НЕ утекает."""
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc-email", "password": "pass",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "email": "user@gmail.com", "email_password": "app-pass-123",
        "tier_id": tier_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "user@gmail.com"
    assert "email_password" not in str(data)
    assert "app-pass-123" not in str(data)


async def test_create_account_without_email(auth_client: AsyncClient, session: AsyncSession):
    """Обратная совместимость: аккаунт без email создаётся, email=None."""
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc-noemail", "password": "pass",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "tier_id": tier_id,
    })
    assert resp.status_code == 201
    assert resp.json()["email"] is None


async def test_create_account_without_totp_secret(auth_client: AsyncClient, session: AsyncSession):
    """Раньше totp_secret был обязателен — теперь дефолт "" не падает."""
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc-nototp", "password": "pass",
        "tier_id": tier_id,
    })
    assert resp.status_code == 201
    assert resp.json()["login"] == "acc-nototp"


async def test_get_account_returns_email(auth_client: AsyncClient, session: AsyncSession):
    """AccountWithLimits (GET /{id}) тоже возвращает email."""
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts", json={
        "login": "acc-detail", "password": "pass",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "email": "detail@gmail.com", "email_password": "secret",
        "tier_id": tier_id,
    })
    acc_id = resp.json()["id"]
    resp = await auth_client.get(f"/api/accounts/{acc_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "detail@gmail.com"
    assert "email_password" not in str(data)


async def test_bulk_add_accounts_without_totp(auth_client: AsyncClient, session: AsyncSession):
    """Bulk add с пустым totp_secret не падает (дефолт "" на каждом item)."""
    tier_id = await _seed_tier(session)
    resp = await auth_client.post("/api/accounts/bulk", json={
        "tier_id": tier_id,
        "accounts": [
            {"login": "b1", "password": "p"},
            {"login": "b2", "password": "p", "email": "b2@gmail.com", "email_password": "ep"},
        ],
    })
    assert resp.status_code == 201
    assert resp.json()["created"] == 2
