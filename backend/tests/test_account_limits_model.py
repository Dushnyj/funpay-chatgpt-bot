from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier


@pytest.mark.asyncio
async def test_account_limits_one_per_account(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="u@e.com", password_encrypted="p", totp_secret_encrypted="t",
        tier_id=tier.id, status="active",
    )
    session.add(acc)
    await session.flush()

    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted="rt-secret",
        access_token_encrypted="at-secret",
        access_token_expires_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        account_id_openai="acc-openai-123",
        codex_5h_remaining_pct=90,
        codex_weekly_remaining_pct=75,
        codex_primary_remaining_pct=90,
        codex_primary_window_seconds=18000,
        codex_primary_resets_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        codex_secondary_remaining_pct=75,
        codex_secondary_window_seconds=604800,
        codex_secondary_resets_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
        plan_type="plus",
        subscription_expires_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        measured_at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        refresh_status="ok",
    )
    session.add(limits)
    await session.commit()

    fetched = await session.execute(select(AccountLimits).where(AccountLimits.account_id == acc.id))
    reloaded = fetched.scalar_one()
    assert reloaded.refresh_token_encrypted == "rt-secret"
    assert reloaded.codex_primary_window_seconds == 18000
    assert reloaded.codex_secondary_window_seconds == 604800
    assert reloaded.refresh_status == "ok"
    assert reloaded.refresh_recover_attempts == 0  # default


@pytest.mark.asyncio
async def test_account_limits_unique_per_account(session):
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="u@e.com", password_encrypted="p", totp_secret_encrypted="t",
        tier_id=tier.id, status="active",
    )
    session.add(acc)
    await session.flush()

    l1 = AccountLimits(account_id=acc.id, refresh_token_encrypted="rt1")
    session.add(l1)
    await session.flush()

    l2 = AccountLimits(account_id=acc.id, refresh_token_encrypted="rt2")
    session.add(l2)
    with pytest.raises(IntegrityError):
        await session.flush()
