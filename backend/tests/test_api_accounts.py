import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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


async def _seed_browser_confirmation_evidence(
    session: AsyncSession,
    *,
    login: str = "manual-browser@example.com",
    password: str = "stored-password",
    totp_secret: str = "JBSWY3DPEHPK3PXP",
    intermediate_full_job: bool = False,
) -> tuple[Account, AccountLimits, AccountCheckJob, AccountCheckJob]:
    now = datetime.now(timezone.utc)
    tier_id = await _seed_tier(session)
    account = Account(
        login=login,
        password_encrypted=password,
        totp_secret_encrypted=totp_secret,
        tier_id=tier_id,
        plan_raw_type="plus",
        plan_source="id_token",
        plan_confidence=1.0,
        plan_detected_at=now - timedelta(minutes=2),
        status="validation_failed",
    )
    session.add(account)
    await session.flush()
    limits = AccountLimits(
        account_id=account.id,
        refresh_token_encrypted="refresh-token",
        access_token_encrypted="access-token",
        plan_type="plus",
        plan_window_status="ok",
        expected_long_window_seconds=7 * 24 * 60 * 60,
        codex_primary_remaining_pct=79,
        codex_primary_window_seconds=7 * 24 * 60 * 60,
        measured_at=now - timedelta(minutes=2),
        refresh_status="ok",
    )
    device_job = AccountCheckJob(
        account_id=account.id,
        priority="manual",
        job_type="device_auth",
        status="done",
        result="tokens_connected",
        created_at=now - timedelta(minutes=5),
        started_at=now - timedelta(minutes=5),
        finished_at=now - timedelta(minutes=4),
    )
    session.add_all([limits, device_job])
    await session.flush()
    if intermediate_full_job:
        session.add(
            AccountCheckJob(
                account_id=account.id,
                priority="scheduled",
                job_type="full_validation",
                status="done",
                result="ok",
                created_at=now - timedelta(minutes=3),
                started_at=now - timedelta(minutes=3),
                finished_at=now - timedelta(minutes=2, seconds=30),
            )
        )
        await session.flush()
    validation_job = AccountCheckJob(
        account_id=account.id,
        priority="manual",
        job_type="full_validation",
        status="failed",
        created_at=now - timedelta(minutes=4),
        started_at=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=1),
        error=json.dumps(
            {
                "stage": "login",
                "code": "cloudflare_challenge",
                "detail": "Cloudflare blocked the server browser.",
            }
        ),
    )
    session.add(validation_job)
    await session.flush()
    session.add(
        AuditLog(
            event_type="account_device_auth_completed",
            account_id=account.id,
            timestamp=now - timedelta(minutes=3, seconds=59),
            metadata_={
                "actor": "admin",
                "job_id": device_job.id,
                "credential_validation_job_id": validation_job.id,
            },
        )
    )
    await session.commit()
    return account, limits, device_job, validation_job


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


async def test_create_account_rejects_manual_subscription_expiry(
    auth_client: AsyncClient,
):
    response = await auth_client.post(
        "/api/accounts",
        json={
            "login": "manual-expiry@example.com",
            "password": "password",
            "subscription_expires_at": "2026-08-01T00:00:00Z",
        },
    )
    assert response.status_code == 422
    assert "measured automatically" in response.text


async def test_account_capacity_above_one_is_rejected(
    auth_client: AsyncClient,
):
    created = await auth_client.post(
        "/api/accounts",
        json={
            "login": "unsafe-capacity",
            "password": "pass",
            "max_active_rentals": 2,
        },
    )
    assert created.status_code == 422


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
    lifecycle = MagicMock()
    app.state.lifecycle = lifecycle
    try:
        resp = await auth_client.patch(
            f"/api/accounts/{acc_id}", json={"status": "maintenance"}
        )
    finally:
        del app.state.lifecycle
    assert resp.status_code == 200
    assert resp.json()["status"] == "maintenance"
    lifecycle.request_capacity_reconcile.assert_called_once_with()

    resp = await auth_client.patch(
        f"/api/accounts/{acc_id}", json={"status": "active"}
    )
    assert resp.status_code == 422
    account = await session.get(Account, acc_id)
    await session.refresh(account)
    assert account.status == "maintenance"
    assert account.operator_status_override == "maintenance"


