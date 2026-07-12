import base64
import json
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openai.device_auth import DeviceAuthorization, DeviceCode
from app.integrations.openai.oauth import RefreshedTokens
from app.models.account import Account, AccountCheckJob
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
