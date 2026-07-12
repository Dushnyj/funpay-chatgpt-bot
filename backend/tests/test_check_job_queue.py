from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import CheckJobQueue
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
