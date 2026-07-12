from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import CheckJobQueue
from app.services.account_validation import ValidationOutcome, validate_account

logger = logging.getLogger(__name__)


class RefreshRecoveryWorker:
    """Воркер перезаходов: обрабатывает AccountCheckJob (full_validation, refresh_recover).

    Один вызов process_next: берёт старейший pending job, выполняет validate_account,
    помечает done/failed. Пауза check_delay_seconds после операции (анти-спам).
    """

    def __init__(self, check_delay_seconds: int = 45) -> None:
        self._queue = CheckJobQueue()
        self._check_delay = check_delay_seconds

    async def process_next(self, session: AsyncSession) -> bool:
        """Обработать один pending job. True если обработал, False если очереди пуста."""
        job = await self._queue.fetch_next_pending(
            session, job_types=("full_validation", "refresh_recover"),
        )
        if job is None:
            return False

        await self._queue.mark_running(session, job)
        try:
            outcome = await validate_account(session, job.account_id)
            if outcome is ValidationOutcome.OK:
                await self._queue.mark_done(session, job, result="ok")
            else:
                await self._queue.mark_failed(session, job, error=outcome.value)
            await session.commit()
        except Exception as exc:
            await self._queue.mark_failed(session, job, error=str(exc))
            await session.commit()
            logger.exception("Job %s failed for account %s", job.id, job.account_id)

        if self._check_delay > 0:
            await asyncio.sleep(self._check_delay)
        return True
