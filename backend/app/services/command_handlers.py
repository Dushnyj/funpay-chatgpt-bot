from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.catalog import SubscriptionTier
from app.models.rental import Rental
from app.services.command_router import CommandContext
from app.services.messages import render_message
from app.services.totp import generate_totp


# Анти-спам для !код: не чаще раза в 30 секунд на одну аренду.
_CODE_RATE_LIMIT = timedelta(seconds=30)


def _as_utc(dt: datetime) -> datetime:
    """Нормализует datetime к tz-aware UTC.

    SQLite (aiosqlite) при чтении возвращает naive datetime, теряя tzinfo.
    `datetime.now(timezone.utc)` — aware. Без нормализации вычитание падает с
    TypeError: can't subtract offset-naive and offset-aware datetimes.
    Считаем, что naive значения хранятся в UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _session_from_ctx(ctx: CommandContext) -> AsyncSession:
    """Достаёт AsyncSession из контекста.

    CommandContext — frozen dataclass без session. Session注入ается внешним
    кодом через `object.__setattr__(ctx, "_session", session)` (см. тесты/wiring).
    """
    session = getattr(ctx, "_session", None)
    if session is None:
        raise RuntimeError("CommandContext has no _session attached")
    return session


def _pct_val(limits: AccountLimits | None, field: str) -> str:
    """Значение процента лимита для подстановки в шаблон.

    Возвращает только число (без `%`): символ `%` уже зашит в шаблонах,
    например `{chat_5h}%`. Добавление `%` здесь привело бы к `80%%`.
    """
    if limits is None:
        return "—"
    val = getattr(limits, f"{field}_remaining_pct", None)
    return str(val) if val is not None else "—"


def _fmt_date(dt: datetime | None) -> str:
    """Дата в формате DD.MM.YYYY для шаблонов подписки."""
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y")


def _fmt_remaining(expires_at: datetime, now: datetime) -> str:
    """Человекочитаемый остаток до окончания подписки/аренды.

    Формат выбирается по величине: минуты / часы / дни.
    """
    delta = _as_utc(expires_at) - _as_utc(now)
    if delta.total_seconds() <= 0:
        return "0"
    total_secs = int(delta.total_seconds())
    if total_secs < 3600:
        return f"{max(total_secs // 60, 1)}м"
    hours = total_secs // 3600
    if hours < 24:
        return f"{hours}ч"
    return f"{hours // 24}д"


async def _find_rental_by_chat(session: AsyncSession, chat_id: int) -> Rental | None:
    """Последняя аренда для чата FunPay (по started_at).

    Привязка покупателя идёт по buyer_funpay_chat_id (строка). Если аренд
    несколько — берётся последняя по started_at.
    """
    result = await session.execute(
        select(Rental).where(Rental.buyer_funpay_chat_id == str(chat_id))
    )
    rentals = result.scalars().all()
    if not rentals:
        return None
    return max(rentals, key=lambda r: r.started_at)


class CodeHandler:
    """Обработка !код/!code: выдача TOTP по активной аренде.

    Привязка по chat_id (не по логину). Анти-спам 30 сек между запросами.
    """

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        rental = await _find_rental_by_chat(session, ctx.chat_id)

        if rental is None or rental.status != "active":
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        now = datetime.now(timezone.utc)
        if rental.last_code_request_at is not None:
            elapsed = now - _as_utc(rental.last_code_request_at)
            if elapsed < _CODE_RATE_LIMIT:
                retry_in = int((_CODE_RATE_LIMIT - elapsed).total_seconds())
                text = await render_message(
                    session, "code_rate_limited", ctx.lang, retry_in_sec=retry_in,
                )
                await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
                return

        account = await session.get(Account, rental.account_id)
        if account is None:
            return
        # FernetEncrypted — TypeDecorator, расшифровывает при чтении.
        # НЕ вызываем decrypt(): значение уже plaintext.
        totp_secret = account.totp_secret_encrypted
        code = generate_totp(totp_secret)

        rental.last_code_request_at = now
        await session.flush()

        expires_in = _fmt_remaining(rental.expires_at, now)
        text = await render_message(
            session, "code_success", ctx.lang, code=code, expires_in=expires_in,
        )
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)


class HelpHandler:
    """Обработка !помощь/!help: отправка help template."""

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        text = await render_message(session, "help", ctx.lang)
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)


class SubscriptionHandler:
    """Обработка !подписка/!sub: показать тариф, срок и лимиты аккаунта."""

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        rental = await _find_rental_by_chat(session, ctx.chat_id)

        if rental is None or rental.status != "active":
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        account = await session.get(Account, rental.account_id)
        if account is None:
            return
        limits = await session.get(AccountLimits, account.id)
        tier = await session.get(SubscriptionTier, account.tier_id)

        now = datetime.now(timezone.utc)
        text = await render_message(
            session, "subscription", ctx.lang,
            tier=tier.name if tier else "",
            expires_at=_fmt_date(account.subscription_expires_at),
            expires_in=_fmt_remaining(rental.expires_at, now),
            chat_5h=_pct_val(limits, "chat_5h"),
            chat_weekly=_pct_val(limits, "chat_weekly"),
            codex_5h=_pct_val(limits, "codex_5h"),
            codex_weekly=_pct_val(limits, "codex_weekly"),
        )
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)


class SellerHandler:
    """Обработка !продавец/!seller: уведомление покупателю.

    Фаза 7 добавит реальную Telegram-нотификацию продавцу. Пока — отвечаем
    seller_called template, подтверждая что вызов принят.
    """

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        text = await render_message(session, "seller_called", ctx.lang)
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
