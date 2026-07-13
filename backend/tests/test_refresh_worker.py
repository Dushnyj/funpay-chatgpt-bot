import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountCheckJob, AccountLimits
from app.models.audit import AuditLog
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings
from app.refresh_worker import RefreshRecoveryWorker
from app.services.account_validation import (
    AccountValidationError,
    ValidationCode,
    ValidationOutcome,
    ValidationStage,
)
from app.services.account_limits import MeasureResult


async def _add_account_with_job(session: AsyncSession, job_type: str = "full_validation") -> tuple[Account, AccountCheckJob]:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    acc = Account(
        login="acc1", password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=tier.id, status="pending_validation",
    )
    session.add(acc)
    await session.flush()
    job = AccountCheckJob(
        account_id=acc.id, priority="new", job_type=job_type, status="pending",
    )
    session.add(job)
    await session.flush()
    return acc, job


async def test_process_next_pending_job_success(session: AsyncSession):
    acc, job = await _add_account_with_job(session)
    worker = RefreshRecoveryWorker(check_delay_seconds=0)
    with patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as mock_validate:
        mock_validate.return_value = ValidationOutcome.OK
        result = await worker.process_next(session)
    assert result is True
    await session.refresh(job)
    assert job.status == "done"
    await session.refresh(acc)
    assert acc.chatgpt_last_check_at is not None
    mock_validate.assert_awaited_once_with(session, acc.id)


async def test_process_next_returns_false_when_no_jobs(session: AsyncSession):
    worker = RefreshRecoveryWorker(check_delay_seconds=0)
    result = await worker.process_next(session)
    assert result is False


async def test_pending_job_waits_while_account_is_replacement_target(
    session: AsyncSession,
):
    target, job = await _add_account_with_job(session)
    tier = await session.get(SubscriptionTier, target.tier_id)
    duration = Duration(minutes=60, is_enabled=True, sort_order=10)
    scope = LimitScope(code="any", name="Any")
    old_account = Account(
        login="old-account@example.com",
        password_encrypted="password",
        totp_secret_encrypted="totp",
        tier_id=tier.id,
        status="maintenance",
    )
    session.add_all([duration, scope, old_account])
    await session.flush()
    now = datetime.now(timezone.utc)
    order = Order(
        funpay_order_id="worker-reservation-order",
        funpay_chat_id="worker-reservation-chat",
        buyer_funpay_id="worker-reservation-buyer",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        price=100,
        status="completed",
    )
    session.add(order)
    await session.flush()
    rental = Rental(
        order_id=order.id,
        account_id=old_account.id,
        replacement_target_account_id=target.id,
        buyer_funpay_id="worker-reservation-buyer",
        buyer_funpay_chat_id="worker-reservation-chat",
        tier_id=tier.id,
        duration_id=duration.id,
        limit_scope_id=scope.id,
        lang="ru",
        started_at=now,
        expires_at=now + timedelta(hours=1),
        status="active",
        expiry_revoke_started_at=now,
        credentials_delivery_status="sent",
        credentials_delivery_template="welcome",
        credentials_delivery_attempts=1,
    )
    session.add(rental)
    await session.commit()
    worker = RefreshRecoveryWorker(check_delay_seconds=0)

    with patch(
        "app.refresh_worker.validate_account", new_callable=AsyncMock,
    ) as validate:
        assert await worker.process_next(session) is False
        validate.assert_not_awaited()
    await session.refresh(job)
    assert job.status == "pending"

    rental.replacement_target_account_id = None
    rental.expiry_revoke_started_at = None
    await session.commit()
    with patch(
        "app.refresh_worker.validate_account",
        new=AsyncMock(return_value=ValidationOutcome.OK),
    ) as validate:
        assert await worker.process_next(session) is True
        validate.assert_awaited_once_with(session, target.id)


async def test_process_next_marks_failed_on_validation_error(session: AsyncSession):
    acc, job = await _add_account_with_job(session)
    worker = RefreshRecoveryWorker(check_delay_seconds=0)
    with patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as mock_validate:
        mock_validate.side_effect = AccountValidationError(
            ValidationStage.LOGIN,
            ValidationCode.INVALID_CREDENTIALS,
            "OpenAI отклонил логин или пароль.",
        )
        result = await worker.process_next(session)
    assert result is True
    await session.refresh(job)
    await session.refresh(acc)
    assert job.status == "failed"
    assert json.loads(job.error) == {
        "stage": "login",
        "code": "invalid_credentials",
        "detail": "OpenAI отклонил логин или пароль.",
    }
    assert acc.status == "validation_failed"


