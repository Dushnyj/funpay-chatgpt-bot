from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers import accounts as accounts_router
from app.api.auth import COOKIE_NAME, create_access_token
from app.main import app
from app.models.account import (
    Account,
    AccountCheckJob,
    AccountLimits,
    EmailOAuthCredential,
)
from app.models.audit import AuditLog
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.rental import Order, Rental


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


async def test_get_account_detail_exports_exact_observed_usage_windows(
    auth_client: AsyncClient, session: AsyncSession
):
    created = await auth_client.post(
        "/api/accounts",
        json={"login": "usage-detail", "password": "pass"},
    )
    account_id = created.json()["id"]
    reset_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    session.add(AccountLimits(
        account_id=account_id,
        refresh_token_encrypted="refresh",
        codex_primary_remaining_pct=93,
        codex_primary_window_seconds=2592000,
        codex_primary_resets_at=reset_at,
        codex_secondary_remaining_pct=None,
        codex_secondary_window_seconds=None,
        codex_secondary_resets_at=None,
        codex_5h_remaining_pct=None,
        codex_weekly_remaining_pct=None,
    ))
    await session.commit()

    response = await auth_client.get(f"/api/accounts/{account_id}")

    assert response.status_code == 200
    limits = response.json()["limits"]
    assert limits["codex_primary_remaining_pct"] == 93
    assert limits["codex_primary_window_seconds"] == 2592000
    assert limits["codex_primary_resets_at"] == reset_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert limits["codex_secondary_remaining_pct"] is None
    assert limits["codex_secondary_window_seconds"] is None
    assert limits["codex_secondary_resets_at"] is None
    # Legacy fields are still in the response but do not mislabel the 30-day
    # observation as a 5-hour/weekly allowance.
    assert limits["codex_5h_remaining_pct"] is None
    assert limits["codex_weekly_remaining_pct"] is None

    listed = await auth_client.get("/api/accounts")
    assert listed.status_code == 200
    listed_account = next(
        item for item in listed.json() if item["id"] == account_id
    )
    assert listed_account["limits"]["codex_primary_remaining_pct"] == 93
    assert listed_account["limits"]["codex_primary_window_seconds"] == 2592000


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
    resp = await auth_client.patch(
        f"/api/accounts/{acc_id}", json={"status": "maintenance"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "maintenance"

    resp = await auth_client.patch(
        f"/api/accounts/{acc_id}", json={"status": "active"}
    )
    assert resp.status_code == 422
    account = await session.get(Account, acc_id)
    await session.refresh(account)
    assert account.status == "maintenance"
    assert account.operator_status_override == "maintenance"


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
    account.operator_status_override = "maintenance"
    job.status = "failed"
    job.error = '{"stage":"login","code":"cloudflare_challenge","detail":"blocked"}'
    await session.commit()

    response = await auth_client.post(f"/api/accounts/{account.id}/recheck")
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "pending_validation"
    assert payload["validation_job"]["status"] == "pending"
    assert payload["validation_job"]["priority"] == "manual"
    await session.refresh(account)
    assert account.operator_status_override is None


async def test_recheck_rejects_live_validation_without_mutating_account(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    created = await auth_client.post(
        "/api/accounts",
        json={
            "login": "running-account",
            "password": "pass",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        },
    )
    account = await session.get(Account, created.json()["id"])
    job = (
        await session.execute(
            select(AccountCheckJob).where(AccountCheckJob.account_id == account.id)
        )
    ).scalar_one()
    account.status = "active"
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await session.commit()

    response = await auth_client.post(f"/api/accounts/{account.id}/recheck")

    assert response.status_code == 409
    assert account.status == "active"
    assert job.status == "running"


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


async def test_patch_account_credentials_is_write_only_and_requeues_validation(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    created = await auth_client.post(
        "/api/accounts",
        json={
            "login": "old-login@example.com",
            "password": "old-chat-password",
            "totp_secret": "OLDTOTPSECRET",
            "email": "old-mail@hotmail.com",
            "email_password": "old-mail-password",
        },
    )
    account_id = created.json()["id"]
    account = await session.get(Account, account_id)
    account.operator_status_override = "maintenance"
    session.add(
        EmailOAuthCredential(
            account_id=account_id,
            email="old-mail@hotmail.com",
            external_subject="subject-1",
            refresh_token_encrypted="graph-refresh-token",
            scopes="Mail.Read offline_access",
        )
    )
    await session.commit()

    payload = {
        "login": "new-login@example.com",
        "password": "new-chat-password",
        "totp_secret": "NEWTOTPSECRET",
        "email": "new-mail@hotmail.com",
        "email_password": "new-mail-password",
    }
    response = await auth_client.patch(
        f"/api/accounts/{account_id}/credentials",
        json=payload,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["login"] == payload["login"]
    assert body["email"] == payload["email"]
    assert body["status"] == "pending_validation"
    assert body["operator_status_override"] is None
    assert body["email_oauth_connected"] is False
    serialized = str(body)
    for secret in (
        payload["password"],
        payload["totp_secret"],
        payload["email_password"],
        "graph-refresh-token",
    ):
        assert secret not in serialized

    await session.refresh(account)
    assert account.login == payload["login"]
    assert account.password_encrypted == payload["password"]
    assert account.totp_secret_encrypted == payload["totp_secret"]
    assert account.email == payload["email"]
    assert account.email_password_encrypted == payload["email_password"]
    assert account.validation_rerun_requested is False
    assert await session.get(EmailOAuthCredential, account_id) is None
    raw = (
        await session.execute(
            text(
                "SELECT password_encrypted, totp_secret_encrypted, "
                "email_password_encrypted FROM accounts WHERE id=:id"
            ),
            {"id": account_id},
        )
    ).one()
    assert raw.password_encrypted != payload["password"]
    assert raw.totp_secret_encrypted != payload["totp_secret"]
    assert raw.email_password_encrypted != payload["email_password"]

    jobs = list(
        (
            await session.execute(
                select(AccountCheckJob)
                .where(AccountCheckJob.account_id == account_id)
                .order_by(AccountCheckJob.id)
            )
        ).scalars()
    )
    assert [(job.status, job.priority, job.job_type) for job in jobs] == [
        ("done", "new", "full_validation"),
        ("pending", "manual", "full_validation"),
    ]
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "account_credentials_updated"
            )
        )
    ).scalar_one()
    assert audit.metadata_["changed_fields"] == [
        "email",
        "email_oauth",
        "email_password",
        "login",
        "password",
        "totp_secret",
    ]
    assert audit.metadata_["rerun_requested"] is False
    assert audit.message_text is None
    audit_text = str(audit.metadata_)
    assert all(secret not in audit_text for secret in payload.values())


async def test_patch_account_credentials_requires_a_safe_explicit_change(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    created = await auth_client.post(
        "/api/accounts",
        json={"login": "safe-patch", "password": "unchanged-password"},
    )
    account_id = created.json()["id"]

    assert (
        await auth_client.patch(
            f"/api/accounts/{account_id}/credentials", json={}
        )
    ).status_code == 422
    assert (
        await auth_client.patch(
            f"/api/accounts/{account_id}/credentials",
            json={"password": None},
        )
    ).status_code == 422
    assert (
        await auth_client.patch(
            f"/api/accounts/{account_id}/credentials",
            json={"totp_secret": ""},
        )
    ).status_code == 422
    mailbox_secret = "must-never-be-echoed"
    missing_email = await auth_client.patch(
        f"/api/accounts/{account_id}/credentials",
        json={"email_password": mailbox_secret},
    )
    assert missing_email.status_code == 422
    assert mailbox_secret not in missing_email.text
    oversized_secret = "x" * 4097
    oversized = await auth_client.patch(
        f"/api/accounts/{account_id}/credentials",
        json={"password": oversized_secret},
    )
    assert oversized.status_code == 422
    assert oversized_secret not in oversized.text

    account = await session.get(Account, account_id)
    await session.refresh(account)
    assert account.login == "safe-patch"
    assert account.password_encrypted == "unchanged-password"


async def test_patch_account_credentials_uses_null_only_for_optional_clears(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    created = await auth_client.post(
        "/api/accounts",
        json={
            "login": "clear-credentials",
            "password": "password",
            "totp_secret": "TOTPTOCLEAR",
            "email": "clear@example.com",
            "email_password": "mail-to-clear",
        },
    )
    account_id = created.json()["id"]

    response = await auth_client.patch(
        f"/api/accounts/{account_id}/credentials",
        json={"totp_secret": None, "email": None},
    )

    assert response.status_code == 202
    assert response.json()["email"] is None
    account = await session.get(Account, account_id)
    await session.refresh(account)
    assert account.totp_secret_encrypted == ""
    assert account.email is None
    assert account.email_password_encrypted is None


async def test_patch_account_credentials_duplicate_login_rolls_back_all_changes(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    first = await auth_client.post(
        "/api/accounts",
        json={"login": "credential-first", "password": "first-password"},
    )
    await auth_client.post(
        "/api/accounts",
        json={"login": "credential-second", "password": "second-password"},
    )
    first_id = first.json()["id"]

    response = await auth_client.patch(
        f"/api/accounts/{first_id}/credentials",
        json={
            "login": "credential-second",
            "password": "must-not-be-partially-written",
        },
    )

    assert response.status_code == 409
    account = await session.get(Account, first_id)
    await session.refresh(account)
    assert account.login == "credential-first"
    assert account.password_encrypted == "first-password"
    jobs = list(
        (
            await session.execute(
                select(AccountCheckJob).where(
                    AccountCheckJob.account_id == first_id
                )
            )
        ).scalars()
    )
    assert [(job.status, job.priority) for job in jobs] == [("pending", "new")]


async def test_patch_account_credentials_during_running_job_requests_followup(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    created = await auth_client.post(
        "/api/accounts",
        json={"login": "credential-race", "password": "old-password"},
    )
    account_id = created.json()["id"]
    account = await session.get(Account, account_id)
    job = (
        await session.execute(
            select(AccountCheckJob).where(
                AccountCheckJob.account_id == account_id
            )
        )
    ).scalar_one()
    account.status = "active"
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await session.commit()

    response = await auth_client.patch(
        f"/api/accounts/{account_id}/credentials",
        json={"password": "new-password"},
    )

    assert response.status_code == 202
    assert response.json()["validation_job"]["id"] == job.id
    assert response.json()["validation_job"]["status"] == "running"
    await session.refresh(account)
    await session.refresh(job)
    assert account.password_encrypted == "new-password"
    assert account.status == "pending_validation"
    assert account.validation_rerun_requested is True
    assert job.status == "running"
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "account_credentials_updated"
            )
        )
    ).scalar_one()
    assert audit.metadata_["changed_fields"] == ["password"]
    assert audit.metadata_["rerun_requested"] is True
    assert "new-password" not in str(audit.metadata_)


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


async def test_totp_code_is_not_cacheable_hides_secret_and_is_audited(
    auth_client: AsyncClient, session: AsyncSession
):
    tier_id = await _seed_tier(session)
    created = await auth_client.post(
        "/api/accounts",
        json={
            "login": "totp-code-audit",
            "password": "pass",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "tier_id": tier_id,
        },
    )

    response = await auth_client.get(
        f"/api/accounts/{created.json()['id']}/totp-code"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    payload = response.json()
    assert payload["code"].isdigit()
    assert len(payload["code"]) == 6
    assert 1 <= payload["seconds_remaining"] <= 30
    assert "secret" not in payload
    audit = (
        await session.execute(
            select(AuditLog).where(AuditLog.event_type == "totp_code_generated")
        )
    ).scalar_one()
    assert audit.account_id == created.json()["id"]
    assert audit.metadata_ == {"actor": "admin"}
    assert audit.message_text is None


async def test_totp_code_waits_for_a_safe_window_at_boundary(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    created = await auth_client.post(
        "/api/accounts",
        json={
            "login": "totp-window-boundary",
            "password": "pass",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        },
    )
    timestamps = iter((1_750_000_019.0, 1_750_000_020.1))
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(accounts_router, "_now", lambda: next(timestamps))
    monkeypatch.setattr(accounts_router, "_sleep", fake_sleep)

    response = await auth_client.get(
        f"/api/accounts/{created.json()['id']}/totp-code"
    )

    assert response.status_code == 200
    assert sleeps == pytest.approx([1.05])
    assert response.json()["seconds_remaining"] == 28


async def test_account_list_reports_active_rentals_count(
    auth_client: AsyncClient, session: AsyncSession
):
    tier = SubscriptionTier(code="plus", name="Plus", is_active=True)
    duration = Duration(days=7, is_enabled=True, sort_order=10)
    scope = LimitScope(code="any", name="Any")
    session.add_all([tier, duration, scope])
    await session.flush()
    account = Account(
        login="rental-count@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="active",
    )
    session.add(account)
    await session.flush()
    now = datetime.now(timezone.utc)
    for index, status in enumerate(("active", "expired"), start=1):
        order = Order(
            funpay_order_id=f"count-order-{index}",
            funpay_chat_id=f"chat-{index}",
            buyer_funpay_id=f"buyer-{index}",
            tier_id=tier.id,
            duration_id=duration.id,
            limit_scope_id=scope.id,
            price=100,
            status="completed",
        )
        session.add(order)
        await session.flush()
        session.add(
            Rental(
                order_id=order.id,
                account_id=account.id,
                buyer_funpay_id=f"buyer-{index}",
                buyer_funpay_chat_id=f"chat-{index}",
                tier_id=tier.id,
                duration_id=duration.id,
                limit_scope_id=scope.id,
                lang="ru",
                started_at=now,
                expires_at=now + timedelta(days=7),
                status=status,
                credentials_delivery_status="sent",
                credentials_delivery_template="welcome",
                credentials_delivery_attempts=1,
            )
        )
    await session.commit()

    response = await auth_client.get("/api/accounts")

    assert response.status_code == 200
    listed = next(item for item in response.json() if item["id"] == account.id)
    assert listed["active_rentals_count"] == 1

    updated = await auth_client.patch(
        f"/api/accounts/{account.id}", json={"notes": "capacity stays exact"},
    )
    assert updated.status_code == 200
    assert updated.json()["active_rentals_count"] == 1

    rechecked = await auth_client.post(f"/api/accounts/{account.id}/recheck")
    assert rechecked.status_code == 202
    assert rechecked.json()["active_rentals_count"] == 1
