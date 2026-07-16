import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.openai.device_auth import DeviceAuthorization, DeviceCode
from app.integrations.openai.oauth import RefreshedTokens
from app.integrations.playwright.proxy import BrowserProxy
from app.models.account import Account, AccountCheckJob
from app.models.audit import AuditLog
from app.check_job_queue import ActiveJobConflict, CheckJobQueue
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
    validation_wakes = 0

    def validation_queued():
        nonlocal validation_wakes
        validation_wakes += 1

    manager = AccountDeviceAuthManager(validation_queued=validation_queued)

    async def fake_request():
        return DeviceCode("device", "ABCD-EFGH", 1)

    async def fake_poll(*_args):
        return DeviceAuthorization("code", "verifier", "challenge")

    async def fake_exchange(_authorization):
        return RefreshedTokens("access", "refresh", _id_token("OWNER@example.com"))

    async def fake_save(
        _session, target: Account, _tokens, *, browser_proxy=None
    ):
        assert browser_proxy is None
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
    assert (
        job is not None
        and job.status == "done"
        and job.result == "tokens_connected"
    )
    assert account.status == "pending_validation"
    credential_jobs = list(
        (
            await session.execute(
                select(AccountCheckJob).where(
                    AccountCheckJob.account_id == account.id,
                    AccountCheckJob.job_type == "full_validation",
                )
            )
        ).scalars()
    )
    assert len(credential_jobs) == 1
    assert credential_jobs[0].status == "pending"
    assert validation_wakes == 1


async def test_device_auth_manager_routes_request_poll_and_exchange(
    session: AsyncSession,
    monkeypatch,
):
    account = await _account(session)
    manager = AccountDeviceAuthManager()
    selected = BrowserProxy(
        41,
        "socks5",
        "home-relay",
        1080,
        username="relay-user",
        password="relay-password",
        config_revision=7,
    )
    resolved_for: list[int] = []
    request_routes: list[BrowserProxy | None] = []
    poll_routes: list[BrowserProxy | None] = []
    exchange_routes: list[BrowserProxy | None] = []
    pinned_checks: list[tuple[BrowserProxy | None, bool]] = []

    async def fake_resolve(_session, target):
        resolved_for.append(target.id)
        return selected

    async def fake_request(*, proxy=None):
        request_routes.append(proxy)
        return DeviceCode("device", "ABCD-EFGH", 1)

    async def fake_poll(*_args, proxy=None):
        poll_routes.append(proxy)
        return DeviceAuthorization("code", "verifier", "challenge")

    async def fake_exchange(_authorization, *, proxy=None):
        exchange_routes.append(proxy)
        return RefreshedTokens("access", "refresh", _id_token("owner@example.com"))

    async def fake_save(
        _session, target: Account, _tokens, *, browser_proxy=None
    ):
        assert browser_proxy is selected
        target.status = "active"

    async def fake_assert_proxy(
        _session,
        _target,
        expected,
        *,
        lock_account=False,
    ):
        pinned_checks.append((expected, lock_account))

    monkeypatch.setattr(
        "app.services.account_device_auth.resolve_browser_proxy", fake_resolve
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.request_device_code", fake_request
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.poll_device_authorization", fake_poll
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.exchange_device_authorization",
        fake_exchange,
    )
    monkeypatch.setattr(
        "app.services.account_device_auth._save_tokens_and_measure", fake_save
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.assert_proxy_selection_unchanged",
        fake_assert_proxy,
    )

    auth_session = await manager.start(session, account)
    auth_session.next_poll_at = datetime.now(timezone.utc)
    result = await manager.poll(session, account, auth_session.id)

    assert result.status == "completed"
    assert resolved_for == [account.id, account.id, account.id]
    assert request_routes == [selected]
    assert poll_routes == [selected]
    assert exchange_routes == [selected]
    assert pinned_checks == [(selected, True), (selected, False)]
    assert result.browser_proxy is None

    assert result.terminal_at is not None
    result.terminal_at = datetime.now(timezone.utc) - timedelta(minutes=6)
    with pytest.raises(KeyError):
        await manager.poll(session, account, result.id)
    assert result.id not in manager._sessions