async def test_transient_scheduled_validation_error_preserves_active_account(
    session: AsyncSession,
):
    acc, job = await _add_account_with_job(session)
    acc.status = "active"
    job.priority = "scheduled"
    await session.flush()
    worker = RefreshRecoveryWorker(check_delay_seconds=0)

    with patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as validate:
        async def transient_failure(current_session, account_id):
            current = await current_session.get(Account, account_id)
            current.status = "validation_failed"
            await current_session.flush()
            raise AccountValidationError(
                ValidationStage.LOGIN,
                ValidationCode.LOGIN_TIMEOUT,
                "OpenAI временно не ответил.",
            )

        validate.side_effect = transient_failure
        assert await worker.process_next(session) is True

    await session.refresh(job)
    await session.refresh(acc)
    assert job.status == "failed"
    assert acc.status == "active"


async def test_terminal_scheduled_validation_error_disables_active_account(
    session: AsyncSession,
):
    acc, job = await _add_account_with_job(session)
    acc.status = "active"
    job.priority = "scheduled"
    await session.flush()
    worker = RefreshRecoveryWorker(check_delay_seconds=0)

    with patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as validate:
        validate.side_effect = AccountValidationError(
            ValidationStage.LOGIN,
            ValidationCode.INVALID_CREDENTIALS,
            "OpenAI отклонил логин или пароль.",
        )
        assert await worker.process_next(session) is True

    await session.refresh(job)
    await session.refresh(acc)
    assert job.status == "failed"
    assert acc.status == "validation_failed"


@pytest.mark.parametrize(
    "code",
    [
        ValidationCode.EMAIL_AUTH_FAILED,
        ValidationCode.EMAIL_PROVIDER_UNSUPPORTED,
        ValidationCode.EMAIL_SECURITY_CHALLENGE,
    ],
)
async def test_persistent_email_failure_disables_scheduled_active_account(
    session: AsyncSession,
    code: ValidationCode,
):
    acc, job = await _add_account_with_job(session)
    acc.status = "active"
    job.priority = "scheduled"
    await session.flush()
    worker = RefreshRecoveryWorker(check_delay_seconds=0)

    with patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as validate:
        validate.side_effect = AccountValidationError(
            ValidationStage.EMAIL_PREFLIGHT,
            code,
            "Почта требует вмешательства оператора.",
        )
        assert await worker.process_next(session) is True

    await session.refresh(acc)
    assert acc.status == "validation_failed"


async def test_cancelled_validation_is_durably_requeued(session: AsyncSession):
    _acc, job = await _add_account_with_job(session)
    worker = RefreshRecoveryWorker(check_delay_seconds=999)
    entered = asyncio.Event()

    async def wait_forever(*_args):
        entered.set()
        await asyncio.Future()

    with patch(
        "app.refresh_worker.validate_account",
        new=AsyncMock(side_effect=wait_forever),
    ):
        task = asyncio.create_task(worker.process_next(session))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await session.refresh(job)
    assert job.status == "pending"
    assert job.started_at is None
    assert job.result == "requeued_after_worker_cancelled"


async def test_refresh_failure_records_attempt_for_delayed_retry(
    session: AsyncSession,
):
    acc, job = await _add_account_with_job(session, "refresh_recover")
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="refresh",
        refresh_status="expired",
    )
    session.add(limits)
    await session.flush()
    worker = RefreshRecoveryWorker(max_attempts=3)
    with patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as validate:
        validate.side_effect = AccountValidationError(
            ValidationStage.LOGIN,
            ValidationCode.INVALID_CREDENTIALS,
            "bad login",
        )
        assert await worker.process_next(session) is True

    await session.refresh(job)
    await session.refresh(limits)
    assert job.status == "failed"
    assert limits.refresh_status == "expired"
    assert limits.refresh_recover_attempts == 1
    assert limits.refresh_last_recover_at is not None


async def test_refresh_job_stops_after_configured_max_attempts(
    session: AsyncSession,
):
    acc, job = await _add_account_with_job(session, "refresh_recover")
    session.add(AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="refresh",
        refresh_status="expired",
        refresh_recover_attempts=3,
        refresh_last_recover_at=datetime.now(timezone.utc),
    ))
    await session.flush()
    worker = RefreshRecoveryWorker(max_attempts=3)
    with patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as validate:
        assert await worker.process_next(session) is True

    validate.assert_not_awaited()
    await session.refresh(job)
    assert job.status == "failed"
    assert json.loads(job.error)["code"] == "refresh_recovery_exhausted"


async def test_limit_check_runs_inside_shared_account_job_queue(
    session: AsyncSession,
):
    acc, job = await _add_account_with_job(session, "limit_check")
    session.add(AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="refresh",
        refresh_status="ok",
    ))
    await session.flush()
    worker = RefreshRecoveryWorker()
    with (
        patch(
            "app.refresh_worker.measure_account_limits",
            new=AsyncMock(return_value=MeasureResult.OK),
        ) as measure,
        patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as validate,
    ):
        assert await worker.process_next(session) is True

    measure.assert_awaited_once_with(session, acc.id)
    validate.assert_not_awaited()
    await session.refresh(job)
    assert job.status == "done"
    assert job.result == "ok"


