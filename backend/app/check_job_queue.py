from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountCheckJob


_PRIORITY_ORDER = {"new": 0, "refresh_recover": 0, "manual": 1, "scheduled": 2, "limit_check": 3}
_RUNNING_JOB_LEASE = timedelta(minutes=20)


class ActiveJobConflict(RuntimeError):
    def __init__(self, job: AccountCheckJob) -> None:
        self.job_id = job.id
        self.job_type = job.job_type
        super().__init__(f"account job {job.id} ({job.job_type}) is running")


class CheckJobQueue:
    """Очередь задач проверки аккаунтов (AccountCheckJob).

    Дедупликация: если для аккаунта уже есть pending/running job, новый не
    создаём. Высший приоритет перебивает низший (старый помечается done).
    """

    async def enqueue(
        self,
        session: AsyncSession,
        account_id: int,
        priority: str,
        job_type: str,
    ) -> AccountCheckJob:
        # Serialize every producer on the account row so regular jobs cannot
        # slip in beside an exclusive device/manual validation transaction.
        await session.execute(
            select(Account.id).where(Account.id == account_id).with_for_update()
        )
        existing = await self._find_active(session, account_id)
        if existing is not None:
            if existing.status == "running":
                return existing
            if _PRIORITY_ORDER.get(priority, 9) < _PRIORITY_ORDER.get(
                existing.priority, 9
            ):
                existing.status = "done"
                existing.result = "superseded"
                existing.finished_at = datetime.now(timezone.utc)
                await session.flush()
            else:
                return existing

        job = AccountCheckJob(
            account_id=account_id,
            priority=priority,
            job_type=job_type,
            status="pending",
        )
        session.add(job)
        await session.flush()
        return job

    async def fetch_next_pending(
        self,
        session: AsyncSession,
        job_types: tuple[str, ...],
    ) -> AccountCheckJob | None:
        result = await session.execute(
            select(AccountCheckJob)
            .where(
                AccountCheckJob.status == "pending",
                AccountCheckJob.job_type.in_(job_types),
            )
            .order_by(AccountCheckJob.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return result.scalar_one_or_none()

    async def enqueue_exclusive(
        self,
        session: AsyncSession,
        account_id: int,
        priority: str,
        job_type: str,
        *,
        superseded_by: str,
    ) -> AccountCheckJob:
        """Create the only active validation job for an account.

        Pending jobs are durably superseded. A live running job is never raced;
        callers receive ``ActiveJobConflict`` and can retry. Jobs whose worker
        lease expired are failed and no longer block recovery.
        """
        now = datetime.now(timezone.utc)
        # Lock the stable parent row as well as existing jobs. Locking only the
        # jobs is insufficient when two transactions both observe an empty
        # active set and concurrently insert their first pending job.
        await session.execute(
            select(Account.id)
            .where(Account.id == account_id)
            .with_for_update()
        )
        active = list(
            (
                await session.execute(
                    select(AccountCheckJob)
                    .where(
                        AccountCheckJob.account_id == account_id,
                        AccountCheckJob.status.in_(["pending", "running"]),
                    )
                    .order_by(AccountCheckJob.id)
                    .with_for_update()
                )
            ).scalars()
        )
        for job in active:
            started_at = job.started_at
            if started_at is not None and started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            if (
                job.status == "running"
                and started_at is not None
                and started_at > now - _RUNNING_JOB_LEASE
            ):
                raise ActiveJobConflict(job)

        for job in active:
            if job.status == "running":
                job.status = "failed"
                job.error = "stale_worker_lease"
            else:
                job.status = "done"
                job.result = f"superseded:{superseded_by}"
            job.finished_at = now

        job = AccountCheckJob(
            account_id=account_id,
            priority=priority,
            job_type=job_type,
            status="pending",
        )
        session.add(job)
        await session.flush()
        return job

    async def mark_running(self, session: AsyncSession, job: AccountCheckJob) -> None:
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        await session.flush()

    async def mark_done(self, session: AsyncSession, job: AccountCheckJob, result: str) -> None:
        job.status = "done"
        job.result = result
        job.finished_at = datetime.now(timezone.utc)
        await session.flush()

    async def mark_failed(self, session: AsyncSession, job: AccountCheckJob, error: str) -> None:
        job.status = "failed"
        job.error = error
        job.finished_at = datetime.now(timezone.utc)
        await session.flush()

    async def _find_active(
        self,
        session: AsyncSession,
        account_id: int,
    ) -> AccountCheckJob | None:
        result = await session.execute(
            select(AccountCheckJob).where(
                AccountCheckJob.account_id == account_id,
                AccountCheckJob.status.in_(["pending", "running"]),
            ).order_by(AccountCheckJob.id).limit(1).with_for_update()
        )
        return result.scalar_one_or_none()