async def test_device_auth_fails_closed_if_route_changes_mid_session(
    session: AsyncSession,
    monkeypatch,
):
    account = await _account(session)
    manager = AccountDeviceAuthManager()
    route_a = BrowserProxy(51, "socks5", "home-a", 1080, config_revision=3)
    route_b = BrowserProxy(52, "socks5", "home-b", 1080, config_revision=1)
    resolutions = 0

    async def fake_resolve(_session, _target):
        nonlocal resolutions
        resolutions += 1
        return route_a if resolutions <= 2 else route_b

    async def fake_request(*, proxy=None):
        assert proxy is route_a
        return DeviceCode("device", "ABCD-EFGH", 1)

    async def unexpected_poll(*_args, **_kwargs):
        pytest.fail("a changed route must be rejected before polling OpenAI")

    monkeypatch.setattr(
        "app.services.account_device_auth.resolve_browser_proxy", fake_resolve
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.request_device_code", fake_request
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.poll_device_authorization",
        unexpected_poll,
    )

    auth_session = await manager.start(session, account)
    auth_session.next_poll_at = datetime.now(timezone.utc)
    result = await manager.poll(session, account, auth_session.id)

    assert result.status == "failed"
    assert result.error_code == "proxy_route_changed"
    assert result.browser_proxy is None
    assert resolutions == 3


async def test_device_auth_rechecks_pinned_route_after_exchange(
    session: AsyncSession,
    monkeypatch,
):
    from app.services.account_validation import (
        AccountValidationError,
        ValidationCode,
        ValidationStage,
    )

    account = await _account(session)
    manager = AccountDeviceAuthManager()
    checks = 0
    save_called = False

    async def fake_request(*, proxy=None):
        assert proxy is None
        return DeviceCode("device", "ABCD-EFGH", 1)

    async def fake_poll(*_args, proxy=None):
        assert proxy is None
        return DeviceAuthorization("code", "verifier", "challenge")

    async def fake_exchange(_authorization, *, proxy=None):
        assert proxy is None
        return RefreshedTokens("access", "refresh", _id_token(account.login))

    async def fake_assert(
        _session,
        target,
        _expected,
        *,
        lock_account=False,
    ):
        nonlocal checks
        checks += 1
        if checks == 2:
            target.validation_rerun_requested = True
            raise AccountValidationError(
                ValidationStage.PROXY,
                ValidationCode.PROXY_ROUTE_CHANGED,
                "route changed",
            )
        assert lock_account is True

    async def unexpected_save(*_args, **_kwargs):
        nonlocal save_called
        save_called = True

    monkeypatch.setattr(
        "app.services.account_device_auth.request_device_code", fake_request
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.poll_device_authorization", fake_poll
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.exchange_device_authorization",
        fake_exchange,
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.assert_proxy_selection_unchanged",
        fake_assert,
    )
    monkeypatch.setattr(
        "app.services.account_device_auth._save_tokens_and_measure",
        unexpected_save,
    )

    auth_session = await manager.start(session, account)
    auth_session.next_poll_at = datetime.now(timezone.utc)
    result = await manager.poll(session, account, auth_session.id)

    assert result.status == "failed"
    assert result.error_code == ValidationCode.PROXY_ROUTE_CHANGED.value
    assert checks == 2
    assert save_called is False
    jobs = list(
        (
            await session.execute(
                select(AccountCheckJob)
                .where(AccountCheckJob.account_id == account.id)
                .order_by(AccountCheckJob.id)
            )
        ).scalars()
    )
    assert [job.status for job in jobs] == ["failed", "pending"]
    assert jobs[-1].job_type == "full_validation"


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


