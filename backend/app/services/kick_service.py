from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.playwright.browser import browser_context
from app.integrations.playwright.kick import kick_account
from app.models.account import Account
from app.services.account_validation import _build_email_provider


_KICK_LOCKS: dict[int, asyncio.Lock] = {}
_KICK_ADVISORY_NAMESPACE = 0x46504B43  # "FPKC", signed PostgreSQL int32.
KICK_TIMEOUT_SECONDS = 240.0


@dataclass(frozen=True)
class KickResult:
    """Результат операции kick."""

    success: bool
    deduplicated: bool = False
    error: str | None = None


class KickService:
    """Logout all through Playwright, serialized per account everywhere.

    Every revoke request executes. A time-window no-op is unsafe because the
    buyer may log in again immediately after a previous successful logout.
    PostgreSQL's transaction-scoped advisory lock complements the local lock,
    so several uvicorn workers or containers cannot revoke the same account
    out of order. Callers commit/rollback immediately after ``kick`` and thus
    release that lock before their short database finalization phase.
    """

    async def kick(self, session: AsyncSession, account_id: int) -> KickResult:
        lock = _KICK_LOCKS.setdefault(account_id, asyncio.Lock())
        async with lock:
            try:
                return await asyncio.wait_for(
                    self._kick_serialized(session, account_id),
                    timeout=KICK_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # The durable rental/refund claim was committed by the caller
                # before this method. Only the browser/advisory-lock
                # transaction is discarded here, leaving the claim retryable.
                await session.rollback()
                return KickResult(
                    success=False,
                    error=(
                        "Account session revoke exceeded the safe "
                        f"{int(KICK_TIMEOUT_SECONDS)} second timeout"
                    ),
                )

    async def _kick_serialized(
        self,
        session: AsyncSession,
        account_id: int,
    ) -> KickResult:
        await self._acquire_database_lock(session, account_id)
        return await self._kick_once(session, account_id)

    @staticmethod
    async def _acquire_database_lock(
        session: AsyncSession,
        account_id: int,
    ) -> None:
        """Serialize account-wide logout across PostgreSQL processes.

        SQLite is used only by the single-process test suite, where the local
        ``asyncio.Lock`` above provides the equivalent serialization.
        """

        if session.get_bind().dialect.name != "postgresql":
            return
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock(:namespace, :account_id)"
            ),
            {
                "namespace": _KICK_ADVISORY_NAMESPACE,
                "account_id": account_id,
            },
        )

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
            # Provider/DB setup failures can leave SQLAlchemy in partial
            # rollback. Preserve a healthy transaction (it may contain a
            # rotated Graph token), but repair a poisoned one so the caller
            # can durably audit this retryable revoke failure.
            if not session.is_active:
                await session.rollback()
            return KickResult(success=False, error=str(exc))

        return KickResult(success=True)
