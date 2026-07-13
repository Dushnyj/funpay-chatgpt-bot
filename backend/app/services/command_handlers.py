from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.email.provider import (
    EmailErrorCode,
    EmailProvider,
    EmailProviderError,
    FreshVerificationCode,
)
from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.audit import AuditLog
from app.models.catalog import SubscriptionTier
from app.models.rental import Order, Rental
from app.services.command_router import CommandContext
from app.services.messages import render_message, usage_template_variables
from app.services.totp import generate_totp
from app.telegram_notifier import TelegramNotifier


# Анти-спам для !код: не чаще раза в 30 секунд на одну аренду.
_CODE_RATE_LIMIT = timedelta(seconds=30)
_EMAIL_CODE_LOOKBACK = timedelta(minutes=10)
_EMAIL_CODE_AUDIT_EVENT = "buyer_email_code_delivered"
_TOTP_STEP_SECONDS = 30.0
_TOTP_MIN_VALIDITY_SECONDS = 12.0


class AmbiguousRentalError(RuntimeError):
    """A chat contains multiple active rentals and no order was selected."""


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


async def _find_rental_for_context(
    session: AsyncSession,
    ctx: CommandContext,
    *,
    for_update: bool = False,
) -> Rental | None:
    """Resolve the rental by order first; never guess between active rentals."""

    if ctx.order_id:
        stmt = (
            select(Rental)
            .join(Order, Order.id == Rental.order_id)
            .where(
                Rental.buyer_funpay_chat_id == str(ctx.chat_id),
                Order.funpay_order_id == ctx.order_id,
            )
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await session.execute(stmt)).scalar_one_or_none()

    active_stmt = (
        select(Rental)
        .where(
            Rental.buyer_funpay_chat_id == str(ctx.chat_id),
            Rental.status.in_(["active", "expiry_pending"]),
        )
        .order_by(Rental.started_at.desc(), Rental.id.desc())
        .limit(2)
    )
    if for_update:
        active_stmt = active_stmt.with_for_update().execution_options(
            populate_existing=True,
        )
    active = list((await session.execute(active_stmt)).scalars())
    if len(active) > 1:
        raise AmbiguousRentalError("multiple active rentals in one FunPay chat")
    if active:
        return active[0]

    latest_stmt = (
        select(Rental)
        .where(Rental.buyer_funpay_chat_id == str(ctx.chat_id))
        .order_by(Rental.started_at.desc(), Rental.id.desc())
        .limit(1)
    )
    if for_update:
        latest_stmt = latest_stmt.with_for_update().execution_options(
            populate_existing=True,
        )
    return (await session.execute(latest_stmt)).scalar_one_or_none()


async def _send_ambiguous_rental(ctx: CommandContext, session: AsyncSession) -> None:
    text = await render_message(session, "rental_ambiguous", ctx.lang)
    await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)


async def _wait_for_safe_totp_window(min_validity_seconds: float) -> None:
    if min_validity_seconds <= 0:
        return
    remaining = _TOTP_STEP_SECONDS - (time.time() % _TOTP_STEP_SECONDS)
    if remaining < min_validity_seconds:
        await asyncio.sleep(remaining + 0.05)


async def _expire_rental_if_due(
    session: AsyncSession,
    rental: Rental,
    now: datetime,
) -> bool:
    """Synchronously close an active rental at the authorization boundary."""
    order_status = await session.scalar(
        select(Order.status).where(Order.id == rental.order_id)
    )
    if order_status in {"refund_pending", "refunded"}:
        # Keep Rental.active while logout is retryable, but revoke buyer
        # command authorization immediately when payment is refunded.
        return True
    if rental.status != "active" or _as_utc(rental.expires_at) > _as_utc(now):
        return rental.status != "active"
    # Authorization stops immediately, while the expiry worker must still
    # revoke every OpenAI session before the terminal ``expired`` state.
    rental.status = "expiry_pending"
    session.add(AuditLog(
        event_type="rental_expired_on_command",
        account_id=rental.account_id,
        rental_id=rental.id,
        chat_id=rental.buyer_funpay_chat_id,
    ))
    await session.flush()
    return True