async def test_capacity_callback_failure_does_not_undo_committed_status(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    created = await auth_client.post(
        "/api/accounts",
        json={"login": "callback-failure", "password": "pass"},
    )
    account_id = created.json()["id"]
    lifecycle = MagicMock()
    lifecycle.request_capacity_reconcile.side_effect = RuntimeError("offline")
    app.state.lifecycle = lifecycle
    try:
        response = await auth_client.patch(
            f"/api/accounts/{account_id}",
            json={"status": "maintenance"},
        )
    finally:
        del app.state.lifecycle

    assert response.status_code == 200
    account = await session.get(Account, account_id)
    await session.refresh(account)
    assert account.status == "maintenance"


async def test_validation_enqueues_wake_scheduler_without_false_http_failure(
    auth_client: AsyncClient,
):
    lifecycle = MagicMock()
    lifecycle.request_validation_check.side_effect = RuntimeError("offline")
    app.state.lifecycle = lifecycle
    try:
        created = await auth_client.post(
            "/api/accounts",
            json={"login": "wake-validation", "password": "password"},
        )
        assert created.status_code == 201
        lifecycle.request_validation_check.assert_called_once_with()

        lifecycle.reset_mock()
        repaired = await auth_client.patch(
            f"/api/accounts/{created.json()['id']}/credentials",
            json={"password": "replacement-password"},
        )
        assert repaired.status_code == 202
        lifecycle.request_validation_check.assert_called_once_with()
    finally:
        del app.state.lifecycle


async def test_device_auth_start_requests_capacity_reconcile(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    account = Account(
        login="device-auth-capacity@example.com",
        password_encrypted="pass",
        totp_secret_encrypted="",
        status="active",
    )
    session.add(account)
    await session.commit()
    auth_session = SimpleNamespace(
        id="device-session",
        code=SimpleNamespace(
            verification_url="https://example.com/device",
            user_code="ABCD-EFGH",
            interval_seconds=5,
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    monkeypatch.setattr(
        accounts_router.account_device_auth_manager,
        "start",
        AsyncMock(return_value=auth_session),
    )
    lifecycle = MagicMock()
    app.state.lifecycle = lifecycle
    try:
        response = await auth_client.post(f"/api/accounts/{account.id}/device-auth")
    finally:
        del app.state.lifecycle

    assert response.status_code == 201
    lifecycle.request_capacity_reconcile.assert_called_once_with()


async def test_manual_browser_confirmation_activates_only_attested_account(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    account, _limits, device_job, validation_job = (
        await _seed_browser_confirmation_evidence(session)
    )
    lifecycle = MagicMock()
    lifecycle.request_capacity_reconcile.side_effect = RuntimeError("offline")
    app.state.lifecycle = lifecycle
    try:
        response = await auth_client.post(
            f"/api/accounts/{account.id}/confirm-browser-validation"
        )
    finally:
        del app.state.lifecycle

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["validation_job"]["id"] == validation_job.id
    lifecycle.request_capacity_reconcile.assert_called_once_with()
    await session.refresh(account)
    assert account.status == "active"
    assert account.chatgpt_last_check_at is not None
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "account_browser_validation_confirmed"
            )
        )
    ).scalar_one()
    assert audit.account_id == account.id
    assert audit.metadata_ == {
        "actor": "admin",
        "device_auth_job_id": device_job.id,
        "full_validation_job_id": validation_job.id,
    }


@pytest.mark.parametrize(
    ("password", "totp_secret"),
    [
        ("", "JBSWY3DPEHPK3PXP"),
        ("stored-password", "not-a-base32-secret!"),
    ],
)
async def test_manual_browser_confirmation_rejects_invalid_stored_credentials(
    auth_client: AsyncClient,
    session: AsyncSession,
    password: str,
    totp_secret: str,
):
    account, *_rest = await _seed_browser_confirmation_evidence(
        session,
        password=password,
        totp_secret=totp_secret,
    )

    response = await auth_client.post(
        f"/api/accounts/{account.id}/confirm-browser-validation"
    )

    assert response.status_code == 422
    await session.refresh(account)
    assert account.status == "validation_failed"


@pytest.mark.parametrize(
    "invalid_evidence",
    [
        "wrong_error",
        "stale_device",
        "stale_limits",
        "window_mismatch",
        "missing_tier",
        "operator_override",
        "unlinked_device_auth",
    ],
)
async def test_manual_browser_confirmation_rejects_incomplete_evidence(
    auth_client: AsyncClient,
    session: AsyncSession,
    invalid_evidence: str,
):
    account, limits, device_job, validation_job = (
        await _seed_browser_confirmation_evidence(
            session,
            login=f"{invalid_evidence}@example.com",
        )
    )
    if invalid_evidence == "wrong_error":
        validation_job.error = json.dumps(
            {"stage": "login", "code": "invalid_credentials", "detail": "bad"}
        )
    elif invalid_evidence == "stale_device":
        device_job.finished_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    elif invalid_evidence == "stale_limits":
        limits.measured_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    elif invalid_evidence == "window_mismatch":
        limits.plan_window_status = "mismatch"
    elif invalid_evidence == "missing_tier":
        account.tier_id = None
    elif invalid_evidence == "operator_override":
        account.operator_status_override = "maintenance"
    else:
        completion_audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.account_id == account.id,
                    AuditLog.event_type == "account_device_auth_completed",
                )
            )
        ).scalar_one()
        completion_audit.metadata_ = {
            **completion_audit.metadata_,
            "credential_validation_job_id": validation_job.id + 1,
        }
    await session.commit()

    response = await auth_client.post(
        f"/api/accounts/{account.id}/confirm-browser-validation"
    )

    assert response.status_code == 409
    await session.refresh(account)
    assert account.status == "validation_failed"


