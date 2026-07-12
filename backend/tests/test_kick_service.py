from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.catalog import SubscriptionTier
from app.services.kick_service import KickService, KickResult


async def _add_account(session: AsyncSession, login: str = "acc1") -> Account:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    acc = Account(
        login=login,
        password_encrypted="plain_pass",
        totp_secret_encrypted="plain_totp",
        tier_id=tier.id,
        status="active",
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(acc)
    await session.flush()
    return acc


async def test_kick_account_success(session: AsyncSession):
    acc = await _add_account(session)
    svc = KickService()
    with patch("app.services.kick_service.browser_context") as mock_ctx, \
         patch("app.services.kick_service.kick_account", new_callable=AsyncMock) as mock_kick:
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await svc.kick(session, acc.id)
    assert result.success is True
    mock_kick.assert_awaited_once()


async def test_kick_account_failure_returns_error(session: AsyncSession):
    acc = await _add_account(session)
    svc = KickService()
    with patch("app.services.kick_service.browser_context") as mock_ctx, \
         patch("app.services.kick_service.kick_account", new_callable=AsyncMock) as mock_kick:
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_kick.side_effect = RuntimeError("login failed")
        result = await svc.kick(session, acc.id)
    assert result.success is False
    assert "login failed" in (result.error or "")


async def test_kick_dedup_skips_within_60_seconds(session: AsyncSession):
    acc = await _add_account(session)
    svc = KickService()
    with patch("app.services.kick_service.browser_context") as mock_ctx, \
         patch("app.services.kick_service.kick_account", new_callable=AsyncMock) as mock_kick:
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        await svc.kick(session, acc.id)
        result2 = await svc.kick(session, acc.id)
    assert result2.success is True
    assert result2.deduplicated is True
    mock_kick.assert_awaited_once()


async def test_kick_unknown_account_raises(session: AsyncSession):
    svc = KickService()
    with pytest.raises(KeyError):
        await svc.kick(session, 99999)
