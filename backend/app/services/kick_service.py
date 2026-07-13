from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.playwright.browser import browser_context
from app.integrations.playwright.kick import kick_account
from app.models.account import Account
from app.services.account_validation import _build_email_provider


_KICK_LOCKS: dict[int, asyncio.Lock] = {}


@dataclass(frozen=True)
class KickResult:
    """Результат операции kick."""

    success: bool
    deduplicated: bool = False
    error: str | None = None


class KickService:
    """Logout all through Playwright, serialized per account in this process.

    Every revoke request executes. A time-window no-op is unsafe because the
    buyer may log in again immediately after a previous successful logout.
    """

    async def kick(self, session: AsyncSession, account_id: int) -> KickResult:
        lock = _KICK_LOCKS.setdefault(account_id, asyncio.Lock())
        async with lock:
            return await self._kick_once(session, account_id)

    async def _kick_once(
        self,
        session: AsyncSession,
        account_id: int,
    ) -> KickResult:
        account = await session.get(Account, account_id)
        if account is None:
            raise KeyError(f"Account {account_id} not found")

        # FernetEncrypted TypeDecorator уже дешифрует при чтении — НЕ вызываем decrypt()
        password = account.password_encrypted
        totp_secret = account.totp_secret_encrypted

        try:
            email_provider = await _build_email_provider(
                session,
                account,
                account.email,
                account.email_password_encrypted or None,
            )
            async with browser_context() as context:
                await kick_account(
                    context=context,
                    login=account.login,
                    password=password,
                    totp_secret=totp_secret,
                    email_provider=email_provider,
                )
        except Exception as exc:
            return KickResult(success=False, error=str(exc))

        return KickResult(success=True)
