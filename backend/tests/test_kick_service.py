from datetime import datetime, timedelta, timezone
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    provider = object()
    with patch("app.services.kick_service._build_email_provider", new=AsyncMock(return_value=provider)), \
         patch("app.services.kick_service.browser_context") as mock_ctx, \
         patch("app.services.kick_service.kick_account", new_callable=AsyncMock) as mock_kick:
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await svc.kick(session, acc.id)
    assert result.success is True
    mock_kick.assert_awaited_once()
    assert mock_kick.await_args.kwargs["email_provider"] is provider


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


async def test_kick_repairs_failed_database_transaction(session: AsyncSession):
    acc = await _add_account(session)
    account_id = acc.id
    login = acc.login
    await session.commit()

    async def poison_transaction(db, *_args):
        db.add(Account(
            login=login,
            password_encrypted="duplicate",
            totp_secret_encrypted="",
            status="active",
        ))
        await db.flush()

    with patch(
        "app.services.kick_service._build_email_provider",
        side_effect=poison_transaction,
    ):
        result = await KickService().kick(session, account_id)

    assert result.success is False
    assert session.is_active
    assert await session.get(Account, account_id) is not None


async def test_sequential_kicks_always_revoke_again(session: AsyncSession):
    acc = await _add_account(session)
    svc = KickService()
    with patch("app.services.kick_service.browser_context") as mock_ctx, \
         patch("app.services.kick_service.kick_account", new_callable=AsyncMock) as mock_kick:
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        await svc.kick(session, acc.id)
        result2 = await svc.kick(session, acc.id)
    assert result2.success is True
    assert result2.deduplicated is False
    assert mock_kick.await_count == 2


async def test_kick_unknown_account_raises(session: AsyncSession):
    svc = KickService()
    with pytest.raises(KeyError):
        await svc.kick(session, 99999)


async def test_kick_uses_postgres_transaction_advisory_lock():
    session = MagicMock()
    session.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql")
    )
    session.execute = AsyncMock()
    svc = KickService()
    svc._kick_once = AsyncMock(return_value=KickResult(success=True))

    result = await svc.kick(session, 42)

    assert result.success is True
    session.execute.assert_awaited_once()
    statement, parameters = session.execute.await_args.args
    assert "pg_advisory_xact_lock" in str(statement)
    assert parameters["account_id"] == 42


async def test_kick_timeout_rolls_back_browser_transaction(monkeypatch):
    import app.services.kick_service as kick_service

    session = MagicMock()
    session.rollback = AsyncMock()
    svc = KickService()

    async def never_finishes(_session, _account_id):
        await asyncio.sleep(60)

    svc._kick_serialized = never_finishes
    monkeypatch.setattr(kick_service, "KICK_TIMEOUT_SECONDS", 0.01)

    result = await svc.kick(session, 43)

    assert result.success is False
    assert "timeout" in (result.error or "")
    session.rollback.assert_awaited_once()
