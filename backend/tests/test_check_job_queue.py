from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import ActiveJobConflict, CheckJobQueue
from app.models.account import Account, AccountCheckJob
from app.models.catalog import SubscriptionTier


async def _add_account(session: AsyncSession, login: str = "acc1") -> Account:
    tier = SubscriptionTier(name=f"tier_{login}", is_active=True)
    session.add(tier)
    await session.flush()
    acc = Account(
        login=login, password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=tier.id, status="active",
    )
    session.add(acc)
    await session.flush()
    return acc


async def test_enqueue_creates_pending_job(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    job = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    assert job.status == "pending"
    assert job.priority == "new"
    assert job.job_type == "full_validation"


async def test_dedup_skips_existing_pending(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    first = await q.enqueue(session, account_id=acc.id, priority="scheduled", job_type="limit_check")
    second = await q.enqueue(session, account_id=acc.id, priority="scheduled", job_type="limit_check")
    assert second.id == first.id


async def test_higher_priority_overrides_lower(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    low = await q.enqueue(session, account_id=acc.id, priority="scheduled", job_type="limit_check")
    high = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    await session.refresh(low)
    assert low.status == "done"
    assert high.priority == "new"
    assert high.job_type == "full_validation"


async def test_regular_enqueue_does_not_supersede_running_job(
    session: AsyncSession,
):
    account = await _add_account(session)
    queue = CheckJobQueue()
    running = await queue.enqueue(
        session,
        account_id=account.id,
        priority="manual",
        job_type="device_auth",
    )
    await queue.mark_running(session, running)

    result = await queue.enqueue(
        session,
        account_id=account.id,
        priority="new",
        job_type="full_validation",
    )

    assert result.id == running.id
    assert running.status == "running"


async def test_running_limit_check_serializes_full_validation(
    session: AsyncSession,
):
    account = await _add_account(session)
    queue = CheckJobQueue()
    limit_job = await queue.enqueue(
        session,
        account.id,
        priority="limit_check",
        job_type="limit_check",
    )
    await queue.mark_running(session, limit_job)

    validation = await queue.enqueue(
        session,
        account.id,
        priority="scheduled",
        job_type="full_validation",
    )

    assert validation.id == limit_job.id
    assert limit_job.status == "running"


async def test_fetch_next_pending_returns_oldest(session: AsyncSession):
    acc1 = await _add_account(session, "acc1")
    q = CheckJobQueue()
    j1 = await q.enqueue(session, account_id=acc1.id, priority="scheduled", job_type="limit_check")
    import asyncio
    await asyncio.sleep(0.01)
    acc2 = await _add_account(session, "acc2")
    j2 = await q.enqueue(session, account_id=acc2.id, priority="scheduled", job_type="limit_check")
    next_job = await q.fetch_next_pending(session, job_types=("limit_check",))
    assert next_job is not None
    assert next_job.id == j1.id


async def test_fetch_next_pending_filters_by_type(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    next_job = await q.fetch_next_pending(session, job_types=("limit_check",))
    assert next_job is None


async def test_fetch_next_pending_respects_priority_across_accounts(
    session: AsyncSession,
):
    low_account = await _add_account(session, "low")
    high_account = await _add_account(session, "high")
    queue = CheckJobQueue()
    await queue.enqueue(
        session,
        low_account.id,
        priority="scheduled",
        job_type="full_validation",
    )
    high = await queue.enqueue(
        session,
        high_account.id,
        priority="refresh_recover",
        job_type="refresh_recover",
    )

    next_job = await queue.fetch_next_pending(
        session, ("full_validation", "refresh_recover")
    )

    assert next_job is not None
    assert next_job.id == high.id


async def test_fetch_next_pending_fresh_recheck_skips_busy_candidate(
    session: AsyncSession,
    monkeypatch,
):
    """A rental committed before Account lock must defer that account's job."""

    import asyncio

    first_account = await _add_account(session, "fresh-busy-first")
    queue = CheckJobQueue()
    first_job = await queue.enqueue(
        session,
        first_account.id,
        priority="scheduled",
        job_type="limit_check",
    )
    await asyncio.sleep(0.01)
    second_account = await _add_account(session, "fresh-busy-second")
    second_job = await queue.enqueue(
        session,
        second_account.id,
        priority="scheduled",
        job_type="limit_check",
    )
    checked: list[int] = []

    async def fresh_busy(_session: AsyncSession, account_id: int) -> bool:
        checked.append(account_id)
        return account_id == first_account.id

    monkeypatch.setattr("app.check_job_queue.account_is_busy", fresh_busy)

    next_job = await queue.fetch_next_pending(session, ("limit_check",))

    assert next_job is not None
    assert next_job.id == second_job.id
    assert first_job.status == "pending"
    assert checked == [first_account.id, second_account.id]


async def test_mark_running_updates_status(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    job = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    await q.mark_running(session, job)
    await session.refresh(job)
    assert job.status == "running"
    assert job.started_at is not None


async def test_mark_done_updates_status(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    job = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    await q.mark_done(session, job, result="ok")
    await session.refresh(job)
    assert job.status == "done"
    assert job.result == "ok"
    assert job.finished_at is not None


async def test_mark_failed_updates_error(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    job = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    await q.mark_failed(session, job, error="connection timeout")
    await session.refresh(job)
    assert job.status == "failed"
    assert job.error == "connection timeout"


async def test_exclusive_job_supersedes_all_pending_validation_jobs(
    session: AsyncSession,
):
    account = await _add_account(session)
    queue = CheckJobQueue()
    old = await queue.enqueue(
        session, account.id, priority="new", job_type="full_validation"
    )

    device = await queue.enqueue_exclusive(
        session,
        account.id,
        priority="manual",
        job_type="device_auth",
        superseded_by="device_auth",
    )

    assert old.status == "done"
    assert old.result == "superseded:device_auth"
    assert device.status == "pending"


async def test_exclusive_job_rejects_live_running_validation(session: AsyncSession):
    account = await _add_account(session)
    queue = CheckJobQueue()
    running = await queue.enqueue(
        session, account.id, priority="new", job_type="full_validation"
    )
    await queue.mark_running(session, running)

    with pytest.raises(ActiveJobConflict) as error:
        await queue.enqueue_exclusive(
            session,
            account.id,
            priority="manual",
            job_type="device_auth",
            superseded_by="device_auth",
        )

    assert error.value.job_id == running.id
    assert running.status == "running"


async def test_exclusive_job_recovers_expired_running_lease(session: AsyncSession):
    account = await _add_account(session)
    queue = CheckJobQueue()
    stale = await queue.enqueue(
        session, account.id, priority="new", job_type="full_validation"
    )
    stale.status = "running"
    stale.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.flush()

    replacement = await queue.enqueue_exclusive(
        session,
        account.id,
        priority="manual",
        job_type="device_auth",
        superseded_by="device_auth",
    )

    assert stale.status == "failed"
    assert stale.error == "stale_worker_lease"
    assert replacement.status == "pending"


async def test_recover_stale_running_requeues_only_expired_worker_jobs(
    session: AsyncSession,
):
    stale_account = await _add_account(session, "stale")
    live_account = await _add_account(session, "live")
    queue = CheckJobQueue()
    stale = await queue.enqueue(
        session, stale_account.id, priority="scheduled", job_type="full_validation"
    )
    live = await queue.enqueue(
        session, live_account.id, priority="refresh_recover", job_type="refresh_recover"
    )
    stale.status = "running"
    stale.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    live.status = "running"
    live.started_at = datetime.now(timezone.utc)
    await session.flush()

    recovered = await queue.recover_stale_running(
        session, ("full_validation", "refresh_recover")
    )

    assert recovered == 1
    assert stale.status == "pending"
    assert stale.started_at is None
    assert stale.result == "requeued_after_stale_worker"
    assert live.status == "running"


async def test_startup_recovery_can_requeue_all_previous_process_jobs(
    session: AsyncSession,
):
    account = await _add_account(session)
    queue = CheckJobQueue()
    job = await queue.enqueue(
        session, account.id, priority="scheduled", job_type="full_validation"
    )
    await queue.mark_running(session, job)

    recovered = await queue.recover_stale_running(
        session,
        ("full_validation", "refresh_recover"),
        stale_before=datetime.now(timezone.utc),
    )

    assert recovered == 1
    assert job.status == "pending"


async def test_fail_active_jobs_terminalizes_non_resumable_device_auth(
    session: AsyncSession,
):
    account = await _add_account(session)
    queue = CheckJobQueue()
    job = await queue.enqueue(
        session, account.id, priority="manual", job_type="device_auth"
    )
    await queue.mark_running(session, job)

    account_ids = await queue.fail_active_jobs(
        session,
        ("device_auth",),
        error="device_auth_server_restarted",
    )

    assert account_ids == [account.id]
    assert job.status == "failed"
    assert job.error == "device_auth_server_restarted"
    assert job.finished_at is not None