async def _email_code_was_delivered(
    session: AsyncSession,
    rental_id: int,
    fingerprint: str,
) -> bool:
    rows = (
        await session.execute(
            select(AuditLog.metadata_).where(
                AuditLog.event_type == _EMAIL_CODE_AUDIT_EVENT,
                AuditLog.rental_id == rental_id,
            )
        )
    ).scalars()
    return any(
        isinstance(metadata, dict)
        and metadata.get("fingerprint") == fingerprint
        for metadata in rows
    )


EmailProviderBuilder = Callable[
    [AsyncSession, Account, str | None, str | None],
    Awaitable[EmailProvider | None],
]


class CodeHandler:
    """Issue a labelled TOTP and an optional provably fresh email OTP."""

    def __init__(
        self,
        *,
        email_provider_builder: EmailProviderBuilder | None = None,
        email_timeout_s: float = 10.0,
        totp_min_validity_s: float = _TOTP_MIN_VALIDITY_SECONDS,
    ) -> None:
        self._email_provider_builder = email_provider_builder
        self._email_timeout_s = max(0.0, email_timeout_s)
        self._totp_min_validity_s = min(
            _TOTP_STEP_SECONDS - 1,
            max(0.0, totp_min_validity_s),
        )

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        # Do not hold a rental row lock while a mailbox provider performs
        # network I/O. The exact rental is locked and re-authorized below.
        try:
            rental = await _find_rental_for_context(session, ctx)
        except AmbiguousRentalError:
            await _send_ambiguous_rental(ctx, session)
            return
        now = datetime.now(timezone.utc)

        if (
            rental is None
            or await _expire_rental_if_due(session, rental, now)
        ):
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        if await self._send_rate_limit_if_needed(ctx, session, rental, now):
            return

        account = await session.get(Account, rental.account_id)
        if account is None:
            return
        account_id = account.id
        rental_id = rental.id

        # FernetEncrypted decrypts on read. The secret and generated codes are
        # kept in memory only and are never included in AuditLog metadata.
        email_code, email_state = await self._find_fresh_email_code(
            session,
            account,
            not_before=max(
                _as_utc(rental.started_at),
                now - _EMAIL_CODE_LOOKBACK,
            ),
        )

        # Mailbox access (notably Graph token rotation) may end a transaction.
        # Lock and re-authorize the exact rental immediately before disclosure.
        try:
            rental = await _find_rental_for_context(
                session, ctx, for_update=True,
            )
        except AmbiguousRentalError:
            await _send_ambiguous_rental(ctx, session)
            return
        now = datetime.now(timezone.utc)
        if (
            rental is None
            or rental.id != rental_id
            or rental.account_id != account_id
            or await _expire_rental_if_due(session, rental, now)
        ):
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return
        if await self._send_rate_limit_if_needed(ctx, session, rental, now):
            return

        if email_code is not None:
            if await _email_code_was_delivered(
                session, rental.id, email_code.fingerprint,
            ):
                email_code = None
                email_state = "duplicate"

        # A concurrent transaction may have delayed the row lock. Check the
        # real wall-clock boundary only now, immediately before generation.
        await _wait_for_safe_totp_window(self._totp_min_validity_s)
        now = datetime.now(timezone.utc)
        if await _expire_rental_if_due(session, rental, now):
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return
        if email_code is not None:
            session.add(AuditLog(
                event_type=_EMAIL_CODE_AUDIT_EVENT,
                account_id=account.id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={
                    "fingerprint": email_code.fingerprint,
                    "received_at": email_code.received_at.isoformat(),
                },
            ))
        rental.last_code_request_at = now
        await session.flush()
        totp_code = generate_totp(account.totp_secret_encrypted)
        labelled_totp = (
            f"TOTP (приложение): {totp_code}"
            if ctx.lang != "en"
            else f"TOTP (authenticator): {totp_code}"
        )
        text = await render_message(
            session,
            "code_success",
            ctx.lang,
            code=labelled_totp,
            expires_in=_fmt_remaining(rental.expires_at, now),
        )
        text += self._email_code_suffix(ctx.lang, email_code, email_state)
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)

    async def _send_rate_limit_if_needed(
        self,
        ctx: CommandContext,
        session: AsyncSession,
        rental: Rental,
        now: datetime,
    ) -> bool:
        if rental.last_code_request_at is None:
            return False
        elapsed = now - _as_utc(rental.last_code_request_at)
        if elapsed >= _CODE_RATE_LIMIT:
            return False
        retry_in = max(
            1,
            int((_CODE_RATE_LIMIT - elapsed).total_seconds()),
        )
        text = await render_message(
            session,
            "code_rate_limited",
            ctx.lang,
            retry_in_sec=retry_in,
        )
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
        return True

    async def _find_fresh_email_code(
        self,
        session: AsyncSession,
        account: Account,
        *,
        not_before: datetime,
    ) -> tuple[FreshVerificationCode | None, str]:
        if not account.email:
            return None, "unavailable"
        try:
            if self._email_provider_builder is None:
                from app.services.account_validation import _build_email_provider

                provider = await _build_email_provider(
                    session,
                    account,
                    account.email,
                    account.email_password_encrypted or None,
                )
            else:
                provider = await self._email_provider_builder(
                    session,
                    account,
                    account.email,
                    account.email_password_encrypted or None,
                )
            if provider is None:
                return None, "unavailable"
            code = await provider.fetch_fresh_verification_code(
                not_before=not_before,
                timeout=self._email_timeout_s,
            )
            if (
                code.received_at.tzinfo is None
                or _as_utc(code.received_at) < _as_utc(not_before)
                or not code.fingerprint
            ):
                return None, "not_found"
            return code, "found"
        except EmailProviderError as exc:
            if exc.code is EmailErrorCode.NO_CODE:
                return None, "not_found"
            return None, "seller_required"
        except Exception:
            return None, "seller_required"

    @staticmethod
    def _email_code_suffix(
        lang: str,
        email_code: FreshVerificationCode | None,
        state: str,
    ) -> str:
        if email_code is not None:
            if lang == "en":
                return f"\n\n📧 OpenAI email OTP: {email_code.code}"
            return f"\n\n📧 Email OTP OpenAI: {email_code.code}"
        if lang == "en":
            if state == "duplicate":
                return (
                    "\n\n📧 The latest email OTP was already delivered. "
                    "Request a new one in OpenAI, then use !code again."
                )
            if state == "not_found":
                return (
                    "\n\n📧 No fresh email OTP was found. If OpenAI asks for "
                    "one, contact the seller with !seller."
                )
            return (
                "\n\n📧 Mailbox access needs seller assistance: !seller."
            )
        if state == "duplicate":
            return (
                "\n\n📧 Последний email-код уже выдавался. Запросите новый "
                "код в OpenAI и повторите !код."
            )
        if state == "not_found":
            return (
                "\n\n📧 Свежий email-код не найден. Если OpenAI просит код "
                "из письма — вызовите продавца: !продавец."
            )
        return "\n\n📧 Для доступа к почте нужен продавец: !продавец."


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
        try:
            rental = await _find_rental_for_context(
                session, ctx, for_update=True,
            )
        except AmbiguousRentalError:
            await _send_ambiguous_rental(ctx, session)
            return
        now = datetime.now(timezone.utc)

        if (
            rental is None
            or await _expire_rental_if_due(session, rental, now)
        ):
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        account = await session.get(Account, rental.account_id)
        if account is None:
            return
        limits = await session.get(AccountLimits, account.id)
        tier = await session.get(SubscriptionTier, account.tier_id)

        text = await render_message(
            session, "subscription", ctx.lang,
            tier=tier.name if tier else "",
            expires_at=_fmt_date(account.subscription_expires_at),
            expires_in=_fmt_remaining(rental.expires_at, now),
            **usage_template_variables(limits, lang=ctx.lang),
        )
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)


