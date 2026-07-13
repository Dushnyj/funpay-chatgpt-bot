from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.funpay.gateway import ChatGateway
from app.models.account import Account, AccountLimits
from app.models.audit import AuditLog
from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.rental import Order, Rental
from app.models.settings import SellerSettings
from app.services.account_pool import AccountCriteria, AccountPool
from app.services.messages import issued_usage_template_variables, render_message


CREDENTIAL_DELIVERY_LEASE = timedelta(minutes=5)
CREDENTIAL_DELIVERY_MAX_ATTEMPTS = 6
_CREDENTIAL_RETRY_BASE = timedelta(minutes=1)
_CREDENTIAL_RETRY_MAX = timedelta(hours=1)
_FULFILLABLE_ORDER_STATUSES = {"pending", "completed"}


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
        *,
        notify_unavailable: bool = True,
    ) -> Rental | None:
        # Serialize the live FunPay callback and scheduled/manual retries for
        # the same order. Together with uq_rental_order this prevents two
        # workers from allocating and delivering two independent rentals.
        order = (
            await session.execute(
                select(Order).where(Order.id == order_id).with_for_update()
            )
        ).scalar_one_or_none()
        if order is None:
            raise KeyError(f"Order {order_id} not found")
        rental = await self._find_rental_by_order(session, order_id)
        if rental is not None:
            if not self._claim_existing_delivery(order, rental):
                return rental
            await session.commit()
            return await self._deliver_claimed(
                session, gateway, order.id, rental.id,
            )
        if order.status not in _FULFILLABLE_ORDER_STATUSES:
            return None

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
            if notify_unavailable:
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
            issued_codex_primary_pct=(
                limits.codex_primary_remaining_pct if limits else None
            ),
            issued_codex_primary_window_seconds=(
                limits.codex_primary_window_seconds if limits else None
            ),
            issued_codex_primary_resets_at=(
                limits.codex_primary_resets_at if limits else None
            ),
            issued_codex_secondary_pct=(
                limits.codex_secondary_remaining_pct if limits else None
            ),
            issued_codex_secondary_window_seconds=(
                limits.codex_secondary_window_seconds if limits else None
            ),
            issued_codex_secondary_resets_at=(
                limits.codex_secondary_resets_at if limits else None
            ),
            issued_plan_window_status=(
                limits.plan_window_status if limits else None
            ),
            issued_expected_long_window_seconds=(
                limits.expected_long_window_seconds if limits else None
            ),
            issued_limits_measured_at=limits.measured_at if limits else None,
            credentials_delivery_status="sending",
            credentials_delivery_template="welcome",
            credentials_delivery_started_at=now,
            credentials_delivery_next_attempt_at=None,
            credentials_delivery_attempts=1,
        )
        session.add(rental)
        await session.flush()
        # Persist the allocation before external delivery. If the process dies
        # after FunPay accepts the message, retries can only reuse this Rental
        # and therefore never allocate a second account for the same order.
        await session.commit()
        return await self._deliver_claimed(session, gateway, order.id, rental.id)

    async def deliver_claimed_rental(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        rental_id: int,
    ) -> Rental:
        """Deliver a previously committed primary/replacement credential claim."""
        rental = await session.get(Rental, rental_id)
        if rental is None:
            raise KeyError(f"Rental {rental_id} not found")
        return await self._deliver_claimed(
            session, gateway, rental.order_id, rental.id,
        )

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

    @staticmethod
    def _claim_existing_delivery(order: Order, rental: Rental) -> bool:
        if (
            order.status not in _FULFILLABLE_ORDER_STATUSES
            or rental.status != "active"
            or rental.credentials_delivery_status == "sent"
            or rental.credentials_delivery_status == "manual"
            or rental.credentials_delivery_attempts
            >= CREDENTIAL_DELIVERY_MAX_ATTEMPTS
        ):
            return False

        now = datetime.now(timezone.utc)
        next_attempt_at = rental.credentials_delivery_next_attempt_at
        if next_attempt_at is not None and next_attempt_at.tzinfo is None:
            next_attempt_at = next_attempt_at.replace(tzinfo=timezone.utc)
        if next_attempt_at is not None and next_attempt_at > now:
            return False

        started_at = rental.credentials_delivery_started_at
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if (
            rental.credentials_delivery_status == "sending"
            and started_at is not None
            and started_at > now - CREDENTIAL_DELIVERY_LEASE
        ):
            return False

        rental.credentials_delivery_status = "sending"
        rental.credentials_delivery_started_at = now
        rental.credentials_delivery_next_attempt_at = None
        rental.credentials_delivery_attempts += 1
        rental.credentials_delivery_last_error = None
        return True

    async def _deliver_claimed(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order_id: int,
        rental_id: int,
    ) -> Rental:
        # Reacquire the order lock after the allocation commit. Refund handling
        # uses the same lock, so credentials cannot race a completed refund.
        order = (
            await session.execute(
                select(Order).where(Order.id == order_id).with_for_update()
            )
        ).scalar_one()
        rental = await session.get(Rental, rental_id)
        if rental is None:
            raise KeyError(f"Rental {rental_id} not found")
        if (
            order.status not in _FULFILLABLE_ORDER_STATUSES
            or rental.status != "active"
            or rental.credentials_delivery_status != "sending"
        ):
            if rental.credentials_delivery_status == "sending":
                rental.credentials_delivery_status = "failed"
                rental.credentials_delivery_last_error = "order_not_fulfillable"
                rental.credentials_delivery_next_attempt_at = None
                await session.commit()
            return rental

        account = await session.get(Account, rental.account_id)
        duration = await session.get(Duration, rental.duration_id)
        if account is None or duration is None:
            rental.credentials_delivery_status = "manual"
            rental.credentials_delivery_last_error = "delivery_data_missing"
            rental.credentials_delivery_next_attempt_at = None
            self._record_manual_delivery_required(session, rental)
            await session.commit()
            return rental
        try:
            await self._send_welcome(
                session,
                gateway,
                order,
                account,
                rental,
                duration.days,
                template_key=rental.credentials_delivery_template,
            )
        except Exception as exc:
            self._schedule_delivery_retry(
                session,
                rental,
                f"delivery_failed:{type(exc).__name__}"[:128],
            )
            await session.commit()
            raise

        delivered_at = datetime.now(timezone.utc)
        rental.credentials_delivery_status = "sent"
        rental.credentials_delivered_at = delivered_at
        rental.credentials_delivery_next_attempt_at = None
        rental.credentials_delivery_last_error = None
        # Only the initial paid rental begins at credential delivery. A
        # replacement must preserve the customer's original expiry instead of
        # silently granting a new full term.
        if rental.credentials_delivery_template == "welcome":
            rental.started_at = delivered_at
            rental.expires_at = delivered_at + timedelta(days=duration.days)
        await session.commit()
        return rental

    @staticmethod
    def _schedule_delivery_retry(
        session: AsyncSession,
        rental: Rental,
        error: str,
    ) -> None:
        rental.credentials_delivery_last_error = error[:128]
        if rental.credentials_delivery_attempts >= CREDENTIAL_DELIVERY_MAX_ATTEMPTS:
            rental.credentials_delivery_status = "manual"
            rental.credentials_delivery_next_attempt_at = None
            RentalService._record_manual_delivery_required(session, rental)
            return

        exponent = max(0, rental.credentials_delivery_attempts - 1)
        delay_seconds = min(
            _CREDENTIAL_RETRY_BASE.total_seconds() * (2**exponent),
            _CREDENTIAL_RETRY_MAX.total_seconds(),
        )
        rental.credentials_delivery_status = "failed"
        rental.credentials_delivery_next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        )

    @staticmethod
    def _record_manual_delivery_required(
        session: AsyncSession,
        rental: Rental,
    ) -> None:
        session.add(
            AuditLog(
                event_type="credential_delivery_manual_required",
                account_id=rental.account_id,
                rental_id=rental.id,
                chat_id=rental.buyer_funpay_chat_id,
                metadata_={
                    "attempts": rental.credentials_delivery_attempts,
                    "error": rental.credentials_delivery_last_error,
                },
            )
        )

    async def _send_welcome(
        self,
        session: AsyncSession,
        gateway: ChatGateway,
        order: Order,
        account: Account,
        rental: Rental,
        days: int,
        *,
        template_key: str = "welcome",
    ) -> None:
        tier = await session.get(SubscriptionTier, account.tier_id)
        # account.password_encrypted использует FernetEncrypted TypeDecorator:
        # ORM автоматически расшифровывает при чтении, отдавая plaintext.
        password = account.password_encrypted
        lang = order.buyer_locale or "ru"
        if template_key not in {"welcome", "replace_success"}:
            template_key = "welcome"
        text = await render_message(
            session, template_key, lang,
            login=account.login,
            password=password,
            tier=tier.name if tier else "",
            days=days,
            expires_at=_fmt_expires(account.subscription_expires_at),
            **issued_usage_template_variables(rental, lang=lang),
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


def _fmt_expires(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y")