async def test_device_auth_failure_hands_credential_update_to_followup_job(
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
    account.validation_rerun_requested = True
    await session.commit()
    auth_session.next_poll_at = datetime.now(timezone.utc)

    result = await manager.poll(session, account, auth_session.id)

    assert result.status == "failed"
    await session.refresh(account)
    assert account.status == "pending_validation"
    assert account.validation_rerun_requested is False
    jobs = list(
        (
            await session.execute(
                select(AccountCheckJob)
                .where(AccountCheckJob.account_id == account.id)
                .order_by(AccountCheckJob.id)
            )
        ).scalars()
    )
    assert [(job.status, job.job_type) for job in jobs] == [
        ("failed", "device_auth"),
        ("pending", "full_validation"),
    ]


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


async def test_device_auth_restart_supersedes_running_manager_owned_job(
    session: AsyncSession,
    monkeypatch,
):
    account = await _account(session)
    manager = AccountDeviceAuthManager()
    counter = 0

    async def fake_request():
        nonlocal counter
        counter += 1
        return DeviceCode(f"device-{counter}", f"CODE-{counter}", 1)

    async def fake_poll(*_args):
        return None

    monkeypatch.setattr(
        "app.services.account_device_auth.request_device_code", fake_request
    )
    monkeypatch.setattr(
        "app.services.account_device_auth.poll_device_authorization", fake_poll
    )

    first = await manager.start(session, account)
    first.next_poll_at = datetime.now(timezone.utc)
    await manager.poll(session, account, first.id)
    first_job = await session.get(AccountCheckJob, first.job_id)
    assert first_job is not None and first_job.status == "running"

    second = await manager.start(session, account)
    second_job = await session.get(AccountCheckJob, second.job_id)

    await session.refresh(first_job)
    assert first.status == "expired"
    assert first.error_code == "device_auth_restarted"
    assert first.code is None
    assert first_job.status == "done"
    assert first_job.result == "superseded:device_auth"
    assert second_job is not None and second_job.status == "pending"
    restart_audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "account_device_auth_restarted"
            )
        )
    ).scalar_one()
    assert restart_audit.account_id == account.id
    assert restart_audit.metadata_ == {"actor": "admin", "job_id": first_job.id}


async def test_device_auth_restart_does_not_supersede_another_running_job_type(
    session: AsyncSession,
    monkeypatch,
):
    account = await _account(session)
    queue = CheckJobQueue()
    validation_job = await queue.enqueue(
        session,
        account.id,
        priority="manual",
        job_type="full_validation",
    )
    await queue.mark_running(session, validation_job)
    await session.commit()
    manager = AccountDeviceAuthManager()

    async def fake_request():
        return DeviceCode("device", "ABCD-EFGH", 1)

    monkeypatch.setattr(
        "app.services.account_device_auth.request_device_code", fake_request
    )

    with pytest.raises(ActiveJobConflict) as conflict:
        await manager.start(session, account)

    assert conflict.value.job_id == validation_job.id
    assert conflict.value.job_type == "full_validation"
    await session.refresh(validation_job)
    assert validation_job.status == "running"


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


async def test_background_poll_completes_without_frontend_status_requests(
    session: AsyncSession,
    test_engine,
    monkeypatch,
):
    account = await _account(session)
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    manager = AccountDeviceAuthManager(session_factory=session_factory)
    completed = asyncio.Event()

    async def fake_request():
        return DeviceCode("device", "ABCD-EFGH", 1)

    async def fake_poll(*_args):
        return DeviceAuthorization("code", "verifier", "challenge")

    async def fake_exchange(_authorization):
        return RefreshedTokens("access", "refresh", _id_token("owner@example.com"))

    async def fake_save(
        _session, target: Account, _tokens, *, browser_proxy=None
    ):
        assert browser_proxy is None
        target.status = "active"
        completed.set()

    monkeypatch.setattr("app.services.account_device_auth.request_device_code", fake_request)
    monkeypatch.setattr("app.services.account_device_auth.poll_device_authorization", fake_poll)
    monkeypatch.setattr("app.services.account_device_auth.exchange_device_authorization", fake_exchange)
    monkeypatch.setattr("app.services.account_device_auth._save_tokens_and_measure", fake_save)

    auth_session = await manager.start(session, account)
    await asyncio.wait_for(completed.wait(), timeout=2)
    for _ in range(20):
        if auth_session.status == "completed":
            break
        await asyncio.sleep(0.01)

    assert auth_session.status == "completed"
    async with session_factory() as background_session:
        job = await background_session.get(AccountCheckJob, auth_session.job_id)
        stored_account = await background_session.get(Account, account.id)
    assert (
        job is not None
        and job.status == "done"
        and job.result == "tokens_connected"
    )
    assert stored_account is not None and stored_account.status == "pending_validation"
    async with session_factory() as background_session:
        credential_jobs = list(
            (
                await background_session.execute(
                    select(AccountCheckJob).where(
                        AccountCheckJob.account_id == account.id,
                        AccountCheckJob.job_type == "full_validation",
                    )
                )
            ).scalars()
        )
    assert len(credential_jobs) == 1
    assert credential_jobs[0].status == "pending"
    await manager.shutdown()


