from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from collections.abc import Awaitable, Callable

from sqlalchemy import and_, or_, select
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
from app.models.funpay_sale import FunPaySale
from app.models.rental import Order, Rental
from app.services.command_router import CommandContext
from app.services.account_limits import MeasureResult, measure_account_limits
from app.services.durations import (
    format_access_expiry,
    format_plan_expiry,
    format_remaining_seconds,
)
from app.services.messages import render_message, usage_template_variables
from app.services.totp import generate_totp
from app.telegram_notifier import TelegramNotifier


# Анти-спам для !код: не чаще раза в 30 секунд на одну аренду.
_CODE_RATE_LIMIT = timedelta(seconds=30)
_EMAIL_CODE_LOOKBACK = timedelta(minutes=10)
_EMAIL_CODE_AUDIT_EVENT = "buyer_email_code_delivered"
_TOTP_STEP_SECONDS = 30.0
_TOTP_MIN_VALIDITY_SECONDS = 20.0
_CODE_DISCLOSURE_MIN_REMAINING = timedelta(seconds=60)
_CODE_SEND_TIMEOUT_SECONDS = 10.0


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


def _fmt_date(dt: datetime | None, lang: str) -> str:
    """Дата в формате DD.MM.YYYY для шаблонов подписки."""
    return format_plan_expiry(dt, lang)


def _fmt_remaining(expires_at: datetime, now: datetime, lang: str = "ru") -> str:
    """Человекочитаемый остаток до окончания подписки/аренды.

    Формат выбирается по величине: минуты / часы / дни.
    """
    delta = _as_utc(expires_at) - _as_utc(now)
    return format_remaining_seconds(delta.total_seconds(), lang)


async def _find_rental_for_context(
    session: AsyncSession,
    ctx: CommandContext,
    *,
    for_update: bool = False,
) -> Rental | None:
    """Resolve the rental by order first; never guess between active rentals."""

    verified_buyer_fallback = and_(
        Rental.buyer_funpay_id == str(ctx.sender_id),
        select(FunPaySale.id)
        .where(
            FunPaySale.funpay_order_id == Order.funpay_order_id,
            FunPaySale.buyer_funpay_id == str(ctx.sender_id),
            FunPaySale.funpay_chat_id == str(ctx.chat_id),
        )
        .exists(),
    )
    context_identity = or_(
        Rental.buyer_funpay_chat_id == str(ctx.chat_id),
        verified_buyer_fallback,
    )

    if ctx.order_id:
        stmt = (
            select(Rental)
            .join(Order, Order.id == Rental.order_id)
            .where(
                context_identity,
                Order.funpay_order_id == ctx.order_id,
            )
            .limit(1)
        )
        resolved = (await session.execute(stmt)).scalar_one_or_none()
        if not for_update or resolved is None:
            return resolved
        return await _lock_resolved_rental(session, ctx, resolved)

    active_stmt = (
        select(Rental)
        .join(Order, Order.id == Rental.order_id)
        .where(
            context_identity,
            Rental.status.in_(["active", "expiry_pending"]),
        )
        .order_by(Rental.started_at.desc(), Rental.id.desc())
        .limit(2)
    )
    active = list((await session.execute(active_stmt)).scalars())
    if len(active) > 1:
        raise AmbiguousRentalError("multiple active rentals in one FunPay chat")
    if active:
        return (
            await _lock_resolved_rental(session, ctx, active[0])
            if for_update
            else active[0]
        )

    latest_stmt = (
        select(Rental)
        .join(Order, Order.id == Rental.order_id)
        .where(context_identity)
        .order_by(Rental.started_at.desc(), Rental.id.desc())
        .limit(1)
    )
    resolved = (await session.execute(latest_stmt)).scalar_one_or_none()
    if not for_update or resolved is None:
        return resolved
    return await _lock_resolved_rental(session, ctx, resolved)