async def test_limit_check_refresh_expiry_enqueues_recovery_job(
    session: AsyncSession,
):
    acc, limit_job = await _add_account_with_job(session, "limit_check")
    session.add(AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="expired",
        refresh_status="ok",
    ))
    await session.flush()
    worker = RefreshRecoveryWorker()
    with patch(
        "app.refresh_worker.measure_account_limits",
        new=AsyncMock(return_value=MeasureResult.REFRESH_FAILED),
    ):
        assert await worker.process_next(session) is True

    jobs = (
        await session.execute(
            select(AccountCheckJob).where(AccountCheckJob.account_id == acc.id)
        )
    ).scalars().all()
    assert len(jobs) == 2
    assert limit_job.status == "failed"
    assert json.loads(limit_job.error)["code"] == "refresh_failed"
    recovery = next(job for job in jobs if job.id != limit_job.id)
    assert recovery.job_type == "refresh_recover"
    assert recovery.priority == "refresh_recover"
    assert recovery.status == "pending"


async def test_requested_validation_rerun_is_enqueued_after_current_job(
    session: AsyncSession,
):
    acc, current = await _add_account_with_job(session)
    acc.validation_rerun_requested = True
    await session.flush()
    worker = RefreshRecoveryWorker()

    with patch(
        "app.refresh_worker.validate_account",
        new=AsyncMock(return_value=ValidationOutcome.OK),
    ):
        assert await worker.process_next(session) is True

    await session.refresh(acc)
    jobs = (
        await session.execute(
            select(AccountCheckJob)
            .where(AccountCheckJob.account_id == acc.id)
            .order_by(AccountCheckJob.id)
        )
    ).scalars().all()
    assert acc.validation_rerun_requested is False
    assert acc.status == "pending_validation"
    assert [(job.status, job.priority, job.job_type) for job in jobs] == [
        ("done", "new", "full_validation"),
        ("pending", "manual", "full_validation"),
    ]
    assert current.id == jobs[0].id


async def test_low_long_window_warning_is_sent_once_per_reset(
    session: AsyncSession,
):
    acc, _job = await _add_account_with_job(session)
    reset_at = datetime.now(timezone.utc) + timedelta(days=6)
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="refresh",
        refresh_status="ok",
        plan_type="plus",
        plan_window_status="ok",
        expected_long_window_seconds=7 * 24 * 60 * 60,
        codex_secondary_remaining_pct=15,
        codex_secondary_window_seconds=7 * 24 * 60 * 60,
        codex_secondary_resets_at=reset_at,
        measured_at=datetime.now(timezone.utc),
    )
    session.add_all([
        limits,
        SellerSettings(
            id=1,
            limits_warn_threshold_pct=20,
            telegram_bot_token="token",
            telegram_seller_chat_id="chat",
        ),
    ])
    await session.flush()
    notifier = AsyncMock()
    notifier.notify_low_limits.return_value = True
    worker = RefreshRecoveryWorker()

    with patch(
        "app.refresh_worker.TelegramNotifier.from_settings",
        new=AsyncMock(return_value=notifier),
    ):
        await worker._warn_if_low_limits(session, acc.id)
        await worker._warn_if_low_limits(session, acc.id)

    notifier.notify_low_limits.assert_awaited_once_with(
        acc.login,
        remaining_pct=15,
        window_label="7 дней",
    )
    assert limits.low_limit_warning_fingerprint == (
        f"long:{7 * 24 * 60 * 60}:{reset_at.isoformat()}"
    )
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "account_low_limit_warning"
            )
        )
    ).scalar_one()
    assert audit.metadata_["remaining_pct"] == 15


async def test_low_limit_warning_retries_failed_telegram_delivery(
    session: AsyncSession,
):
    acc, _job = await _add_account_with_job(session)
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="refresh",
        refresh_status="ok",
        plan_type="free",
        plan_window_status="ok",
        expected_long_window_seconds=30 * 24 * 60 * 60,
        codex_primary_remaining_pct=10,
        codex_primary_window_seconds=30 * 24 * 60 * 60,
        measured_at=datetime.now(timezone.utc),
    )
    session.add_all([
        limits,
        SellerSettings(
            id=1,
            limits_warn_threshold_pct=20,
            telegram_bot_token="token",
            telegram_seller_chat_id="chat",
        ),
    ])
    await session.flush()
    notifier = AsyncMock()
    notifier.notify_low_limits.side_effect = [False, True]
    worker = RefreshRecoveryWorker()

    with patch(
        "app.refresh_worker.TelegramNotifier.from_settings",
        new=AsyncMock(return_value=notifier),
    ):
        await worker._warn_if_low_limits(session, acc.id)
        assert limits.low_limit_warning_fingerprint is None
        await worker._warn_if_low_limits(session, acc.id)

    assert notifier.notify_low_limits.await_count == 2
    assert limits.low_limit_warning_fingerprint == (
        f"long:{30 * 24 * 60 * 60}:unknown"
    )