async def test_manual_browser_confirmation_rejects_busy_account(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    account, *_rest = await _seed_browser_confirmation_evidence(session)
    monkeypatch.setattr(
        accounts_router,
        "account_is_busy",
        AsyncMock(return_value=True),
    )

    response = await auth_client.post(
        f"/api/accounts/{account.id}/confirm-browser-validation"
    )

    assert response.status_code == 409
    await session.refresh(account)
    assert account.status == "validation_failed"


async def test_manual_browser_confirmation_rejects_intermediate_validation_job(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    account, *_rest = await _seed_browser_confirmation_evidence(
        session,
        intermediate_full_job=True,
    )

    response = await auth_client.post(
        f"/api/accounts/{account.id}/confirm-browser-validation"
    )

    assert response.status_code == 409
    await session.refresh(account)
    assert account.status == "validation_failed"


async def test_manual_browser_confirmation_rejects_credentials_changed_after_device_auth(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    account, *_rest = await _seed_browser_confirmation_evidence(session)

    changed = await auth_client.patch(
        f"/api/accounts/{account.id}/credentials",
        json={"password": "replacement-password"},
    )
    assert changed.status_code == 202
    replacement_job = await session.get(
        AccountCheckJob,
        changed.json()["validation_job"]["id"],
    )
    now = datetime.now(timezone.utc)
    replacement_job.status = "failed"
    replacement_job.started_at = now - timedelta(seconds=20)
    replacement_job.finished_at = now - timedelta(seconds=10)
    replacement_job.error = json.dumps(
        {
            "stage": "login",
            "code": "cloudflare_challenge",
            "detail": "Cloudflare blocked the server browser.",
        }
    )
    account.status = "validation_failed"
    await session.commit()

    response = await auth_client.post(
        f"/api/accounts/{account.id}/confirm-browser-validation"
    )

    assert response.status_code == 409
    await session.refresh(account)
    assert account.status == "validation_failed"


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

    lifecycle = MagicMock()
    app.state.lifecycle = lifecycle
    try:
        response = await auth_client.post(f"/api/accounts/{account.id}/recheck")
    finally:
        del app.state.lifecycle
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "pending_validation"
    assert payload["validation_job"]["status"] == "pending"
    assert payload["validation_job"]["priority"] == "manual"
    await session.refresh(account)
    assert account.operator_status_override is None
    lifecycle.request_validation_check.assert_called_once_with()
    lifecycle.request_capacity_reconcile.assert_called_once_with()


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


async def test_account_list_reports_occupying_rentals_count(
    auth_client: AsyncClient, session: AsyncSession
):
    tier = SubscriptionTier(code="plus", name="Plus", is_active=True)
    duration = Duration(minutes=7 * 24 * 60, is_enabled=True, sort_order=10)
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
    for index, status in enumerate(
        ("expiry_pending", "expired"), start=1
    ):
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
    assert rechecked.status_code == 409
    await session.refresh(account)
    assert account.status == "active"

    rentals = (
        await session.execute(
            select(Rental).where(Rental.account_id == account.id)
        )
    ).scalars().all()
    blocked_delete = await auth_client.delete(f"/api/accounts/{account.id}")
    assert blocked_delete.status_code == 409


async def test_replacement_target_is_mutation_locked_but_not_counted_as_sold(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    tier = SubscriptionTier(code="plus", name="Plus", is_active=True)
    duration = Duration(minutes=60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="any", name="Any")
    session.add_all([tier, duration, scope])
    await session.flush()
    old_account = Account(
        login="old-rented@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        tier_id=tier.id,
        status="maintenance",
    )
    target = Account(
        login="reserved-target@outlook.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        email="reserved-target@outlook.com",
        tier_id=tier.id,
        status="active",
    )
    session.add_all([old_account, target])
    await session.flush()
    now = datetime.now(timezone.utc)
    order = Order(
        funpay_order_id="replacement-reservation-order",
        funpay_chat_id="replacement-chat",
        buyer_funpay_id="replacement-buyer",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        status="completed",
    )
    session.add(order)
    await session.flush()
    session.add(Rental(
        order_id=order.id,
        account_id=old_account.id,
        replacement_target_account_id=target.id,
        buyer_funpay_id="replacement-buyer",
        buyer_funpay_chat_id="replacement-chat",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        lang="ru",
        started_at=now,
        expires_at=now + timedelta(hours=1),
        status="active",
        expiry_revoke_started_at=now,
        credentials_delivery_status="sent",
        credentials_delivery_template="welcome",
        credentials_delivery_attempts=1,
    ))
    await session.commit()

    listed_response = await auth_client.get("/api/accounts")
    assert listed_response.status_code == 200
    listed = next(
        item for item in listed_response.json() if item["id"] == target.id
    )
    assert listed["active_rentals_count"] == 0
    assert listed["replacement_reserved"] is True

    notes_update = await auth_client.patch(
        f"/api/accounts/{target.id}",
        json={"notes": "safe while reserved"},
    )
    assert notes_update.status_code == 200
    assert notes_update.json()["replacement_reserved"] is True

    blocked_requests = [
        await auth_client.patch(
            f"/api/accounts/{target.id}", json={"status": "maintenance"},
        ),
        await auth_client.patch(
            f"/api/accounts/{target.id}",
            json={"subscription_expires_at": (now - timedelta(days=1)).isoformat()},
        ),
        await auth_client.patch(
            f"/api/accounts/{target.id}/credentials",
            json={"password": "new-password"},
        ),
        await auth_client.post(f"/api/accounts/{target.id}/recheck"),
        await auth_client.post(f"/api/accounts/{target.id}/device-auth"),
        await auth_client.post(
            f"/api/accounts/{target.id}/email-oauth/microsoft"
        ),
        await auth_client.delete(f"/api/accounts/{target.id}"),
    ]
    assert [response.status_code for response in blocked_requests] == [
        409, 422, 409, 409, 409, 409, 409,
    ]
