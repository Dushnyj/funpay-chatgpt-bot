from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings
from app.services.account_pool import AccountCriteria, AccountPool
from app.services.messages import render_message


class RentalService:
    """Связывает Order → Account → Rental → welcome message.

    fulfill_order: идемпотентен (если Rental для Order уже есть — возвращает существующий).
    Если аккаунт не найден — отправляет no_account_available, возвращает None.
    """

    def __init__(self, account_pool: AccountPool | None = None) -> None:
        self._pool = account_pool or AccountPool()

    async def fulfill_order(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: int,
        default_max_active_rentals: int,
    ) -> Rental | None:
        existing = await self._find_rental_by_order(session, order_id)
        if existing is not None:
            return existing

        order = await session.get(Order, order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found")

        duration = await session.get(Duration, order.duration_id)
        if duration is None:
            raise KeyError(f"Duration {order.duration_id} not found")

        scope = await session.get(LimitScope, order.limit_scope_id)
        scope_code = scope.code if scope else "any"

        criteria = AccountCriteria(
            tier_id=order.tier_id,
            duration_days=duration.days,
            scope=scope_code,
            min_limit_pct=order.min_limit_pct,
            max_5h_pct=order.max_5h_pct,
            max_weekly_pct=order.max_weekly_pct,
        )
        account = await self._pool.acquire(session, criteria, default_max_active_rentals)
        if account is None:
            await self._send_no_account(session, gateway, order)
            return None

        limits = await session.get(AccountLimits, account.id)
        now = datetime.now(timezone.utc)
        rental = Rental(
            order_id=order.id,
            account_id=account.id,
            buyer_funpay_id=order.buyer_funpay_id,
            buyer_funpay_chat_id=order.funpay_chat_id,
            tier_id=order.tier_id,
            duration_id=order.duration_id,
            limit_scope_id=order.limit_scope_id,
            min_limit_pct=order.min_limit_pct,
            max_5h_pct=order.max_5h_pct,
            max_weekly_pct=order.max_weekly_pct,
            lang=order.buyer_locale or "ru",
            started_at=now,
            expires_at=now + timedelta(days=duration.days),
            status="active",
            issued_chat_5h_pct=limits.chat_5h_remaining_pct if limits else None,
            issued_chat_weekly_pct=limits.chat_weekly_remaining_pct if limits else None,
            issued_codex_5h_pct=limits.codex_5h_remaining_pct if limits else None,
            issued_codex_weekly_pct=limits.codex_weekly_remaining_pct if limits else None,
        )
        session.add(rental)
        await session.flush()

        await self._send_welcome(session, gateway, order, account, limits, duration.days)
        return rental

    async def revoke_rental(self, session: AsyncSession, rental_id: int) -> Rental:
        rental = await session.get(Rental, rental_id)
        if rental is None:
            raise KeyError(f"Rental {rental_id} not found")
        rental.status = "revoked"
        await session.flush()
        return rental

    async def _find_rental_by_order(
        self, session: AsyncSession, order_id: int,
    ) -> Rental | None:
        result = await session.execute(
            select(Rental).where(Rental.order_id == order_id)
        )
        return result.scalar_one_or_none()

    async def _send_welcome(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order: Order,
        account: Account,
        limits: AccountLimits | None,
        days: int,
    ) -> None:
        tier = await session.get(SubscriptionTier, account.tier_id)
        # account.password_encrypted использует FernetEncrypted TypeDecorator:
        # ORM автоматически расшифровывает при чтении, отдавая plaintext.
        password = account.password_encrypted
        lang = order.buyer_locale or "ru"
        text = await render_message(
            session, "welcome", lang,
            login=account.login,
            password=password,
            tier=tier.name if tier else "",
            days=days,
            expires_at=_fmt_expires(account.subscription_expires_at),
            chat_5h=_pct(limits, "chat_5h") if limits else "—",
            chat_weekly=_pct(limits, "chat_weekly") if limits else "—",
            codex_5h=_pct(limits, "codex_5h") if limits else "—",
            codex_weekly=_pct(limits, "codex_weekly") if limits else "—",
        )
        await gateway.send_message(chat_id=int(order.funpay_chat_id), text=text)

    async def _send_no_account(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order: Order,
    ) -> None:
        lang = order.buyer_locale or "ru"
        retry_minutes = await self._retry_minutes(session)
        text = await render_message(
            session, "no_account_available", lang, retry_minutes=retry_minutes,
        )
        await gateway.send_message(chat_id=int(order.funpay_chat_id), text=text)

    @staticmethod
    async def _retry_minutes(session: AsyncSession) -> int:
        settings = await session.get(SellerSettings, 1)
        if settings is not None:
            return settings.limits_check_interval_minutes
        return 5


def _pct(limits: AccountLimits, field: str) -> str:
    val = getattr(limits, f"{field}_remaining_pct")
    return f"{val}%" if val is not None else "—"


def _fmt_expires(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y")
