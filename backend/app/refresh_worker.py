from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import CheckJobQueue
from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.audit import AuditLog
from app.models.settings import SellerSettings
from app.services.account_limits import MeasureResult, measure_account_limits
from app.services.account_validation import (
    AccountValidationError,
    ValidationCode,
    ValidationOutcome,
    ValidationStage,
    validate_account,
)
from app.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)

_TERMINAL_ACCOUNT_FAILURE_CODES = {
    ValidationCode.INVALID_CREDENTIALS.value,
    ValidationCode.INVALID_TOTP.value,
    ValidationCode.MISSING_2FA_DATA.value,
    ValidationCode.EMAIL_AUTH_FAILED.value,
    ValidationCode.EMAIL_PROVIDER_UNSUPPORTED.value,
    ValidationCode.EMAIL_SECURITY_CHALLENGE.value,
    ValidationCode.PLAN_DETECTION_FAILED.value,
    ValidationCode.PLAN_WINDOW_MISMATCH.value,
    ValidationCode.PROXY_ROUTE_CHANGED.value,
}


class RefreshRecoveryWorker:
    """Worker for serialized validation, recovery and limit jobs.

    Один вызов process_next берёт старейший pending job, выполняет
    validate_account и оставляет задачу в устойчивом terminal/pending состоянии.
    Интервал запуска принадлежит Scheduler; воркер сам не спит.
    """

    def __init__(
        self,
        check_delay_seconds: int | None = None,
        *,
        max_attempts: int = 3,
    ) -> None:
        self._queue = CheckJobQueue()
        # Kept as a compatibility argument for existing callers. Sleeping here
        # used to double the Scheduler delay and blocked configured concurrency.
        self._max_attempts = max(1, max_attempts)

    async def process_next(self, session: AsyncSession) -> bool:
        """Обработать один pending job. True если обработал, False если очереди пуста."""
        job = await self._queue.fetch_next_pending(
            session,
            job_types=("full_validation", "refresh_recover", "limit_check"),
        )
        if job is None:
            return False

        account = await session.get(Account, job.account_id)
        original_account_status = account.status if account is not None else None

        if job.job_type == "refresh_recover":
            limits = await session.get(AccountLimits, job.account_id)
            if (
                limits is not None
                and limits.refresh_recover_attempts >= self._max_attempts
            ):
                await self._queue.mark_failed(
                    session,
                    job,
                    error=_safe_error(
                        "refresh_recovery_exhausted",
                        "Автоматическое восстановление исчерпало число попыток.",
                    ),
                )
                await self._enqueue_requested_rerun(session, job.account_id)
                await session.commit()
                return True

        try:
            await self._queue.mark_running(session, job)
            # Publish the running lease before browser/network work. Device
            # auth or manual recheck can then reject a conflicting validation
            # instead of racing token and account status updates.
            await session.commit()
            if job.job_type == "limit_check":
                await self._process_limit_check(session, job)
                return True
            outcome = await validate_account(session, job.account_id)
            if outcome is not ValidationOutcome.OK:
                raise AccountValidationError(
                    ValidationStage.INTERNAL,
                    ValidationCode.INTERNAL_ERROR,
                    "Проверка аккаунта не завершилась успешно.",
                )
            account = await session.get(Account, job.account_id)
            if account is not None:
                account.chatgpt_last_check_at = datetime.now(timezone.utc)
            await self._queue.mark_done(session, job, result="ok")
            await self._enqueue_requested_rerun(session, job.account_id)
            await session.commit()
        except asyncio.CancelledError:
            await self._requeue_cancelled(session, job.id)
            raise
        except AccountValidationError as exc:
            account = await session.get(Account, job.account_id)
            if account is not None:
                await session.refresh(
                    account,
                    attribute_names=["operator_status_override"],
                )
                if account.operator_status_override is not None:
                    account.status = account.operator_status_override
                elif _preserve_scheduled_active_account(
                    job,
                    original_account_status,
                    exc.code,
                ):
                    # validate_account deliberately writes validation_failed on
                    # every error. A transient daily browser/network failure
                    # must not permanently remove a previously proven account
                    # from the pool; the next scheduled run can retry it.
                    account.status = original_account_status
                else:
                    account.status = "validation_failed"
            await self._record_recovery_failure(session, job)
            await self._queue.mark_failed(session, job, error=exc.to_json())
            await self._enqueue_requested_rerun(session, job.account_id)
            await session.commit()
            logger.info(
                "Validation job %s failed for account %s at %s (%s)",
                job.id,
                job.account_id,
                exc.stage,
                exc.code,
            )
        except Exception:
            if job.job_type == "limit_check":
                await self._queue.mark_failed(
                    session,
                    job,
                    error=_safe_error(
                        ValidationCode.MEASURE_FAILED.value,
                        "Не удалось обновить лимиты аккаунта.",
                        stage=ValidationStage.LIMIT_MEASUREMENT,
                    ),
                )
                await self._enqueue_requested_rerun(session, job.account_id)
                await session.commit()
                logger.exception(
                    "Limit job %s failed for account %s", job.id, job.account_id
                )
                return True
            account = await session.get(Account, job.account_id)
            if account is not None:
                await session.refresh(
                    account,
                    attribute_names=["operator_status_override"],
                )
                if account.operator_status_override is not None:
                    account.status = account.operator_status_override
                elif _preserve_scheduled_active_account(
                    job,
                    original_account_status,
                    None,
                ):
                    account.status = original_account_status
                else:
                    account.status = "validation_failed"
            await self._record_recovery_failure(session, job)
            safe_error = _safe_error(
                ValidationCode.INTERNAL_ERROR.value,
                "Внутренняя ошибка проверки аккаунта.",
            )
            await self._queue.mark_failed(session, job, error=safe_error)
            await self._enqueue_requested_rerun(session, job.account_id)
            await session.commit()
            logger.exception("Job %s failed for account %s", job.id, job.account_id)

        return True

    async def _process_limit_check(
        self,
        session: AsyncSession,
        job: AccountCheckJob,
    ) -> None:
        result = await measure_account_limits(session, job.account_id)
        if result is MeasureResult.OK:
            await self._warn_if_low_limits(session, job.account_id)
            await self._queue.mark_done(session, job, result="ok")
        elif result is MeasureResult.REFRESH_FAILED:
            await self._queue.mark_failed(
                session,
                job,
                error=_safe_error(
                    MeasureResult.REFRESH_FAILED.value,
                    "Refresh token аккаунта истёк; создана задача восстановления.",
                    stage=ValidationStage.LIMIT_MEASUREMENT,
                ),
            )
            await self._queue.enqueue(
                session,
                account_id=job.account_id,
                priority="refresh_recover",
                job_type="refresh_recover",
            )
        elif result in {
            MeasureResult.PLAN_DETECTION_FAILED,
            MeasureResult.PLAN_WINDOW_MISMATCH,
        }:
            account = await session.get(Account, job.account_id)
            if account is not None:
                account.status = (
                    account.operator_status_override or "validation_failed"
                )
            detection_failed = result is MeasureResult.PLAN_DETECTION_FAILED
            await self._queue.mark_failed(
                session,
                job,
                error=_safe_error(
                    (
                        ValidationCode.PLAN_DETECTION_FAILED.value
                        if detection_failed
                        else ValidationCode.PLAN_WINDOW_MISMATCH.value
                    ),
                    (
                        "OpenAI не вернул однозначный поддерживаемый тариф."
                        if detection_failed
                        else "Длительное окно Codex не соответствует определённому тарифу."
                    ),
                    stage=ValidationStage.LIMIT_MEASUREMENT,
                ),
            )
        else:
            await self._queue.mark_failed(
                session,
                job,
                error=_safe_error(
                    MeasureResult.BACKEND_ERROR.value,
                    "OpenAI временно не выдал данные о лимитах.",
                    stage=ValidationStage.LIMIT_MEASUREMENT,
                ),
            )
        await self._enqueue_requested_rerun(session, job.account_id)
        await session.commit()

    async def _enqueue_requested_rerun(
        self,
        session: AsyncSession,
        account_id: int,
    ) -> None:
        """Atomically hand a credential-update race to one follow-up job."""

        account = (
            await session.execute(
                select(Account)
                .where(Account.id == account_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if account is None or not account.validation_rerun_requested:
            return
        account.validation_rerun_requested = False
        # A credential update may have raced the worker's final status write.
        # Keep the account out of the allocation pool until the follow-up pass
        # has validated the newly stored credentials.
        account.status = account.operator_status_override or "pending_validation"
        await self._queue.enqueue(
            session,
            account_id=account_id,
            priority="manual",
            job_type="full_validation",
        )

    async def _warn_if_low_limits(
        self,
        session: AsyncSession,
        account_id: int,
    ) -> None:
        """Send at most one warning for an observed long-window reset."""

        limits = await session.get(AccountLimits, account_id)
        settings = await session.get(SellerSettings, 1)
        account = await session.get(Account, account_id)
        if limits is None or settings is None or account is None:
            return
        expected = limits.expected_long_window_seconds
        if limits.plan_window_status != "ok" or expected is None:
            return

        observations = (
            (
                limits.codex_primary_window_seconds,
                limits.codex_primary_remaining_pct,
                limits.codex_primary_resets_at,
            ),
            (
                limits.codex_secondary_window_seconds,
                limits.codex_secondary_remaining_pct,
                limits.codex_secondary_resets_at,
            ),
        )
        match = next((item for item in observations if item[0] == expected), None)
        if match is None or match[1] is None:
            return
        remaining = int(match[1])
        if remaining > settings.limits_warn_threshold_pct:
            # A recovered/high value marks a new threshold cycle. This also
            # lets providers that omit ``resets_at`` warn again after an
            # observable reset, while still sending only once during a low
            # period.
            limits.low_limit_warning_fingerprint = None
            limits.low_limit_warned_at = None
            return

        resets_at = match[2]
        if resets_at is not None and resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=timezone.utc)
        reset_key = resets_at.isoformat() if resets_at is not None else "unknown"
        fingerprint = f"long:{expected}:{reset_key}"
        if limits.low_limit_warning_fingerprint == fingerprint:
            return

        window_label = "30 дней" if expected == 30 * 24 * 60 * 60 else "7 дней"
        notifier = await TelegramNotifier.from_settings(session)
        if notifier is None:
            return
        sent = await notifier.notify_low_limits(
            account.login,
            remaining_pct=remaining,
            window_label=window_label,
        )
        if not sent:
            return

        # Mark the edge only after Telegram accepted the message. A transient
        # network failure therefore retries on the next measurement.
        limits.low_limit_warning_fingerprint = fingerprint
        limits.low_limit_warned_at = datetime.now(timezone.utc)
        session.add(
            AuditLog(
                event_type="account_low_limit_warning",
                account_id=account.id,
                metadata_={
                    "remaining_pct": remaining,
                    "window_seconds": expected,
                    "resets_at": reset_key,
                    "threshold_pct": settings.limits_warn_threshold_pct,
                },
            )
        )

    async def _record_recovery_failure(
        self,
        session: AsyncSession,
        job: AccountCheckJob,
    ) -> None:
        if job.job_type != "refresh_recover":
            return
        limits = await session.get(AccountLimits, job.account_id)
        if limits is None:
            return
        limits.refresh_status = "expired"
        limits.refresh_recover_attempts += 1
        limits.refresh_last_recover_at = datetime.now(timezone.utc)

    async def _requeue_cancelled(
        self,
        session: AsyncSession,
        job_id: int,
    ) -> None:
        """Best-effort durable handoff before propagating worker cancellation."""

        async def persist() -> None:
            await session.rollback()
            current = await session.get(AccountCheckJob, job_id)
            if current is not None and current.status == "running":
                await self._queue.requeue(
                    session,
                    current,
                    reason="requeued_after_worker_cancelled",
                )
                await session.commit()

        try:
            await asyncio.shield(persist())
        except Exception:
            logger.exception("Could not requeue cancelled validation job %s", job_id)


def _safe_error(
    code: str,
    detail: str,
    *,
    stage: ValidationStage = ValidationStage.INTERNAL,
) -> str:
    return json.dumps(
        {
            "stage": stage.value,
            "code": code,
            "detail": detail,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _preserve_scheduled_active_account(
    job: AccountCheckJob,
    original_status: str | None,
    error_code: str | None,
) -> bool:
    """Keep a proven account sellable after a transient scheduled check error."""

    return (
        job.job_type == "full_validation"
        and job.priority == "scheduled"
        and original_status == "active"
        and error_code not in _TERMINAL_ACCOUNT_FAILURE_CODES
    )
