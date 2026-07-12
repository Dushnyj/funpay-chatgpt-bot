from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import CheckJobQueue
from app.models.account import Account
from app.services.account_validation import (
    AccountValidationError,
    ValidationCode,
    ValidationOutcome,
    ValidationStage,
    validate_account,
)

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
        # Publish the running lease before browser/network work. Device auth or
        # manual recheck can then reject a conflicting validation instead of
        # racing token and account status updates.
        await session.commit()
        try:
            outcome = await validate_account(session, job.account_id)
            if outcome is ValidationOutcome.OK:
                await self._queue.mark_done(session, job, result="ok")
            await session.commit()
        except AccountValidationError as exc:
            account = await session.get(Account, job.account_id)
            if account is not None:
                account.status = "validation_failed"
            await self._queue.mark_failed(session, job, error=exc.to_json())
            await session.commit()
            logger.info(
                "Validation job %s failed for account %s at %s (%s)",
                job.id,
                job.account_id,
                exc.stage,
                exc.code,
            )
        except Exception as exc:
            account = await session.get(Account, job.account_id)
            if account is not None:
                account.status = "validation_failed"
            safe_error = json.dumps(
                {
                    "stage": ValidationStage.INTERNAL.value,
                    "code": ValidationCode.INTERNAL_ERROR.value,
                    "detail": "Внутренняя ошибка проверки аккаунта.",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await self._queue.mark_failed(session, job, error=safe_error)
            await session.commit()
            logger.exception("Job %s failed for account %s", job.id, job.account_id)

        if self._check_delay > 0:
            await asyncio.sleep(self._check_delay)
        return True
