from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountCheckJob
from app.models.rental import OCCUPYING_RENTAL_STATUSES, Rental
from app.services.account_occupancy import account_is_busy


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
        reserved_targets = select(Rental.replacement_target_account_id).where(
            Rental.replacement_target_account_id.is_not(None)
        )
        occupied_accounts = select(Rental.account_id).where(
            Rental.status.in_(OCCUPYING_RENTAL_STATUSES)
        )
        base_stmt = (
            select(AccountCheckJob)
            .join(Account, Account.id == AccountCheckJob.account_id)
            .where(
                AccountCheckJob.status == "pending",
                AccountCheckJob.job_type.in_(job_types),
                Account.id.not_in(reserved_targets),
                Account.id.not_in(occupied_accounts),
            )
            .order_by(
                case(
                    (AccountCheckJob.priority.in_(["new", "refresh_recover"]), 0),
                    (AccountCheckJob.priority == "manual", 1),
                    (AccountCheckJob.priority == "scheduled", 2),
                    (AccountCheckJob.priority == "limit_check", 3),
                    else_=9,
                ),
                AccountCheckJob.created_at.asc(),
            )
        )
        rejected_account_ids: set[int] = set()
        while True:
            stmt = base_stmt
            if rejected_account_ids:
                stmt = stmt.where(Account.id.not_in(rejected_account_ids))
            candidate = (
                await session.execute(
                    stmt.limit(1).with_for_update(
                        # Account is the global allocator/mutator
                        # serialization row. Always lock it before Job.
                        of=Account,
                        skip_locked=True,
                    )
                )
            ).scalar_one_or_none()
            if candidate is None:
                return None

            # The first statement's Rental subqueries may have used a snapshot
            # taken just before a concurrent allocation committed. Account is
            # ours now, so a fresh statement sees that commit and prevents the
            # validation from starting on an occupied/reserved account. Lock
            # Job second to keep the global Account -> Job order.
            job = (
                await session.execute(
                    select(AccountCheckJob)
                    .where(AccountCheckJob.id == candidate.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                job is None
                or job.status != "pending"
                or job.job_type not in job_types
                or await account_is_busy(session, candidate.account_id)
            ):
                rejected_account_ids.add(candidate.account_id)
                continue
            return job

    async def recover_stale_running(
        self,
        session: AsyncSession,
        job_types: tuple[str, ...],
        *,
        stale_before: datetime | None = None,
    ) -> int:
        """Durably requeue worker jobs whose running lease is no longer valid.

        ``stale_before`` is explicit so application startup can reclaim every
        job owned by the previous process immediately. Periodic callers omit it
        and use the normal worker lease.
        """
        now = datetime.now(timezone.utc)
        cutoff = stale_before or now - _RUNNING_JOB_LEASE
        result = await session.execute(
            select(AccountCheckJob)
            .where(
                AccountCheckJob.status == "running",
                AccountCheckJob.job_type.in_(job_types),
                or_(
                    AccountCheckJob.started_at.is_(None),
                    AccountCheckJob.started_at <= cutoff,
                ),
            )
            .order_by(AccountCheckJob.id)
            .with_for_update(skip_locked=True)
        )
        jobs = list(result.scalars())
        for job in jobs:
            job.status = "pending"
            job.started_at = None
            job.finished_at = None
            job.error = None
            job.result = "requeued_after_stale_worker"
        if jobs:
            await session.flush()
        return len(jobs)

    async def fail_active_jobs(
        self,
        session: AsyncSession,
        job_types: tuple[str, ...],
        *,
        error: str,
    ) -> list[int]:
        """Terminalize active jobs that cannot be resumed after a restart."""
        result = await session.execute(
            select(AccountCheckJob)
            .where(
                AccountCheckJob.status.in_(["pending", "running"]),
                AccountCheckJob.job_type.in_(job_types),
            )
            .order_by(AccountCheckJob.id)
            .with_for_update(skip_locked=True)
        )
        jobs = list(result.scalars())
        now = datetime.now(timezone.utc)
        for job in jobs:
            job.status = "failed"
            job.result = None
            job.error = error
            job.finished_at = now
        if jobs:
            await session.flush()
        return list(dict.fromkeys(job.account_id for job in jobs))

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
        job.finished_at = None
        job.error = None
        await session.flush()

    async def mark_done(self, session: AsyncSession, job: AccountCheckJob, result: str) -> None:
        job.status = "done"
        job.result = result
        job.error = None
        job.finished_at = datetime.now(timezone.utc)
        await session.flush()

    async def mark_failed(self, session: AsyncSession, job: AccountCheckJob, error: str) -> None:
        job.status = "failed"
        job.result = None
        job.error = error
        job.finished_at = datetime.now(timezone.utc)
        await session.flush()

    async def requeue(
        self,
        session: AsyncSession,
        job: AccountCheckJob,
        *,
        reason: str,
    ) -> None:
        """Return an interrupted job to the durable pending queue."""
        job.status = "pending"
        job.started_at = None
        job.finished_at = None
        job.error = None
        job.result = reason
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