async def _lock_resolved_rental(
    session: AsyncSession,
    ctx: CommandContext,
    resolved: Rental,
) -> Rental | None:
    """Lock Order -> Rental explicitly; never rely on JOIN planner order."""

    order = (
        await session.execute(
            select(Order)
            .where(Order.id == resolved.order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if order is None:
        return None
    rental = (
        await session.execute(
            select(Rental)
            .where(Rental.id == resolved.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if rental is None:
        return None
    verified_buyer_fallback = await session.scalar(
        select(FunPaySale.id).where(
            FunPaySale.funpay_order_id == order.funpay_order_id,
            FunPaySale.buyer_funpay_id == str(ctx.sender_id),
            FunPaySale.funpay_chat_id == str(ctx.chat_id),
        )
    )
    identity_matches = (
        rental.buyer_funpay_chat_id == str(ctx.chat_id)
        or (
            rental.buyer_funpay_id == str(ctx.sender_id)
            and verified_buyer_fallback is not None
        )
    )
    if (
        rental.order_id != order.id
        or not identity_matches
        or (ctx.order_id is not None and order.funpay_order_id != ctx.order_id)
    ):
        return None
    if ctx.order_id is None:
        active_ids = list((await session.execute(
            select(Rental.id)
            .join(Order, Order.id == Rental.order_id)
            .where(
                or_(
                    Rental.buyer_funpay_chat_id == str(ctx.chat_id),
                    and_(
                        Rental.buyer_funpay_id == str(ctx.sender_id),
                        select(FunPaySale.id)
                        .where(
                            FunPaySale.funpay_order_id == Order.funpay_order_id,
                            FunPaySale.buyer_funpay_id == str(ctx.sender_id),
                            FunPaySale.funpay_chat_id == str(ctx.chat_id),
                        )
                        .exists(),
                    ),
                ),
                Rental.status.in_(["active", "expiry_pending"]),
            )
            .order_by(Rental.started_at.desc(), Rental.id.desc())
            .limit(2)
        )).scalars())
        if len(active_ids) > 1:
            raise AmbiguousRentalError(
                "multiple active rentals in one FunPay chat"
            )
        if active_ids and rental.id != active_ids[0]:
            return None
    return rental


async def _send_bounded_after_transaction(
    ctx: CommandContext,
    session: AsyncSession,
    text: str,
) -> None:
    """Release database locks before a potentially slow non-secret reply."""

    if session.in_transaction():
        await session.commit()
    await asyncio.wait_for(
        ctx.gateway.send_message(chat_id=ctx.chat_id, text=text),
        timeout=_CODE_SEND_TIMEOUT_SECONDS,
    )


async def _send_ambiguous_rental(ctx: CommandContext, session: AsyncSession) -> None:
    text = await render_message(session, "rental_ambiguous", ctx.lang)
    await _send_bounded_after_transaction(ctx, session, text)


def _totp_window_remaining() -> float:
    return _TOTP_STEP_SECONDS - (time.time() % _TOTP_STEP_SECONDS)


async def _wait_for_safe_totp_window(min_validity_seconds: float) -> None:
    if min_validity_seconds <= 0:
        return
    remaining = _totp_window_remaining()
    if remaining < min_validity_seconds:
        await asyncio.sleep(remaining + 0.05)


async def _expire_rental_if_due(
    session: AsyncSession,
    rental: Rental,
    now: datetime,
) -> bool:
    """Synchronously close an active rental at the authorization boundary."""
    if (
        rental.credentials_delivery_template == "welcome"
        and rental.credentials_delivery_status != "sent"
        and rental.credentials_delivery_attempts == 0
    ):
        # The provisional expiry only reserves capacity while delivery is
        # pending. The paid access term has not started and must not expire.
        return False
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


async def _rental_access_denial(
    session: AsyncSession,
    rental: Rental | None,
    now: datetime,
) -> str | None:
    """Return the buyer-facing denial template for one authorization check."""

    if rental is None:
        return "code_expired"
    if rental.credentials_delivery_status != "sent":
        return "delivery_pending"
    if await _expire_rental_if_due(session, rental, now):
        return "code_expired"
    return None


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


async def _code_account_if_available(
    session: AsyncSession,
    account_id: int,
    *,
    for_update: bool = False,
) -> Account | None:
    """Code disclosure requires an operator-approved active account."""

    stmt = select(Account).where(
        Account.id == account_id,
        Account.status == "active",
        Account.operator_status_override.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (
        await session.execute(
            stmt.execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


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

        denial_template = await _rental_access_denial(
            session, rental, now,
        )
        if denial_template is not None:
            await self._send_non_secret(ctx, session, denial_template)
            return

        account = await _code_account_if_available(session, rental.account_id)
        if account is None:
            await self._send_non_secret(
                ctx, session, "account_unavailable",
            )
            return

        if await self._send_rate_limit_if_needed(ctx, session, rental, now):
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
        # A TOTP boundary wait must never hold Order/Rental locks: refunds and
        # expiry are allowed to win while no secret is being disclosed.
        if session.in_transaction():
            await session.commit()

        while True:
            await _wait_for_safe_totp_window(self._totp_min_validity_s)

            # Lock order is Order -> Rental -> Account, matching refund and
            # expiry paths. Re-authorize the exact sale after every wait.
            try:
                rental = await _find_rental_for_context(
                    session, ctx, for_update=True,
                )
            except AmbiguousRentalError:
                await _send_ambiguous_rental(ctx, session)
                return
            now = datetime.now(timezone.utc)
            identity_changed = (
                rental is None
                or rental.id != rental_id
                or rental.account_id != account_id
            )
            denial_template = (
                "code_expired"
                if identity_changed
                else await _rental_access_denial(session, rental, now)
            )
            if denial_template is not None:
                await self._send_non_secret(
                    ctx, session, denial_template,
                )
                return
            if await self._send_rate_limit_if_needed(
                ctx, session, rental, now,
            ):
                return

            # Waiting for the row locks can itself consume the safe TOTP
            # window. Release them and retry instead of sleeping under locks.
            if (
                self._totp_min_validity_s > 0
                and _totp_window_remaining() < self._totp_min_validity_s
            ):
                await session.commit()
                continue

            if email_code is not None and await _email_code_was_delivered(
                session, rental.id, email_code.fingerprint,
            ):
                email_code = None
                email_state = "duplicate"
            if _as_utc(rental.expires_at) - now <= _CODE_DISCLOSURE_MIN_REMAINING:
                await self._send_non_secret(
                    ctx, session, "code_expiring",
                )
                return
            account = await _code_account_if_available(
                session, account_id, for_update=True,
            )
            if account is None:
                await self._send_non_secret(
                    ctx, session, "account_unavailable",
                )
                return
            break
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
            expires_in=_fmt_remaining(rental.expires_at, now, ctx.lang),
        )
        email_text = await self._render_email_code_status(
            session, ctx.lang, email_code, email_state,
        )
        text += f"\n\n{email_text}"
        await asyncio.wait_for(
            ctx.gateway.send_message(chat_id=ctx.chat_id, text=text),
            timeout=_CODE_SEND_TIMEOUT_SECONDS,
        )

    async def _send_non_secret(
        self,
        ctx: CommandContext,
        session: AsyncSession,
        template: str,
        **variables: object,
    ) -> None:
        text = await render_message(
            session, template, ctx.lang, **variables,
        )
        await _send_bounded_after_transaction(ctx, session, text)

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
        await _send_bounded_after_transaction(ctx, session, text)
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
    async def _render_email_code_status(
        session: AsyncSession,
        lang: str,
        email_code: FreshVerificationCode | None,
        state: str,
    ) -> str:
        if email_code is not None:
            return await render_message(
                session,
                "email_code_success",
                lang,
                email_code=email_code.code,
            )
        template = {
            "duplicate": "email_code_duplicate",
            "not_found": "email_code_not_found",
        }.get(state, "email_code_unavailable")
        return await render_message(session, template, lang)


class HelpHandler:
    """Обработка !помощь/!help: отправка help template."""

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        text = await render_message(session, "help", ctx.lang)
        await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)


LimitsRefresher = Callable[[AsyncSession, int], Awaitable[MeasureResult]]


class SubscriptionHandler:
    """Обработка !подписка/!sub: показать тариф, срок и лимиты аккаунта."""

    def __init__(
        self,
        *,
        refresher: LimitsRefresher = measure_account_limits,
        refresh_timeout_s: float = 15.0,
    ) -> None:
        self._refresher = refresher
        self._refresh_timeout_s = max(0.1, refresh_timeout_s)

    async def __call__(self, ctx: CommandContext) -> None:
        session = _session_from_ctx(ctx)
        try:
            rental = await _find_rental_for_context(session, ctx)
        except AmbiguousRentalError:
            await _send_ambiguous_rental(ctx, session)
            return
        now = datetime.now(timezone.utc)

        denial_template = await _rental_access_denial(
            session, rental, now,
        )
        if denial_template is not None:
            text = await render_message(session, denial_template, ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        rental_id = rental.id
        account_id = rental.account_id
        # Establish a clean transaction boundary before the refresh helper,
        # which may commit internally, time out, or leave its transaction in a
        # failed state. No row lock is held here.
        await session.commit()
        try:
            refresh_result = await asyncio.wait_for(
                self._refresher(session, account_id),
                timeout=self._refresh_timeout_s,
            )
        except Exception:
            # A cancelled/failed refresher may leave an open or failed
            # transaction. Reset it before the authoritative requery.
            await session.rollback()
            refresh_result = None

        # The network refresh may commit or run long. Reacquire the exact
        # rental and re-authorize after it, without holding a row lock over I/O.
        try:
            rental = await _find_rental_for_context(
                session, ctx, for_update=True,
            )
        except AmbiguousRentalError:
            await _send_ambiguous_rental(ctx, session)
            return
        now = datetime.now(timezone.utc)
        identity_changed = (
            rental is None
            or rental.id != rental_id
            or rental.account_id != account_id
        )
        denial_template = (
            "code_expired"
            if identity_changed
            else await _rental_access_denial(session, rental, now)
        )
        if denial_template is not None:
            text = await render_message(session, denial_template, ctx.lang)
            await session.commit()
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        account = (
            await session.execute(
                select(Account)
                .where(Account.id == account_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if account is None:
            return
        limits = (
            await session.execute(
                select(AccountLimits)
                .where(AccountLimits.account_id == account.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        tier = await session.get(SubscriptionTier, account.tier_id)

        limits_are_current = (
            refresh_result is MeasureResult.OK
            and limits is not None
            and limits.refresh_status == "ok"
            and limits.plan_window_status == "ok"
        )
        template_key = (
            "subscription"
            if limits_are_current
            else "subscription_limits_unavailable"
        )
        variables = dict(
            tier=tier.name if tier else "",
            expires_at=_fmt_date(account.subscription_expires_at, ctx.lang),
            access_expires_at=format_access_expiry(
                rental.expires_at, ctx.lang
            ),
            expires_in=_fmt_remaining(rental.expires_at, now, ctx.lang),
        )
        if template_key == "subscription":
            variables.update(usage_template_variables(limits, lang=ctx.lang))
        text = await render_message(
            session, template_key, ctx.lang, **variables,
        )
        await session.commit()
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
from app.services.rental_service import (
    REPLACEMENT_DELIVERY_MIN_REMAINING,
    RentalService,
)


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
_REPLACEMENT_REVOKE_LEASE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class _ReplacementRevokeClaim:
    order_id: int
    rental_id: int
    account_id: int
    target_account_id: int
    started_at: datetime


def _replacement_window_is_too_short(
    rental: Rental,
    now: datetime,
) -> bool:
    return (
        _as_utc(rental.expires_at) - _as_utc(now)
        <= REPLACEMENT_DELIVERY_MIN_REMAINING
    )


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
            # Validation may use browser/email network I/O. Resolve a snapshot
            # first and acquire canonical Order -> Rental locks only after it.
            rental = await _find_rental_for_context(session, ctx)
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
        denial_template = await _rental_access_denial(session, rental, now)
        if denial_template is not None:
            text = await render_message(session, denial_template, ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return
        if _replacement_window_is_too_short(rental, now):
            text = await render_message(session, "replace_expiring", ctx.lang)
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

        existing_claim = rental.expiry_revoke_started_at
        if existing_claim is not None:
            existing_claim = _as_utc(existing_claim)
        if (
            existing_claim is not None
            and existing_claim > now - _REPLACEMENT_REVOKE_LEASE
        ):
            # Fast path for a duplicate command while another replacement,
            # refund or expiry worker owns the shared logout lease. The locked
            # claim phase below repeats this check to close the race.
            text = await render_message(session, "seller_called", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        if rental.replacement_target_account_id is not None:
            released = await self._release_stale_replacement_reservation(
                session,
                ctx,
                rental_id=rental.id,
                account_id=rental.account_id,
                observed_claim=existing_claim,
                observed_target_account_id=(
                    rental.replacement_target_account_id
                ),
            )
            if not released:
                text = await render_message(
                    session, "seller_called", ctx.lang,
                )
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

        claim, denial_template = await self._claim_replacement_revoke(
            session,
            ctx,
            original_rental_id=original_rental_id,
            original_account_id=original_account_id,
        )
        if claim is None:
            text = await render_message(
                session, denial_template or "seller_called", ctx.lang,
            )
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        # The claim and maintenance state are durable. KickService is allowed
        # to use the session for mailbox/browser preparation, but no Order,
        # Rental or Account row lock remains held during that external I/O.
        try:
            kick = await self._kick.kick(session, claim.account_id)
        except Exception as exc:
            await session.rollback()
            kick = KickResult(success=False, error=str(exc))
        else:
            try:
                await session.commit()
            except Exception:
                # Logout may already have succeeded externally. Its auxiliary
                # database reads are not part of the durable replacement
                # decision, which is re-authorized below.
                await session.rollback()

        await self._finalize_replacement(
            session,
            ctx,
            claim=claim,
            kick=kick,
        )

    async def _release_stale_replacement_reservation(
        self,
        session: AsyncSession,
        ctx: CommandContext,
        *,
        rental_id: int,
        account_id: int,
        observed_claim: datetime | None,
        observed_target_account_id: int,
    ) -> bool:
        """Release only the exact stale reservation observed by this call."""

        try:
            rental = await _find_rental_for_context(
                session,
                ctx,
                for_update=True,
            )
        except AmbiguousRentalError:
            await session.commit()
            return False
        stored_claim = rental.expiry_revoke_started_at if rental else None
        if stored_claim is not None:
            stored_claim = _as_utc(stored_claim)
        exact_claim = (
            (stored_claim is None and observed_claim is None)
            or (
                stored_claim is not None
                and observed_claim is not None
                and stored_claim == _as_utc(observed_claim)
            )
        )
        now = datetime.now(timezone.utc)
        if (
            rental is None
            or rental.id != rental_id
            or rental.account_id != account_id
            or rental.replacement_target_account_id
            != observed_target_account_id
            or not exact_claim
            or (
                stored_claim is not None
                and stored_claim > now - _REPLACEMENT_REVOKE_LEASE
            )
        ):
            await session.commit()
            return False
        rental.expiry_revoke_started_at = None
        rental.replacement_target_account_id = None
        session.add(AuditLog(
            event_type="replacement_stale_reservation_released",
            account_id=rental.account_id,
            rental_id=rental.id,
            chat_id=rental.buyer_funpay_chat_id,
            metadata_={
                "target_account_id": observed_target_account_id,
            },
        ))
        await session.commit()
        return True

    async def _claim_replacement_revoke(
        self,
        session: AsyncSession,
        ctx: CommandContext,
        *,
        original_rental_id: int | None,
        original_account_id: int | None,
    ) -> tuple[_ReplacementRevokeClaim | None, str | None]:
        """Commit a short Order -> Rental -> Account logout claim."""

        try:
            rental = await _find_rental_for_context(
                session,
                ctx,
                for_update=True,
            )
        except AmbiguousRentalError:
            await session.commit()
            return None, "rental_ambiguous"
        if (
            rental is None
            or rental.id != original_rental_id
            or rental.account_id != original_account_id
        ):
            await session.commit()
            return None, "seller_called"

        now = datetime.now(timezone.utc)
        denial_template = await _rental_access_denial(session, rental, now)
        if denial_template is not None:
            await session.commit()
            return None, denial_template
        if _replacement_window_is_too_short(rental, now):
            await session.commit()
            return None, "replace_expiring"
        if rental.replacement_count >= self._max_replacements:
            session.add(AuditLog(
                event_type="replacement_limit_reached",
                account_id=rental.account_id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={"limit": self._max_replacements},
            ))
            await session.commit()
            return None, "seller_called"

        stored_claim = rental.expiry_revoke_started_at
        if stored_claim is not None:
            stored_claim = _as_utc(stored_claim)
        if (
            stored_claim is not None
            and stored_claim > now - _REPLACEMENT_REVOKE_LEASE
        ):
            # The same timestamp is also used by refund and expiry. Never
            # duplicate an account-wide logout while any live owner holds it.
            session.add(AuditLog(
                event_type="replacement_revoke_busy",
                account_id=rental.account_id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={"claim_started_at": stored_claim.isoformat()},
            ))
            await session.commit()
            return None, "seller_called"

        # A crashed replacement may leave a stale target reservation. Only
        # the worker holding Order -> Rental may release it, and only after
        # the shared revoke lease is no longer live.
        if (
            stored_claim is not None
            or rental.replacement_target_account_id is not None
        ):
            rental.expiry_revoke_started_at = None
            rental.replacement_target_account_id = None
            await session.flush()

        old_account = (
            await session.execute(
                select(Account)
                .where(Account.id == rental.account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if old_account is None:
            await session.commit()
            return None, "seller_called"

        duration = await session.get(Duration, rental.duration_id)
        scope = await session.get(LimitScope, rental.limit_scope_id)
        if duration is None:
            await session.commit()
            return None, "seller_called"
        criteria = AccountCriteria(
            tier_id=rental.tier_id,
            duration_minutes=duration.minutes,
            scope=scope.code if scope else "any",
            min_limit_pct=rental.min_limit_pct,
            max_5h_pct=rental.max_5h_pct,
            max_weekly_pct=rental.max_weekly_pct,
            required_expires_at=_as_utc(rental.expires_at),
        )
        settings = await session.get(SellerSettings, 1)
        default_max = settings.default_max_active_rentals if settings else 1
        target_account = await self._pool.acquire_excluding(
            session,
            criteria,
            exclude_account_id=rental.account_id,
            default_max_active_rentals=default_max,
        )
        if target_account is None or target_account.id == rental.account_id:
            session.add(AuditLog(
                event_type="replacement_no_account",
                account_id=rental.account_id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
            ))
            # No durable revoke claim was created and the old account is not
            # changed here: a buyer must never lose working credentials merely
            # because the replacement pool is empty.
            await session.commit()
            return None, "replace_no_account"

        # AccountPool is DB-only, nevertheless repeat every authorization
        # check before persisting the target reservation. This also keeps
        # custom injected pools from bypassing expiry/refund boundaries.
        now = datetime.now(timezone.utc)
        denial_template = await _rental_access_denial(session, rental, now)
        if denial_template is not None:
            await session.commit()
            return None, denial_template
        if _replacement_window_is_too_short(rental, now):
            await session.commit()
            return None, "replace_expiring"
        if rental.replacement_count >= self._max_replacements:
            await session.commit()
            return None, "seller_called"

        old_account.status = "maintenance"
        rental.expiry_revoke_started_at = now
        rental.replacement_target_account_id = target_account.id
        claim = _ReplacementRevokeClaim(
            order_id=rental.order_id,
            rental_id=rental.id,
            account_id=rental.account_id,
            target_account_id=target_account.id,
            started_at=now,
        )
        session.add(AuditLog(
            event_type="replacement_revoke_claimed",
            account_id=rental.account_id,
            rental_id=rental.id,
            chat_id=rental.buyer_funpay_chat_id,
            metadata_={
                "claim_started_at": now.isoformat(),
                "target_account_id": target_account.id,
            },
        ))
        await session.commit()
        return claim, None

    async def _finalize_replacement(
        self,
        session: AsyncSession,
        ctx: CommandContext,
        *,
        claim: _ReplacementRevokeClaim,
        kick: KickResult,
    ) -> None:
        """Re-authorize and switch the target only for the exact claim."""

        order = (
            await session.execute(
                select(Order)
                .where(Order.id == claim.order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        rental = (
            await session.execute(
                select(Rental)
                .where(Rental.id == claim.rental_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

        stored_claim = rental.expiry_revoke_started_at if rental else None
        owns_timestamp = bool(
            stored_claim is not None
            and _as_utc(stored_claim) == _as_utc(claim.started_at)
        )
        target_matches = bool(
            rental is not None
            and rental.replacement_target_account_id
            == claim.target_account_id
        )
        owns_claim = owns_timestamp and target_matches
        identity_matches = bool(
            order is not None
            and rental is not None
            and rental.order_id == order.id == claim.order_id
            and rental.account_id == claim.account_id
            and rental.buyer_funpay_chat_id == str(ctx.chat_id)
            and (
                ctx.order_id is None
                or order.funpay_order_id == ctx.order_id
            )
        )
        target_account = None
        if owns_claim:
            target_account = (
                await session.execute(
                    select(Account)
                    .where(Account.id == claim.target_account_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        session.add(AuditLog(
            event_type="replacement_kick",
            account_id=claim.account_id,
            order_id=order.id if order is not None else None,
            rental_id=rental.id if rental is not None else None,
            chat_id=(
                rental.buyer_funpay_chat_id if rental is not None else None
            ),
            metadata_={
                "success": kick.success,
                "deduplicated": kick.deduplicated,
                "error": kick.error,
                "claim_owned": owns_claim,
                "identity_matched": identity_matches,
                "target_account_id": claim.target_account_id,
            },
        ))
        if owns_claim and rental is not None:
            # Clear only our exact lease. A refund/expiry owner that replaced a
            # stale claim must retain its own timestamp.
            rental.expiry_revoke_started_at = None
            rental.replacement_target_account_id = None

        denial_template = "seller_called"
        if not owns_claim or not identity_matches:
            await session.commit()
            text = await render_message(session, denial_template, ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return
        if not kick.success:
            # The old account remains in maintenance. It must not be returned
            # to the pool until an operator confirms safe recovery.
            await session.commit()
            text = await render_message(session, denial_template, ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return
        if (
            target_account is None
            or target_account.status != "active"
            or target_account.operator_status_override is not None
            or target_account.tier_id != rental.tier_id
        ):
            # The reservation is released, but the already revoked old account
            # remains in maintenance for explicit operator recovery.
            await session.commit()
            text = await render_message(session, "seller_called", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        # A refund claim is monotonic and wins even when its callback arrived
        # while the replacement logout was in flight. The refund scheduler can
        # acquire the now-cleared common lease and finish revocation later.
        if order.status in {"refund_pending", "refunded"}:
            await session.commit()
            text = await render_message(session, "code_expired", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        now = datetime.now(timezone.utc)
        denial_template = await _rental_access_denial(session, rental, now)
        if denial_template is not None:
            await session.commit()
            text = await render_message(session, denial_template, ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return
        if _replacement_window_is_too_short(rental, now):
            await session.commit()
            text = await render_message(
                session, "replace_expiring", ctx.lang,
            )
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
            await session.commit()
            text = await render_message(session, "seller_called", ctx.lang)
            await ctx.gateway.send_message(chat_id=ctx.chat_id, text=text)
            return

        rental.account_id = target_account.id
        rental.replacement_count += 1
        rental.credentials_delivery_status = "sending"
        rental.credentials_delivery_template = "replace_success"
        rental.credentials_delivery_started_at = datetime.now(timezone.utc)
        rental.credentials_delivery_next_attempt_at = None
        rental.credentials_delivered_at = None
        # A replacement is a new durable delivery claim. RentalService counts
        # actual external sends from zero; the old target's attempts must not
        # consume the replacement retry budget.
        rental.credentials_delivery_attempts = 0
        rental.credentials_delivery_last_error = None
        limits = await session.get(AccountLimits, target_account.id)
        if limits:
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
        await self._jobs.enqueue(
            session,
            account_id=claim.account_id,
            priority="refresh_recover",
            job_type="refresh_recover",
        )
        session.add(AuditLog(
            event_type="replacement_issued",
            account_id=target_account.id,
            rental_id=rental.id,
            chat_id=rental.buyer_funpay_chat_id,
            metadata_={"old_account_id": claim.account_id},
        ))
        # Commit the exact replacement target and a durable delivery claim
        # before touching FunPay. A retry then sends this same account instead
        # of allocating another one or getting stuck behind replacement_count.
        await session.commit()
        await self._rentals.deliver_claimed_rental(
            session, ctx.gateway, rental.id,
        )
