from __future__ import annotations

from datetime import datetime, timedelta, timezone

from collections.abc import Awaitable, Callable

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


async def _find_rental_by_chat(
    session: AsyncSession,
    chat_id: int,
    *,
    for_update: bool = False,
) -> Rental | None:
    """Последняя аренда для чата FunPay (по started_at).

    Привязка покупателя идёт по buyer_funpay_chat_id (строка). Если аренд
    несколько — берётся последняя по started_at.
    """
    stmt = (
        select(Rental)
        .where(Rental.buyer_funpay_chat_id == str(chat_id))
        .order_by(Rental.started_at.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


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


from app.models.catalog import Duration, LimitScope
from app.models.audit import AuditLog
from app.models.settings import SellerSettings
from app.check_job_queue import CheckJobQueue
from app.services.account_validation import (
    AccountValidationError,
    ValidationCode,
    ValidationOutcome,
    validate_account,
)
from app.services.account_pool import AccountCriteria, AccountPool
from app.services.kick_service import KickResult, KickService


AccountValidator = Callable[[AsyncSession, int], Awaitable[ValidationOutcome]]
_REPLACEABLE_OUTCOMES = {
    ValidationOutcome.LOGIN_FAILED,
    ValidationOutcome.INVALID_2FA,
    ValidationOutcome.SETUP_2FA_FAILED,
}
_REPLACEABLE_VALIDATION_CODES = {
    ValidationCode.INVALID_CREDENTIALS.value,
    ValidationCode.INVALID_TOTP.value,
    ValidationCode.OAUTH_REJECTED.value,
}


class ReplaceHandler:
    """Обработка !замена/!replace: смена аккаунта на той же аренде.

    Rental.order_id имеет UNIQUE constraint — замена = смена account_id
    на существующей Rental (replacement_count++), а не создание новой.
    """

    def __init__(
        self,
        account_pool: AccountPool | None = None,
        *,
        validator: AccountValidator = validate_account,
        kick_service: KickService | None = None,
        job_queue: CheckJobQueue | None = None,
        max_replacements: int = 1,
    ) -> None:
        self._pool = account_pool or AccountPool()
        self._validator = validator
        self._kick = kick_service or KickService()
        self._jobs = job_queue or CheckJobQueue()
        self._max_replacements = max(0, max_replacements)

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        rental = await _find_rental_by_chat(session, ctx.chat_id, for_update=True)

        if rental is None or rental.status != "active":
            text = await render_message(session, "replace_declined", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        if rental.replacement_count >= self._max_replacements:
            session.add(AuditLog(
                event_type="replacement_limit_reached",
                account_id=rental.account_id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={"limit": self._max_replacements},
            ))
            await session.flush()
            text = await render_message(session, "seller_called", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        try:
            validation = await self._validator(session, rental.account_id)
        except AccountValidationError as exc:
            session.add(AuditLog(
                event_type="replacement_validation_failed",
                account_id=rental.account_id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={"stage": exc.stage, "code": exc.code},
            ))
            await session.flush()
            if exc.code not in _REPLACEABLE_VALIDATION_CODES:
                text = await render_message(session, "seller_called", ctx.lang)
                await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
                return
            validation = None
        except Exception as exc:
            session.add(AuditLog(
                event_type="replacement_validation_failed",
                account_id=rental.account_id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={"error_type": type(exc).__name__},
            ))
            await session.flush()
            text = await render_message(session, "seller_called", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        if validation is not None and validation not in _REPLACEABLE_OUTCOMES:
            session.add(AuditLog(
                event_type="replacement_declined",
                account_id=rental.account_id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={"validation": validation.value},
            ))
            await session.flush()
            template = (
                "replace_declined"
                if validation is ValidationOutcome.OK
                else "seller_called"
            )
            text = await render_message(session, template, ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        old_account = await session.get(Account, rental.account_id)
        if old_account is None:
            text = await render_message(session, "seller_called", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return
        old_account.status = "maintenance"

        # Revoke old credentials before exposing another account.  If revoke
        # cannot be confirmed, stop and ask the seller to intervene: otherwise
        # one buyer could retain credentials for multiple accounts.
        try:
            kick = await self._kick.kick(session, old_account.id)
        except Exception as exc:
            kick = KickResult(success=False, error=str(exc))
        session.add(AuditLog(
            event_type="replacement_kick",
            account_id=old_account.id,
            rental_id=rental.id,
            chat_id=rental.buyer_funpay_chat_id,
            metadata_={"success": kick.success, "error": kick.error},
        ))
        if not kick.success:
            await session.commit()
            text = await render_message(session, "seller_called", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        await self._jobs.enqueue(
            session,
            account_id=old_account.id,
            priority="refresh_recover",
            job_type="refresh_recover",
        )

        duration = await session.get(Duration, rental.duration_id)
        scope = await session.get(LimitScope, rental.limit_scope_id)
        if duration is None:
            return

        criteria = AccountCriteria(
            tier_id=rental.tier_id,
            duration_days=duration.days,
            scope=scope.code if scope else "any",
            min_limit_pct=rental.min_limit_pct,
            max_5h_pct=rental.max_5h_pct,
            max_weekly_pct=rental.max_weekly_pct,
        )
        settings = await session.get(SellerSettings, 1)
        default_max = settings.default_max_active_rentals if settings else 1
        new_account = await self._pool.acquire_excluding(
            session, criteria,
            exclude_account_id=rental.account_id,
            default_max_active_rentals=default_max,
        )

        if new_account is None:
            session.add(AuditLog(
                event_type="replacement_no_account",
                account_id=old_account.id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
            ))
            await session.commit()
            text = await render_message(session, "replace_no_account", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        old_account_id = rental.account_id
        rental.account_id = new_account.id
        rental.replacement_count += 1
        limits = await session.get(AccountLimits, new_account.id)
        if limits:
            rental.issued_chat_5h_pct = limits.chat_5h_remaining_pct
            rental.issued_chat_weekly_pct = limits.chat_weekly_remaining_pct
            rental.issued_codex_5h_pct = limits.codex_5h_remaining_pct
            rental.issued_codex_weekly_pct = limits.codex_weekly_remaining_pct
        session.add(AuditLog(
            event_type="replacement_issued",
            account_id=new_account.id,
            rental_id=rental.id,
            chat_id=rental.buyer_funpay_chat_id,
            metadata_={"old_account_id": old_account_id},
        ))
        # Commit before sending the credentials.  If delivery fails, a replay
        # sees replacement_count=1 and cannot disclose yet another account.
        await session.commit()

        # password_encrypted — уже plaintext через FernetEncrypted
        password = new_account.password_encrypted
        tier = await session.get(SubscriptionTier, new_account.tier_id)
        text = await render_message(
            session, "replace_success", ctx.lang,
            login=new_account.login,
            password=password,
            tier=tier.name if tier else "",
            days=duration.days,
            expires_at=_fmt_date(new_account.subscription_expires_at),
            chat_5h=_pct_val(limits, "chat_5h") if limits else "—",
            chat_weekly=_pct_val(limits, "chat_weekly") if limits else "—",
            codex_5h=_pct_val(limits, "codex_5h") if limits else "—",
            codex_weekly=_pct_val(limits, "codex_weekly") if limits else "—",
        )
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