async def test_background_and_frontend_poll_cannot_exchange_twice(
    session: AsyncSession,
    test_engine,
    monkeypatch,
):
    account = await _account(session)
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    manager = AccountDeviceAuthManager(session_factory=session_factory)
    poll_started = asyncio.Event()
    release_poll = asyncio.Event()
    poll_calls = 0

    async def fake_request():
        return DeviceCode("device", "ABCD-EFGH", 1)

    async def fake_poll(*_args):
        nonlocal poll_calls
        poll_calls += 1
        poll_started.set()
        await release_poll.wait()
        return DeviceAuthorization("code", "verifier", "challenge")

    async def fake_exchange(_authorization):
        return RefreshedTokens("access", "refresh", _id_token("owner@example.com"))

    async def fake_save(
        _session, target: Account, _tokens, *, browser_proxy=None
    ):
        assert browser_proxy is None
        target.status = "active"

    monkeypatch.setattr("app.services.account_device_auth.request_device_code", fake_request)
    monkeypatch.setattr("app.services.account_device_auth.poll_device_authorization", fake_poll)
    monkeypatch.setattr("app.services.account_device_auth.exchange_device_authorization", fake_exchange)
    monkeypatch.setattr("app.services.account_device_auth._save_tokens_and_measure", fake_save)

    auth_session = await manager.start(session, account)
    await asyncio.wait_for(poll_started.wait(), timeout=2)
    frontend_poll = asyncio.create_task(
        manager.poll(session, account, auth_session.id)
    )
    release_poll.set()
    result = await asyncio.wait_for(frontend_poll, timeout=2)

    assert result.status == "completed"
    assert poll_calls == 1
    await manager.shutdown()


async def test_shutdown_cancels_background_task_and_terminalizes_job(
    session: AsyncSession,
    test_engine,
    monkeypatch,
):
    account = await _account(session)
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    manager = AccountDeviceAuthManager(session_factory=session_factory)
    poll_started = asyncio.Event()

    async def fake_request():
        return DeviceCode("device", "ABCD-EFGH", 60)

    async def fake_poll(*_args):
        poll_started.set()
        return None

    monkeypatch.setattr("app.services.account_device_auth.request_device_code", fake_request)
    monkeypatch.setattr("app.services.account_device_auth.poll_device_authorization", fake_poll)

    auth_session = await manager.start(session, account)
    await asyncio.wait_for(poll_started.wait(), timeout=2)
    assert manager._tasks

    await manager.shutdown()

    assert auth_session.status == "expired"
    assert auth_session.error_code == "device_auth_shutdown"
    assert manager._tasks == {}
    assert manager._sessions == {}
    async with session_factory() as background_session:
        job = await background_session.get(AccountCheckJob, auth_session.job_id)
        stored_account = await background_session.get(Account, account.id)
    assert job is not None and job.status == "failed"
    assert "device_auth_shutdown" in (job.error or "")
    assert stored_account is not None
    assert stored_account.status == "validation_failed"