class SellerHandler:
    """Обработка !продавец/!seller: Telegram + видимый чат в админке."""

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        notifier = await TelegramNotifier.from_settings(session)
        if notifier is not None:
            await notifier.notify_seller_called(
                str(ctx.sender_id),
                funpay_chat_id=str(ctx.chat_id),
                order_id=ctx.order_id,
            )
        text = await render_message(session, "seller_called", ctx.lang)
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)


from app.models.catalog import Duration, LimitScope
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
from app.services.rental_service import RentalService


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
        rental_service: RentalService | None = None,
        max_replacements: int = 1,
    ) -> None:
        self._pool = account_pool or AccountPool()
        self._validator = validator
        self._kick = kick_service or KickService()
        self._jobs = job_queue or CheckJobQueue()
        self._rentals = rental_service or RentalService()
        self._max_replacements = max(0, max_replacements)

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        try:
            rental = await _find_rental_for_context(
                session, ctx, for_update=True,
            )
        except AmbiguousRentalError:
            await _send_ambiguous_rental(ctx, session)
            return
        now = datetime.now(timezone.utc)
        original_rental_id = rental.id if rental is not None else None
        original_account_id = rental.account_id if rental is not None else None

        if rental is None:
            text = await render_message(session, "replace_declined", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return
        if await _expire_rental_if_due(session, rental, now):
            text = await render_message(session, "code_expired", ctx.lang)
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

        # Validation can persist an irreversible TOTP setup and therefore may
        # release the original database lock. Reacquire and repopulate the row
        # before any logout or replacement side effect.
        try:
            locked_rental = await _find_rental_for_context(
                session,
                ctx,
                for_update=True,
            )
        except AmbiguousRentalError:
            await _send_ambiguous_rental(ctx, session)
            return
        if (
            locked_rental is None
            or locked_rental.id != original_rental_id
            or locked_rental.account_id != original_account_id
        ):
            text = await render_message(session, "seller_called", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return
        rental = locked_rental
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

        # Re-authorize against the freshly locked state instead of trusting
        # scheduler lag or an ORM object retained across a commit.
        if await _expire_rental_if_due(
            session, rental, datetime.now(timezone.utc),
        ):
            text = await render_message(session, "code_expired", ctx.lang)
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

        # Kick and account selection can also be slow. This is the final
        # authorization boundary before the durable account_id switch.
        if await _expire_rental_if_due(
            session, rental, datetime.now(timezone.utc),
        ):
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        old_account_id = rental.account_id
        rental.account_id = new_account.id
        rental.replacement_count += 1
        rental.credentials_delivery_status = "sending"
        rental.credentials_delivery_template = "replace_success"
        rental.credentials_delivery_started_at = datetime.now(timezone.utc)
        rental.credentials_delivery_next_attempt_at = None
        rental.credentials_delivered_at = None
        rental.credentials_delivery_attempts += 1
        rental.credentials_delivery_last_error = None
        limits = await session.get(AccountLimits, new_account.id)
        if limits:
            rental.issued_chat_5h_pct = limits.chat_5h_remaining_pct
            rental.issued_chat_weekly_pct = limits.chat_weekly_remaining_pct
            rental.issued_codex_5h_pct = limits.codex_5h_remaining_pct
            rental.issued_codex_weekly_pct = limits.codex_weekly_remaining_pct
            rental.issued_codex_primary_pct = limits.codex_primary_remaining_pct
            rental.issued_codex_primary_window_seconds = (
                limits.codex_primary_window_seconds
            )
            rental.issued_codex_primary_resets_at = limits.codex_primary_resets_at
            rental.issued_codex_secondary_pct = limits.codex_secondary_remaining_pct
            rental.issued_codex_secondary_window_seconds = (
                limits.codex_secondary_window_seconds
            )
            rental.issued_codex_secondary_resets_at = (
                limits.codex_secondary_resets_at
            )
            rental.issued_plan_window_status = limits.plan_window_status
            rental.issued_expected_long_window_seconds = (
                limits.expected_long_window_seconds
            )
            rental.issued_limits_measured_at = limits.measured_at
        session.add(AuditLog(
            event_type="replacement_issued",
            account_id=new_account.id,
            rental_id=rental.id,
            chat_id=rental.buyer_funpay_chat_id,
            metadata_={"old_account_id": old_account_id},
        ))
        # Commit the exact replacement target and a durable delivery claim
        # before touching FunPay. A retry then sends this same account instead
        # of allocating another one or getting stuck behind replacement_count.
        await session.commit()
        await self._rentals.deliver_claimed_rental(
            session, ctx.gateway, rental.id,
        )
