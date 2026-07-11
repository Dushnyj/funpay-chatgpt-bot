from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.account import Account, AccountCheckJob
from app.models.catalog import SubscriptionTier


@pytest.mark.asyncio
async def test_create_check_job_defaults(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="u@e.com", password_encrypted="p", totp_secret_encrypted="t",
        tier_id=tier.id, status="active",
    )
    session.add(acc)
    await session.flush()

    job = AccountCheckJob(
        account_id=acc.id,
        priority="new",
        job_type="full_validation",
    )
    session.add(job)
    await session.commit()

    fetched = await session.execute(select(AccountCheckJob).where(AccountCheckJob.account_id == acc.id))
    reloaded = fetched.scalar_one()
    assert reloaded.status == "pending"
    assert reloaded.priority == "new"
    assert reloaded.job_type == "full_validation"
    assert reloaded.result is None
    assert reloaded.error is None
    # created_at заполняется default-лямбдой
    assert reloaded.created_at is not None
    # Гарантия, что default использует timezone-aware UTC
    assert reloaded.created_at.tzinfo is not None or reloaded.created_at.utcoffset() is not None
    _ = timezone  # помечаем использование импорта (timezone доступен в модуле)
    _ = datetime
