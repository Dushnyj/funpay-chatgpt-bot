import base64
import json
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openai.device_auth import DeviceAuthorization, DeviceCode
from app.integrations.openai.oauth import RefreshedTokens
from app.models.account import Account, AccountCheckJob
from app.check_job_queue import CheckJobQueue
from app.services.account_device_auth import AccountDeviceAuthManager


def _id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


async def _account(session: AsyncSession) -> Account:
    account = Account(
        login="owner@example.com",
        password_encrypted="password",
        totp_secret_encrypted="",
        tier_id=None,
    )
    session.add(account)
    await session.commit()
    return account


async def test_device_auth_start_and_pending_poll(
    session: AsyncSession,
    monkeypatch,
):
    account = await _account(session)
    manager = AccountDeviceAuthManager()

    async def fake_request():
        return DeviceCode("device", "ABCD-EFGH", 3)

    async def fake_poll(device_auth_id: str, user_code: str):
        assert (device_auth_id, user_code) == ("device", "ABCD-EFGH")
        return None

    monkeypatch.setattr("app.services.account_device_auth.request_device_code", fake_request)
    monkeypatch.setattr("app.services.account_device_auth.poll_device_authorization", fake_poll)

    auth_session = await manager.start(session, account)
    assert auth_session.status == "pending"
    job = await session.get(AccountCheckJob, auth_session.job_id)
    assert job is not None and job.status == "pending" and job.job_type == "device_auth"

    await manager.poll(session, account, auth_session.id)
    await session.refresh(job)
    assert job.status == "running"
    assert account.status == "pending_validation"


async def test_device_auth_success_verifies_identity(
    session: AsyncSession,
    monkeypatch,
):
    account = await _account(session)
    manager = AccountDeviceAuthManager()

    async def fake_request():
        return DeviceCode("device", "ABCD-EFGH", 1)

    async def fake_poll(*_args):
        return DeviceAuthorization("code", "verifier", "challenge")

    async def fake_exchange(_authorization):
        return RefreshedTokens("access", "refresh", _id_token("OWNER@example.com"))

    async def fake_save(_session, target: Account, _tokens):
        target.status = "active"

    monkeypatch.setattr("app.services.account_device_auth.request_device_code", fake_request)
    monkeypatch.setattr("app.services.account_device_auth.poll_device_authorization", fake_poll)
    monkeypatch.setattr("app.services.account_device_auth.exchange_device_authorization", fake_exchange)
    monkeypatch.setattr("app.services.account_device_auth._save_tokens_and_measure", fake_save)

    auth_session = await manager.start(session, account)
    auth_session.next_poll_at = datetime.now(timezone.utc)
    result = await manager.poll(session, account, auth_session.id)
    assert result.status == "completed"
    job = await session.get(AccountCheckJob, result.job_id)
    assert job is not None and job.status == "done" and job.result == "ok"
    assert account.status == "active"


async def test_device_auth_rejects_another_openai_account(
    session: AsyncSession,
    monkeypatch,
):
    account = await _account(session)
    manager = AccountDeviceAuthManager()

    async def fake_request():
        return DeviceCode("device", "ABCD-EFGH", 1)

    async def fake_poll(*_args):
        return DeviceAuthorization("code", "verifier", "challenge")

    async def fake_exchange(_authorization):
        return RefreshedTokens("access", "refresh", _id_token("other@example.com"))

    monkeypatch.setattr("app.services.account_device_auth.request_device_code", fake_request)
    monkeypatch.setattr("app.services.account_device_auth.poll_device_authorization", fake_poll)
    monkeypatch.setattr("app.services.account_device_auth.exchange_device_authorization", fake_exchange)

    auth_session = await manager.start(session, account)
    auth_session.next_poll_at = datetime.now(timezone.utc)
    result = await manager.poll(session, account, auth_session.id)
    assert result.status == "failed"
    assert result.error_code == "invalid_credentials"
    assert account.status == "validation_failed"


async def test_device_auth_supersedes_pending_full_validation_and_previous_device_job(
    session: AsyncSession,
    monkeypatch,
):
    account = await _account(session)
    queue = CheckJobQueue()
    full_validation = await queue.enqueue(
        session,
        account.id,
        priority="new",
        job_type="full_validation",
    )
    await session.commit()
    manager = AccountDeviceAuthManager()

    counter = 0

    async def fake_request():
        nonlocal counter
        counter += 1
        return DeviceCode(f"device-{counter}", f"CODE-{counter}", 1)

    monkeypatch.setattr(
        "app.services.account_device_auth.request_device_code", fake_request
    )

    first = await manager.start(session, account)
    first_job = await session.get(AccountCheckJob, first.job_id)
    second = await manager.start(session, account)
    second_job = await session.get(AccountCheckJob, second.job_id)

    await session.refresh(full_validation)
    await session.refresh(first_job)
    assert full_validation.status == "done"
    assert full_validation.result == "superseded:device_auth"
    assert first.status == "expired"
    assert first_job.status == "done"
    assert first_job.result == "superseded:device_auth"
    assert second_job.status == "pending"


async def test_superseded_device_auth_session_cannot_poll(
    session: AsyncSession,
    monkeypatch,
):
    account = await _account(session)
    manager = AccountDeviceAuthManager()

    async def fake_request():
        return DeviceCode("device", "ABCD-EFGH", 1)

    poll_called = False

    async def fake_poll(*_args):
        nonlocal poll_called
        poll_called = True
        return None

    monkeypatch.setattr(
        "app.services.account_device_auth.request_device_code", fake_request
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.poll_device_authorization", fake_poll
    )

    auth_session = await manager.start(session, account)
    auth_session.next_poll_at = datetime.now(timezone.utc)
    replacement = await manager._queue.enqueue_exclusive(
        session,
        account.id,
        priority="manual",
        job_type="full_validation",
        superseded_by="manual_recheck",
    )
    await session.commit()

    result = await manager.poll(session, account, auth_session.id)

    assert result.status == "expired"
    assert result.code is None
    assert poll_called is False
    assert replacement.status == "pending"
