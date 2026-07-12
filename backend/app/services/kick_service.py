from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.playwright.browser import browser_context
from app.integrations.playwright.kick import kick_account
from app.models.account import Account


_KICK_DEDUP_WINDOW = timedelta(seconds=60)


@dataclass(frozen=True)
class KickResult:
    """Результат операции kick."""

    success: bool
    deduplicated: bool = False
    error: str | None = None


class KickService:
    """Logout all через Playwright с дедупликацией 60 сек.

    Дедупликация in-memory: повторный kick аккаунта в пределах окна — no-op.
    """

    def __init__(self) -> None:
        self._last_kick_at: dict[int, datetime] = {}

    async def kick(self, session: AsyncSession, account_id: int) -> KickResult:
        now = datetime.now(timezone.utc)
        last = self._last_kick_at.get(account_id)
        if last is not None and now - last < _KICK_DEDUP_WINDOW:
            return KickResult(success=True, deduplicated=True)

        account = await session.get(Account, account_id)
        if account is None:
            raise KeyError(f"Account {account_id} not found")

        # FernetEncrypted TypeDecorator уже дешифрует при чтении — НЕ вызываем decrypt()
        password = account.password_encrypted
        totp_secret = account.totp_secret_encrypted

        try:
            async with browser_context() as context:
                await kick_account(
                    context=context,
                    login=account.login,
                    password=password,
                    totp_secret=totp_secret,
                )
        except Exception as exc:
            return KickResult(success=False, error=str(exc))

        self._last_kick_at[account_id] = now
        return KickResult(success=True)
