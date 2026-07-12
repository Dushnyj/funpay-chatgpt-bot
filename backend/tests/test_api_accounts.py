import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.account import Account, AccountCheckJob
from app.models.audit import AuditLog
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
    assert data["tier_id"] is None
    assert data["validation_job"]["status"] == "pending"
    assert "password" not in str(data)
    assert "totp_secret" not in str(data)
    account = await session.get(Account, data["id"])
    assert account is not None
    assert account.password_encrypted == "pass"
    raw_password = (
        await session.execute(
            text("SELECT password_encrypted FROM accounts WHERE id=:id"),
            {"id": data["id"]},
        )
    ).scalar_one()
    assert raw_password != "pass"
    jobs = (
        await session.execute(
            select(AccountCheckJob).where(AccountCheckJob.account_id == data["id"])
        )
    ).scalars().all()
    assert [(job.priority, job.job_type, job.status) for job in jobs] == [
        ("new", "full_validation", "pending")
    ]


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
    job_count = (
        await session.execute(select(func.count()).select_from(AccountCheckJob))
    ).scalar_one()
    assert job_count == 2


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


async def test_failed_account_can_be_requeued_with_visible_job(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    created = await auth_client.post("/api/accounts", json={
        "login": "retry-account",
        "password": "pass",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    account = await session.get(Account, created.json()["id"])
    job = (
        await session.execute(
            select(AccountCheckJob).where(AccountCheckJob.account_id == account.id)
        )
    ).scalar_one()
    account.status = "validation_failed"
    job.status = "failed"
    job.error = '{"stage":"login","code":"cloudflare_challenge","detail":"blocked"}'
    await session.commit()

    response = await auth_client.post(f"/api/accounts/{account.id}/recheck")
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "pending_validation"
    assert payload["validation_job"]["status"] == "pending"
    assert payload["validation_job"]["priority"] == "manual"


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


async def test_totp_export_is_not_cacheable_and_is_audited(
    auth_client: AsyncClient, session: AsyncSession
):
    tier_id = await _seed_tier(session)
    created = await auth_client.post(
        "/api/accounts",
        json={
            "login": "totp-audit",
            "password": "pass",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "tier_id": tier_id,
        },
    )

    response = await auth_client.get(
        f"/api/accounts/{created.json()['id']}/totp-export"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    audit = (
        await session.execute(
            select(AuditLog).where(AuditLog.event_type == "totp_export")
        )
    ).scalar_one()
    assert audit.account_id == created.json()["id"]
    assert audit.metadata_ == {"actor": "admin"}
    assert audit.message_text is None
