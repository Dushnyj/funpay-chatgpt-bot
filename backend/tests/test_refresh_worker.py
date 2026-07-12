from unittest.mock import AsyncMock, patch
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountCheckJob
from app.models.catalog import SubscriptionTier
from app.refresh_worker import RefreshRecoveryWorker
from app.services.account_validation import (
    AccountValidationError,
    ValidationCode,
    ValidationOutcome,
    ValidationStage,
)


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
    mock_validate.assert_awaited_once_with(session, acc.id)


async def test_process_next_returns_false_when_no_jobs(session: AsyncSession):
    worker = RefreshRecoveryWorker(check_delay_seconds=0)
    result = await worker.process_next(session)
    assert result is False


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
