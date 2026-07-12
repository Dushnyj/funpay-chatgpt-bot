from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import AccountCheckJob


_PRIORITY_ORDER = {"new": 0, "refresh_recover": 0, "manual": 1, "scheduled": 2, "limit_check": 3}


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
        existing = await self._find_active(session, account_id)
        if existing is not None:
            if _PRIORITY_ORDER.get(priority, 9) < _PRIORITY_ORDER.get(existing.priority, 9):
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
        )
        return result.scalar_one_or_none()

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
            ).limit(1)
        )
        return result.scalar_one_or_none()
